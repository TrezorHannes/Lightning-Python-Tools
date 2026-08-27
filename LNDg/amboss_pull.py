# Purpose: This script downloads active and expired Magma orders from Amboss.
# It writes long-channel-IDs into charge-lnd rule files by fee cap, removes channels
# once the lease expires, re-enables AutoFees in LNDg, and logs status notes in LNDg.

import requests
import os
import datetime
import time
import logging
import configparser
import json
import re
from typing import Dict, List, Tuple, Optional, Any

# Grace period in blocks until charge-lnd changes from static to proportional fee strategy
fee_grace_period = 2016

# Directories & Configuration
parent_dir = os.path.dirname(os.path.abspath(__file__))
config_file_path = os.path.join(parent_dir, "..", "config.ini")
config = configparser.ConfigParser()
config.read(config_file_path)

# --- GraphQL Endpoints ---
MAGMA_GRAPHQL_URL = "https://magma.amboss.tech/graphql"
AMBOSS_SPACE_GRAPHQL_URL = "https://api.amboss.space/graphql"

# Credentials
AMBOSS_TOKEN = config.get("credentials", "amboss_authorization", fallback="")
LNDG_USERNAME = config.get("credentials", "lndg_username", fallback="")
LNDG_PASSWORD = config.get("credentials", "lndg_password", fallback="")
LNDG_BASE_URL = config.get("lndg", "lndg_api_url", fallback="http://localhost:8889")
LNDG_CHANNELS_URL = f"{LNDG_BASE_URL}/api/channels/?is_active=true&is_open=true&limit=300&offset=0"

# Output Paths
CHARGE_LND_PATH = config.get("paths", "charge_lnd_path", fallback="/tmp/charge-lnd")
FINISHED_FILE_PATH = os.path.join(CHARGE_LND_PATH, "magma-finished.txt")
LOG_FILE_PATH = os.path.join(parent_dir, "..", "logs", "amboss-LNDg_changes.log")

# Setup Logging
logs_dir = os.path.join(parent_dir, "..", "logs")
if not os.path.exists(logs_dir):
    try:
        os.makedirs(logs_dir, exist_ok=True)
    except Exception:
        pass

logging.basicConfig(
    filename=LOG_FILE_PATH,
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)


class AmbossAPIError(Exception):
    """Represents an error when interacting with Amboss GraphQL APIs."""
    def __init__(self, message, status_code=None, response_data=None):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data


def get_current_timestamp() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --- GraphQL Queries ---

GET_USER_ORDERS_QUERY = """
query GetUserOrders($input: OrderInput, $page: PageInput) {
  user {
    market {
      orders {
        sales(input: $input, page: $page) {
          total
          pagination {
            limit
            offset
          }
          list {
            id
            status
            channel_id
            created_at
            amount {
              satoshi {
                sats
              }
            }
            promises {
              locked_min_block_length
              locked_fee_rate_cap {
                sats
              }
            }
          }
        }
        purchases(input: $input, page: $page) {
          total
          pagination {
            limit
            offset
          }
          list {
            id
            status
            channel_id
            created_at
            amount {
              satoshi {
                sats
              }
            }
            promises {
              locked_min_block_length
              locked_fee_rate_cap {
                sats
              }
            }
          }
        }
      }
    }
  }
}
"""

GET_EDGE_INFO_BATCH_QUERY = """
query GetEdgeInfoBatch($ids: [String!]!) {
  getEdgeInfoBatch(ids: $ids) {
    long_channel_id
    short_channel_id
  }
}
"""


def scid_to_short_channel_id(scid_str: Optional[str]) -> Optional[str]:
    """
    Converts standard Lightning Network SCID format (e.g., '892345x123x1', '892345:123:1', '892345/123/1')
    to a 64-bit integer channel ID string via mathematical bit-shift:
    (block << 40) | (tx_index << 16) | output_index
    """
    if not scid_str or not isinstance(scid_str, str):
        return None
    
    parts = re.split(r'[:x/]', scid_str.strip())
    if len(parts) != 3:
        return None
    
    try:
        block = int(parts[0])
        tx_idx = int(parts[1])
        out_idx = int(parts[2])
        long_id = (block << 40) | (tx_idx << 16) | out_idx
        return str(long_id)
    except (ValueError, OverflowError):
        return None


