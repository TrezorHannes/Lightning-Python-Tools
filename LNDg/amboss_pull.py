# Purpose: This script downloads active and expired Magma sales from Amboss.
# It writes long-channel-IDs into charge-lnd rule files by fee cap, removes channels
# once the lease expires, re-enables AutoFees in LNDg, and logs status notes in LNDg.
#
# Failure policy (fail closed): if the Amboss API cannot be read completely, no
# charge-lnd rule file and no LNDg channel is touched, and the process exits non-zero.

import requests
import os
import re
import sys
import datetime
import time
import logging
import tempfile
import configparser
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

# Output Paths
CHARGE_LND_PATH = config.get("paths", "charge_lnd_path", fallback="/tmp/charge-lnd")
FINISHED_FILE_NAME = "magma-finished.txt"
FEE_CAP_FILE_PATTERN = re.compile(r"^magma-channels_(\d+)\.txt$")
LOG_FILE_PATH = os.path.join(parent_dir, "..", "logs", "amboss-LNDg_changes.log")

# --- Magma order lifecycle ---
# Only orders we SOLD carry a seller fee-cap promise; purchases are intentionally excluded.
ACTIVE_LEASE_STATUSES = frozenset({"VALID_CHANNEL_OPENING"})
FINISHED_LEASE_STATUSES = frozenset({"CHANNEL_MONITORING_FINISHED"})

SALES_PAGE_SIZE = 100
MAX_SALES_PAGES = 50  # hard guard against runaway pagination (5000 orders)
RESPONSE_BODY_LOG_LIMIT = 1000


class AmbossAPIError(Exception):
    """Represents an error when interacting with Amboss GraphQL APIs."""
    def __init__(self, message, status_code=None, response_data=None, transient=False):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data
        self.transient = transient


def configure_logging() -> None:
    """Configure file logging. Called from main() only, so importing has no side effects."""
    os.makedirs(os.path.dirname(LOG_FILE_PATH), exist_ok=True)
    logging.basicConfig(
        filename=LOG_FILE_PATH,
        level=logging.DEBUG,
        format="%(asctime)s - %(levelname)s - %(message)s"
    )
    # urllib3 connection chatter adds no diagnostic value over our own request logging
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_current_timestamp() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def lndg_channels_url() -> str:
    return f"{LNDG_BASE_URL}/api/channels/?is_open=true&limit=1000&offset=0"


def lndg_channel_url(chan_id: str) -> str:
    return f"{LNDG_BASE_URL}/api/channels/{chan_id}/"


# --- GraphQL Queries ---
# NOTE: list items are `SimpleMarketOrder`, which does NOT expose `promises` or
# `blocks_until_can_be_closed`. Those live on the full `MarketOrder` (get_order).

GET_SALES_PAGE_QUERY = """
query GetMagmaSales($page: PageInput) {
  user {
    market {
      orders {
        sales(page: $page) {
          total
          list {
            id
            status
            channel_id
            created_at
          }
        }
      }
    }
  }
}
"""

GET_ORDER_LEASE_DETAILS_QUERY = """
query GetOrderLeaseDetails($orderId: String!) {
  user {
    market {
      orders {
        get_order(order_id: $orderId) {
          id
          status
          channel_id
          blocks_until_can_be_closed
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
"""

GET_EDGE_INFO_BATCH_QUERY = """
query GetEdgeInfoBatch($ids: [String!]!) {
  getEdgeInfoBatch(ids: $ids) {
    long_channel_id
    short_channel_id
  }
}
"""


def _truncate(text: Optional[str]) -> str:
    text = text or ""
    return text if len(text) <= RESPONSE_BODY_LOG_LIMIT else text[:RESPONSE_BODY_LOG_LIMIT] + "...[truncated]"


def _dig(data: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _to_int(value: Any, default: int = 0) -> int:
    """Coerces GraphQL Int/Float/String scalars (e.g. 8640, 8640.0, "1650") to int."""
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def execute_graphql(
    query: str,
    variables: Optional[dict],
    operation: str,
    url: str = MAGMA_GRAPHQL_URL,
    amboss_token: Optional[str] = None,
    max_attempts: int = 5,
    timeout: int = 15,
    retry_delay: float = 2.0,
) -> dict:
    """
    Executes a GraphQL request and returns its `data` object.

    Transient failures (network errors, timeouts, HTTP 429/5xx) are retried. Deterministic
    failures (HTTP 4xx, GraphQL `errors`, malformed bodies) raise immediately, with the
    server's error body logged so schema drift is diagnosable from the log alone.
    """
    token = amboss_token or AMBOSS_TOKEN
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"query": query, "variables": variables or {}}

    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as e:
            error = AmbossAPIError(f"{operation}: network error: {e}", transient=True)
        else:
            error = _classify_graphql_response(response, operation)
            if error is None:
                return response.json()["data"]

        if not error.transient:
            logging.error(str(error))
            raise error

        logging.warning(f"{error} (attempt {attempt}/{max_attempts})")
        if attempt == max_attempts:
            raise AmbossAPIError(
                f"{operation}: exceeded {max_attempts} attempts; last error: {error}",
                status_code=error.status_code,
                response_data=error.response_data,
                transient=True,
            )
        time.sleep(retry_delay)

    raise AmbossAPIError(f"{operation}: max_attempts must be >= 1")  # pragma: no cover


def _classify_graphql_response(response: Any, operation: str) -> Optional[AmbossAPIError]:
    """Returns None for a usable response, otherwise an AmbossAPIError describing the failure."""
    status = response.status_code
    try:
        body = response.json()
    except ValueError:
        body = None

    if status == 429 or status >= 500:
        return AmbossAPIError(
            f"{operation}: HTTP {status}: {_truncate(response.text)}",
            status_code=status, response_data=body, transient=True,
        )
    if status >= 400:
        return AmbossAPIError(
            f"{operation}: HTTP {status}: {_truncate(response.text)}",
            status_code=status, response_data=body,
        )
    if not isinstance(body, dict):
        return AmbossAPIError(
            f"{operation}: non-JSON response body: {_truncate(response.text)}", status_code=status,
        )
    if body.get("errors"):
        messages = "; ".join(str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in body["errors"])
        return AmbossAPIError(
            f"{operation}: GraphQL errors: {_truncate(messages)}", status_code=status, response_data=body,
        )
    if not isinstance(body.get("data"), dict):
        return AmbossAPIError(
            f"{operation}: response has no data object: {_truncate(response.text)}",
            status_code=status, response_data=body,
        )
    return None


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

    long_chan_id_map: Dict[str, str] = {}

    try:
        data = execute_graphql(
            GET_EDGE_INFO_BATCH_QUERY,
            {"ids": list(short_chan_ids)},
            "GetEdgeInfoBatch",
            url=AMBOSS_SPACE_GRAPHQL_URL,
            amboss_token=amboss_token,
            max_attempts=1,
            timeout=10,
        )
        for edge in data.get("getEdgeInfoBatch") or []:
            if edge and edge.get("short_channel_id") and edge.get("long_channel_id"):
                long_chan_id_map[edge["short_channel_id"]] = str(edge["long_channel_id"])
    except AmbossAPIError as e:
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


def get_fee_cap_file_path(fee_cap: Any, output_dir: Optional[str] = None) -> str:
    """Returns the file path for charge-lnd rules for a specific fee cap."""
    return os.path.join(output_dir or CHARGE_LND_PATH, f"magma-channels_{fee_cap}.txt")