def convert_short_to_long_chan_id(short_chan_ids: List[str], amboss_token: Optional[str] = None) -> Dict[str, str]:
    """
    Converts short channel IDs to long channel IDs using Amboss Space API getEdgeInfoBatch,
    with an automatic deterministic mathematical SCID bit-shift fallback if the API fails.
    """
    if not short_chan_ids:
        return {}

    token = amboss_token or AMBOSS_TOKEN
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    variables = {"ids": list(short_chan_ids)}
    payload = {"query": GET_EDGE_INFO_BATCH_QUERY, "variables": variables}

    long_chan_id_map: Dict[str, str] = {}

    try:
        response = requests.post(AMBOSS_SPACE_GRAPHQL_URL, json=payload, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()

        if "data" in data and data["data"] and "getEdgeInfoBatch" in data["data"] and data["data"]["getEdgeInfoBatch"]:
            for edge in data["data"]["getEdgeInfoBatch"]:
                if edge and "short_channel_id" in edge and "long_channel_id" in edge:
                    long_chan_id_map[edge["short_channel_id"]] = str(edge["long_channel_id"])
    except Exception as e:
        logging.warning(f"Failed to query getEdgeInfoBatch from Space API: {e}. Using mathematical fallback.")

    # Mathematical fallback for any missing channel IDs
    for scid in short_chan_ids:
        if scid not in long_chan_id_map:
            math_id = scid_to_short_channel_id(scid)
            if math_id:
                long_chan_id_map[scid] = math_id
            else:
                logging.error(f"Could not convert short channel ID: {scid}")

    return long_chan_id_map


def get_fee_cap_file_path(fee_cap: Any) -> str:
    """Returns the file path for charge-lnd rules for a specific fee cap."""
    return os.path.join(CHARGE_LND_PATH, f"magma-channels_{fee_cap}.txt")


def extract_order_channel_info(order: dict) -> dict:
    """Extracts normalized fields from a Magma Order object (supporting both modern and legacy shapes)."""
    if not order:
        return {}

    order_id = order.get("id")
    status = str(order.get("status", "UNKNOWN")).upper()
    channel_id = order.get("channel_id")
    created_at = order.get("created_at")

    # Promises (fee cap & min block length)
    promises = order.get("promises", {})
    if isinstance(promises, dict):
        min_block_length = int(promises.get("locked_min_block_length") or 0)
        fee_cap_obj = promises.get("locked_fee_rate_cap")
        if isinstance(fee_cap_obj, dict):
            fee_cap = int(fee_cap_obj.get("sats", 0) or 0)
        elif isinstance(fee_cap_obj, (int, str)) and str(fee_cap_obj).isdigit():
            fee_cap = int(fee_cap_obj)
        else:
            fee_cap = int(promises.get("fee_rate_cap") or 0)
    else:
        min_block_length = int(order.get("locked_min_block_length") or order.get("min_block_length") or 0)
        fee_cap = int(order.get("locked_fee_rate_cap") or order.get("fee_rate_cap") or 0)

    blocks_until_close = int(order.get("blocks_until_can_be_closed") or order.get("blocks_until_close") or 0)

    return {
        "id": order_id,
        "status": status,
        "channel_id": channel_id,
        "locked_min_block_length": min_block_length,
        "locked_fee_rate_cap": fee_cap,
        "blocks_until_close": blocks_until_close,
        "created_at": created_at,
        "raw_order": order
    }


def fetch_magma_orders(amboss_token: Optional[str] = None, max_attempts: int = 5, timeout: int = 15) -> List[dict]:
    """Fetches user's active/historical Magma orders (both sales and purchases) from Magma GraphQL API."""
    token = amboss_token or AMBOSS_TOKEN
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"query": GET_USER_ORDERS_QUERY, "variables": {"page": {"limit": 100, "offset": 0}}}

    data = None
    for attempt in range(max_attempts):
        try:
            response = requests.post(MAGMA_GRAPHQL_URL, json=payload, headers=headers, timeout=timeout)
            response.raise_for_status()
            data = response.json()
            break
        except Exception as e:
            logging.error(f"Error fetching data from Magma API (attempt {attempt+1}/{max_attempts}): {e}")
            if attempt == max_attempts - 1:
                logging.error("Exceeded max retry attempts fetching Magma orders.")
                return []
            time.sleep(2)

    if not data or "data" not in data or not data["data"]:
        logging.warning(f"No order data returned from Magma API: {data}")
        return []

    orders_node = data["data"].get("user", {}).get("market", {}).get("orders", {})
    sales = orders_node.get("sales", {}).get("list", []) if isinstance(orders_node, dict) else []
    purchases = orders_node.get("purchases", {}).get("list", []) if isinstance(orders_node, dict) else []

    all_orders = []
    if isinstance(sales, list):
        all_orders.extend(sales)
    if isinstance(purchases, list):
        all_orders.extend(purchases)

    return all_orders