def extract_order_channel_info(order: dict) -> dict:
    """Extracts normalized lease fields from a (detail-enriched) Magma Order object."""
    if not order:
        return {}

    promises = order.get("promises")
    if not isinstance(promises, dict):
        promises = {}

    fee_cap_obj = promises.get("locked_fee_rate_cap")
    if isinstance(fee_cap_obj, dict):
        fee_cap = _to_int(fee_cap_obj.get("sats"))
    else:
        fee_cap = _to_int(fee_cap_obj)

    return {
        "id": order.get("id"),
        "status": str(order.get("status") or "UNKNOWN").upper(),
        "channel_id": order.get("channel_id"),
        "locked_min_block_length": _to_int(promises.get("locked_min_block_length")),
        "locked_fee_rate_cap": fee_cap,
        "blocks_until_close": _to_int(order.get("blocks_until_can_be_closed")),
        "created_at": order.get("created_at"),
        "raw_order": order
    }


def fetch_magma_sales(
    amboss_token: Optional[str] = None,
    page_size: int = SALES_PAGE_SIZE,
    max_pages: int = MAX_SALES_PAGES,
) -> List[dict]:
    """Fetches ALL of the user's Magma sales (paginated). Raises AmbossAPIError on any failure."""
    orders: List[dict] = []
    seen_ids = set()
    offset = 0

    for _ in range(max_pages):
        data = execute_graphql(
            GET_SALES_PAGE_QUERY,
            {"page": {"limit": page_size, "offset": offset}},
            "GetMagmaSales",
            amboss_token=amboss_token,
        )
        sales = _dig(data, "user", "market", "orders", "sales")
        if not isinstance(sales, dict) or not isinstance(sales.get("list"), list):
            raise AmbossAPIError(f"GetMagmaSales: unexpected response shape: {_truncate(str(data))}")

        page = sales["list"]
        total = _to_int(sales.get("total"))
        for order in page:
            order_id = order.get("id") if isinstance(order, dict) else None
            if order_id and order_id not in seen_ids:
                seen_ids.add(order_id)
                orders.append(order)

        offset += len(page)
        if not page or offset >= total:
            logging.debug(f"Fetched {len(orders)} Magma sales (server total {total}).")
            return orders

    raise AmbossAPIError(f"GetMagmaSales: pagination exceeded {max_pages} pages of {page_size}")


def fetch_order_lease_details(order_id: str, amboss_token: Optional[str] = None) -> dict:
    """Fetches the full MarketOrder (promises, blocks_until_can_be_closed). Raises on failure."""
    data = execute_graphql(
        GET_ORDER_LEASE_DETAILS_QUERY,
        {"orderId": order_id},
        f"GetOrderLeaseDetails-{order_id}",
        amboss_token=amboss_token,
    )
    order = _dig(data, "user", "market", "orders", "get_order")
    if not isinstance(order, dict):
        raise AmbossAPIError(f"GetOrderLeaseDetails-{order_id}: order missing in response")
    return order


def fetch_magma_orders(amboss_token: Optional[str] = None) -> List[dict]:
    """
    Fetches all Magma sales and enriches active leases with their lease promises.
    Raises AmbossAPIError if any part fails, so callers never act on partial data.
    """
    orders = []
    for order in fetch_magma_sales(amboss_token=amboss_token):
        status = str(order.get("status") or "").upper()
        if status in ACTIVE_LEASE_STATUSES and order.get("channel_id"):
            order = {**order, **fetch_order_lease_details(order["id"], amboss_token=amboss_token)}
        orders.append(order)
    return orders