def cluster_sold_channels(orders: Optional[List[dict]] = None, fee_grace: int = 2016) -> Tuple[List[tuple], List[str], Dict[Any, List[str]]]:
    """
    Categorizes channels, writes channel lists to charge-lnd directory by fee cap,
    and returns categorized channel structures.
    """
    if orders is None:
        orders = fetch_magma_orders()

    valid_orders = [o for o in orders if o and o.get("channel_id")]
    short_chan_ids = [o["channel_id"] for o in valid_orders]
    long_chan_id_map = convert_short_to_long_chan_id(short_chan_ids)

    active_channels_info: List[tuple] = []
    non_active_chan_ids: List[str] = []
    fee_cap_groups: Dict[Any, List[str]] = {}

    for order in valid_orders:
        info = extract_order_channel_info(order)
        short_chan_id = info["channel_id"]
        long_chan_id = long_chan_id_map.get(short_chan_id)

        if not long_chan_id:
            logging.error(f"Warning: No long channel ID found for short channel ID {short_chan_id}")
            continue

        status = info["status"]
        blocks_until_close = info["blocks_until_close"]
        min_block_length = info["locked_min_block_length"]
        fee_cap = info["locked_fee_rate_cap"]

        if status in ("VALID_CHANNEL_OPENING", "WAITING_FOR_CHANNEL_OPEN", "ACTIVE"):
            fee_grace_calc = -1 * (min_block_length - blocks_until_close - fee_grace)
            active_channels_info.append((long_chan_id, blocks_until_close, fee_cap, fee_grace_calc))

            if fee_cap not in fee_cap_groups:
                fee_cap_groups[fee_cap] = []
            fee_cap_groups[fee_cap].append(long_chan_id)

        elif status in ("CHANNEL_MONITORING_FINISHED", "CLOSED", "EXPIRED") or blocks_until_close == 0:
            non_active_chan_ids.append(long_chan_id)
            logging.debug(f"Added to non_active_chan_ids: {long_chan_id}")

        else:
            logging.info(f"Channel {long_chan_id} with status {status} and blocks {blocks_until_close} not clustered.")

    # Write active fee cap files if charge_lnd_path exists or can be created
    try:
        os.makedirs(CHARGE_LND_PATH, exist_ok=True)
        for fee_cap, channel_ids in fee_cap_groups.items():
            file_path = get_fee_cap_file_path(fee_cap)
            with open(file_path, "w") as output_file:
                for chan_id in channel_ids:
                    output_file.write(f"{chan_id}\n")

        with open(FINISHED_FILE_PATH, "w") as finished_file:
            for chan_id in non_active_chan_ids:
                finished_file.write(f"{chan_id}\n")
    except Exception as e:
        logging.error(f"Error writing charge-lnd channel files: {e}")

    return active_channels_info, non_active_chan_ids, fee_cap_groups


def update_autofees(non_active_chan_ids: List[str]):
    """Update auto_fees and notes in LNDg for expired Magma channels."""
    timestamp = get_current_timestamp()

    def fetch_current_channel_states() -> Dict[str, bool]:
        current_states = {}
        try:
            response = requests.get(LNDG_CHANNELS_URL, auth=(LNDG_USERNAME, LNDG_PASSWORD), timeout=10)
            if response.status_code == 200:
                data = response.json()
                for channel in data.get("results", []):
                    chan_id = str(channel.get("chan_id", ""))
                    auto_fees = channel.get("auto_fees", False)
                    if not auto_fees:
                        current_states[chan_id] = False
            else:
                logging.error(f"{timestamp} Failed to fetch LNDg channel states: {response.status_code}")
        except Exception as e:
            logging.error(f"{timestamp} Error fetching current channel states: {e}")
        return current_states

    current_channel_states = fetch_current_channel_states()
    channels_to_update = [c for c in non_active_chan_ids if str(c) in current_channel_states]

    for chan_id in channels_to_update:
        notes = "Status: ⛰️ Magma Channel Buy Order Expired"
        payload = {"chan_id": chan_id, "auto_fees": True, "notes": notes}
        try:
            put_url = f"{LNDG_BASE_URL}/api/channels/{chan_id}/"
            response = requests.put(put_url, json=payload, auth=(LNDG_USERNAME, LNDG_PASSWORD), timeout=10)
            if response.status_code == 200:
                with open(LOG_FILE_PATH, "a") as log_file:
                    log_file.write(f"{timestamp}: Updated auto_fees for channel {chan_id}\n")
                logging.info(f"Updated auto_fees for channel {chan_id}")
            else:
                logging.error(f"{timestamp}: Failed to update auto_fees for channel {chan_id}: {response.status_code}")
        except Exception as e:
            logging.error(f"Error updating auto_fees for channel {chan_id}: {e}")


def update_notes_for_active_channels(active_channels_info: List[tuple]):
    """Update notes in LNDg for active leased Magma channels."""
    timestamp = get_current_timestamp()

    for item in active_channels_info:
        try:
            chan_id, blocks_until_close, fee_cap, min_block_length = item
        except (ValueError, TypeError):
            logging.error(f"Error unpacking item: {item}. Expected a 4-element tuple.")
            continue

        if min_block_length < 0:
            notes = f"Status: 🌋 Magma Channel Buy Order Active \n(Lease Expiration: {blocks_until_close} blocks). \nFee Cap: {fee_cap}. Proportional Fee Rate activated ✅"
        else:
            notes = f"Status: 🌋 Magma Channel Buy Order Active \n(Lease Expiration: {blocks_until_close} blocks). \nFee Cap: {fee_cap}. Proportional Fee Rate in: {min_block_length}."

        payload = {"chan_id": chan_id, "auto_fees": False, "notes": notes}
        try:
            put_url = f"{LNDG_BASE_URL}/api/channels/{chan_id}/"
            response = requests.put(put_url, json=payload, auth=(LNDG_USERNAME, LNDG_PASSWORD), timeout=10)
            if response.status_code == 200:
                with open(LOG_FILE_PATH, "a") as log_file:
                    log_file.write(f"{timestamp}: Updated notes for channel {chan_id}\n")
                logging.debug(f"Updated notes for channel {chan_id}")
            else:
                logging.error(f"{timestamp}: Failed to update notes for channel {chan_id}: {response.status_code}")
        except Exception as e:
            logging.error(f"Error updating notes for channel {chan_id}: {e}")


if __name__ == "__main__":
    active_info, non_active_ids, fee_groups = cluster_sold_channels()
    update_autofees(non_active_ids)
    update_notes_for_active_channels(active_info)