def _atomic_write_lines(path: str, lines: List[str]) -> None:
    """Atomically replaces `path`, so charge-lnd never reads a partially written file.
    Preserves the existing file mode (tempfile defaults to 0600, which would lock out
    charge-lnd running as a different user); new files get 0644."""
    directory = os.path.dirname(path) or "."
    try:
        mode = os.stat(path).st_mode & 0o777
    except FileNotFoundError:
        mode = 0o644

    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as tmp_file:
            tmp_file.writelines(f"{line}\n" for line in lines)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def write_charge_lnd_rule_files(
    fee_cap_groups: Dict[int, List[str]],
    non_active_chan_ids: List[str],
    output_dir: Optional[str] = None,
) -> None:
    """
    Writes one rule file per active fee cap plus the finished-lease file. Existing fee-cap
    files with no remaining active lease are emptied (not deleted, since charge-lnd config
    references them), so expired channels stop being governed by a stale cap.
    """
    output_dir = output_dir or CHARGE_LND_PATH
    os.makedirs(output_dir, exist_ok=True)

    stale_caps = set()
    for name in os.listdir(output_dir):
        match = FEE_CAP_FILE_PATTERN.match(name)
        if match and int(match.group(1)) not in fee_cap_groups:
            stale_caps.add(int(match.group(1)))

    for fee_cap, channel_ids in fee_cap_groups.items():
        _atomic_write_lines(get_fee_cap_file_path(fee_cap, output_dir), channel_ids)
    for fee_cap in sorted(stale_caps):
        logging.info(f"Clearing stale fee-cap rule file for cap {fee_cap} (no active Magma lease).")
        _atomic_write_lines(get_fee_cap_file_path(fee_cap, output_dir), [])

    _atomic_write_lines(os.path.join(output_dir, FINISHED_FILE_NAME), non_active_chan_ids)


def cluster_sold_channels(
    orders: Optional[List[dict]] = None,
    fee_grace: int = fee_grace_period,
    output_dir: Optional[str] = None,
) -> Tuple[List[tuple], List[str], Dict[int, List[str]]]:
    """
    Categorizes channels, writes channel lists to charge-lnd directory by fee cap,
    and returns categorized channel structures.

    Raises AmbossAPIError (fetch) or OSError (write) without partially applying changes
    from incomplete data.
    """
    if orders is None:
        orders = fetch_magma_orders()

    valid_orders = [o for o in orders if o and o.get("channel_id")]
    short_chan_ids = list(dict.fromkeys(o["channel_id"] for o in valid_orders))
    long_chan_id_map = convert_short_to_long_chan_id(short_chan_ids)

    active_channels_info: List[tuple] = []
    non_active_chan_ids: List[str] = []
    fee_cap_groups: Dict[int, List[str]] = {}

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

        if status in ACTIVE_LEASE_STATUSES and blocks_until_close > 0:
            fee_grace_calc = -1 * (min_block_length - blocks_until_close - fee_grace)
            active_channels_info.append((long_chan_id, blocks_until_close, fee_cap, fee_grace_calc))
            fee_cap_groups.setdefault(fee_cap, [])
            if long_chan_id not in fee_cap_groups[fee_cap]:
                fee_cap_groups[fee_cap].append(long_chan_id)

        elif status in FINISHED_LEASE_STATUSES or (status in ACTIVE_LEASE_STATUSES and blocks_until_close <= 0):
            if long_chan_id not in non_active_chan_ids:
                non_active_chan_ids.append(long_chan_id)
            logging.debug(f"Added to non_active_chan_ids: {long_chan_id}")

        else:
            logging.info(f"Channel {long_chan_id} with status {status} and blocks {blocks_until_close} not clustered.")

    # A channel with any active lease must never be treated as finished (AutoFees re-enable)
    active_ids = {chan_id for ids in fee_cap_groups.values() for chan_id in ids}
    non_active_chan_ids = [c for c in non_active_chan_ids if c not in active_ids]

    write_charge_lnd_rule_files(fee_cap_groups, non_active_chan_ids, output_dir)

    return active_channels_info, non_active_chan_ids, fee_cap_groups


def update_autofees(non_active_chan_ids: List[str]) -> Dict[str, int]:
    """Re-enable auto_fees and set notes in LNDg for expired Magma channels.
    Returns counts of updated and failed channels; a failed state fetch counts as one failure."""
    result = {"updated": 0, "failed": 0}
    if not non_active_chan_ids:
        return result

    try:
        response = requests.get(lndg_channels_url(), auth=(LNDG_USERNAME, LNDG_PASSWORD), timeout=10)
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching LNDg channel states: {e}")
        result["failed"] += 1
        return result
    if response.status_code != 200:
        logging.error(f"Failed to fetch LNDg channel states: HTTP {response.status_code}")
        result["failed"] += 1
        return result

    autofees_disabled = {
        str(channel.get("chan_id", ""))
        for channel in response.json().get("results", [])
        if not channel.get("auto_fees", False)
    }
    channels_to_update = [c for c in non_active_chan_ids if str(c) in autofees_disabled]

    for chan_id in channels_to_update:
        notes = "Status: ⛰️ Magma Channel Buy Order Expired"
        payload = {"chan_id": chan_id, "auto_fees": True, "notes": notes}
        if _put_lndg_channel(chan_id, payload):
            logging.info(f"Updated auto_fees for channel {chan_id}")
            result["updated"] += 1
        else:
            result["failed"] += 1
    return result


def update_notes_for_active_channels(active_channels_info: List[tuple]) -> Dict[str, int]:
    """Update notes in LNDg for active leased Magma channels. Returns updated/failed counts."""
    result = {"updated": 0, "failed": 0}

    for item in active_channels_info:
        try:
            chan_id, blocks_until_close, fee_cap, fee_grace_calc = item
        except (ValueError, TypeError):
            logging.error(f"Error unpacking item: {item}. Expected a 4-element tuple.")
            result["failed"] += 1
            continue

        if fee_grace_calc < 0:
            notes = f"Status: 🌋 Magma Channel Buy Order Active \n(Lease Expiration: {blocks_until_close} blocks). \nFee Cap: {fee_cap}. Proportional Fee Rate activated ✅"
        else:
            notes = f"Status: 🌋 Magma Channel Buy Order Active \n(Lease Expiration: {blocks_until_close} blocks). \nFee Cap: {fee_cap}. Proportional Fee Rate in: {fee_grace_calc}."

        payload = {"chan_id": chan_id, "auto_fees": False, "notes": notes}
        if _put_lndg_channel(chan_id, payload):
            logging.debug(f"Updated notes for channel {chan_id}")
            result["updated"] += 1
        else:
            result["failed"] += 1
    return result


def _put_lndg_channel(chan_id: str, payload: dict) -> bool:
    try:
        response = requests.put(lndg_channel_url(chan_id), json=payload, auth=(LNDG_USERNAME, LNDG_PASSWORD), timeout=10)
    except requests.exceptions.RequestException as e:
        logging.error(f"Error updating LNDg channel {chan_id}: {e}")
        return False
    if response.status_code != 200:
        logging.error(f"Failed to update LNDg channel {chan_id}: HTTP {response.status_code}")
        return False
    return True


def main() -> int:
    """Runs one lease sync. Returns 0 on full success, 1 on any failure."""
    try:
        active_info, non_active_ids, fee_groups = cluster_sold_channels()
    except AmbossAPIError as e:
        logging.error(f"Magma lease sync aborted; charge-lnd files and LNDg left unchanged: {e}")
        return 1
    except OSError as e:
        logging.error(f"Failed writing charge-lnd rule files; LNDg left unchanged: {e}")
        return 1

    autofees = update_autofees(non_active_ids)
    notes = update_notes_for_active_channels(active_info)
    logging.info(
        f"Magma lease sync complete: {len(active_info)} active leases across fee caps "
        f"{sorted(fee_groups)}, {len(non_active_ids)} finished; LNDg autofees "
        f"updated={autofees['updated']} failed={autofees['failed']}, notes "
        f"updated={notes['updated']} failed={notes['failed']}."
    )
    return 1 if autofees["failed"] or notes["failed"] else 0


if __name__ == "__main__":
    configure_logging()
    sys.exit(main())
