"""
Guidelines for the Fee Adjuster Script

This script automates the adjustment of channel fees based on various conditions and trends.
It uses data from the Amboss API to determine appropriate fee rates for each node.

Configuration Settings (see fee_adjuster_config_docs.txt for details):
- base_adjustment_percentage: Adjusts your local fee by this percentage. Applied to the selected fee base (e.g., median, mean).
- group_adjustment_percentage: Additional adjustment based on node groups. Allows for differentiated strategies.
- max_cap: Maximum fee rate allowed. Prevents fees from exceeding this value.
- trend_sensitivity: Determines how much trends influence fee adjustments. Higher values mean greater influence.
- fee_base: The statistical measure used as the base for fee calculations. Options include "median", "mean", "min", "max", "weighted" and "weighted_corrected".
- groups: Categories or tags for nodes. Used to apply specific strategies or adjustments.
- max_outbound: (Optional) Maximum outbound liquidity percentage (0.0 to 1.0).  Only applies if the channel's outbound liquidity is *below* this value.
- min_outbound: (Optional) Minimum outbound liquidity percentage (0.0 to 1.0). Only applies if the channel's outbound liquidity is *above* this value.
- fee_bands: (Optional) Adjusts fees based on local liquidity. Contains the following sub-settings:
  - enabled: Whether to use fee bands (true/false)
  - discount: Discount percentage (negative value) to apply when local balance is high (80-100%)
  - premium: Premium percentage (positive value) to apply when local balance is low (0-40%)
- stuck_channel_adjustment: (Optional) Adjusts fees for channels that haven't forwarded payments recently:
  - enabled: Whether to use stuck channel adjustment (true/false)
  - stuck_time_period: Number of days after which a channel is considered "stuck" (e.g., 7)


Groups and group_adjustment_percentage:
Nodes can belong to multiple groups, such as "sink" or "expensive". The group_adjustment_percentage is applied to nodes based on their group membership, allowing for tailored fee strategies. For example, nodes in the "expensive" group might have higher fees to reflect their role in the network.

Fee Bands:
The fee_bands feature provides dynamic fee adjustments based on channel liquidity. It divides liquidity into 5 bands:
- Band 0 (80-100% local balance): Maximum discount applied
- Band 1 (60-80% local balance): High discount applied
- Band 2 (40-60% local balance): Neutral adjustment
- Band 3 (20-40% local balance): Maximum premium applied
- Band 4 (0-20% local balance): Maximum premium applied (same as band 3)

Note: The premium is capped at band 3 (40% liquidity threshold) to ensure rebalancing can happen profitably
when channels reach very low liquidity levels. This prevents charging escalating fees when channels are
almost depleted, which would make automated rebalancing unprofitable.

Stuck Channel Adjustment:
The stuck_channel_adjustment feature gradually reduces fees for channels that haven't forwarded payments recently.
For each "stuck_time_period" (in days) that a channel goes without forwarding a payment, its fee band is
moved down by one level (toward the maximum discount). This makes inactive channels more attractive to use
and can help reactivate channels that haven't been routing payments.

For example, with a stuck_time_period of 7 days:
- No forwarding for 7+ days: Move down 1 fee band (e.g., from band 3 to band 2)
- No forwarding for 14+ days: Move down 2 fee bands
- No forwarding for 21+ days: Move down 3 fee bands
- No forwarding for 28+ days: Move down to band 0 (maximum discount)

Important: Stuck channel adjustment is automatically disabled when local liquidity is below 20%. This prevents
offering discounts on channels that are already heavily drained and likely need rebalancing instead of
further incentives to route outbound payments.

The script queries the LNDg API to determine the timestamp of the last forwarding through each channel.
If a channel has no forwarding history at all, it receives the maximum discount.

Usage:
- Configure nodes and their settings in the feeConfig.json file.
- Run the script to automatically adjust fees based on the configured settings and current trends.
- Currently, it requires a running LNDg instance to retrieve local channel details and fees

Charge-lnd Details:
# charge-lnd.config
[🤖 FeeAdjuster Import]
strategy = use_config
config_file = file:///home/chargelnd/charge-lnd/.config/fee_adjuster.txt

Installation:
crontab -e with
0 * * * * /path/Lightning-Python-Tools/.venv/bin/python3 /path/Lightning-Python-Tools/Other/fee_adjuster.py >/dev/null &1

Or run the systemd-installer install_fee_adjuster_service.sh
"""

import os
import requests
from datetime import datetime, timedelta
import configparser
import logging
import json
import time
from prettytable import PrettyTable


# Error classes
class AmbossAPIError(Exception):
    """Represents an error when interacting with the Amboss API."""

    def __init__(self, message, status_code=None, response_data=None):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data


class LNDGAPIError(Exception):
    """Represents an error when interacting with the LNDg API."""

    def __init__(self, message, status_code=None, response_data=None):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data


# Get the path to the parent directory
parent_dir = os.path.dirname(os.path.abspath(__file__))

# Construct the path to the config.ini file
config_file_path = os.path.join(parent_dir, "..", "config.ini")

# File path for the log file
log_file_path = os.path.join(parent_dir, "..", "logs", "fee-adjuster.log")

# Logfile definition
logging.basicConfig(filename=log_file_path, level=logging.DEBUG)


def load_config():
    config = configparser.ConfigParser()
    config.read(config_file_path)
    return config


def load_node_definitions():
    nodes_file_path = os.path.join(parent_dir, "..", "feeConfig.json")
    with open(nodes_file_path, "r") as f:
        node_definitions = json.load(f)
    return node_definitions


def fetch_amboss_data(
    pubkey, config, time_ranges=["TODAY", "ONE_DAY", "ONE_WEEK", "ONE_MONTH"]
):
    amboss_url = "https://api.amboss.space/graphql"
    headers = {
        "Authorization": f"Bearer {config['credentials']['amboss_authorization']}",
        "Content-Type": "application/json",
    }
    query = """
        query Fee_info($pubkey: String!, $timeRange: SnapshotTimeRangeEnum) {
            getNode(pubkey: $pubkey) {
                graph_info {
                    channels {
                        fee_info(timeRange: $timeRange) {
                            remote {
                                max
                                mean
                                median
                                weighted
                                weighted_corrected
                            }
                        }
                    }
                }
            }
        }
    """
    all_fee_data = {}
    for time_range in time_ranges:
        variables = {"pubkey": pubkey, "timeRange": time_range}
        payload = {"query": query, "variables": variables}
        try:
            response = requests.post(amboss_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
            logging.debug(
                f"Raw Amboss API response for {time_range}: {json.dumps(data)}"
            )
            if data.get("errors"):
                logging.error(f"Amboss API error for {time_range}: {data['errors']}")
                raise AmbossAPIError(
                    f"Amboss API error for {time_range}: {data['errors']}"
                )
            channels = data["data"]["getNode"]["graph_info"]["channels"]
            if channels:
                fee_info = channels["fee_info"]["remote"]
                all_fee_data[time_range] = fee_info
            else:
                logging.warning(
                    f"No channels found for pubkey {pubkey} in time range {time_range}"
                )
                all_fee_data[time_range] = {}
        except requests.exceptions.RequestException as e:
            logging.error(f"Error fetching Amboss data for {time_range}: {e}")
            raise AmbossAPIError(f"Error fetching Amboss data for {time_range}: {e}")
    return all_fee_data


def analyze_fee_trends(all_fee_data, fee_base):
    one_day_value = float(all_fee_data.get("ONE_DAY", {}).get(fee_base, 0))
    one_week_value = float(all_fee_data.get("ONE_WEEK", {}).get(fee_base, 0))
    one_month_value = float(all_fee_data.get("ONE_MONTH", {}).get(fee_base, 0))

    # Simple trend analysis, go bonkers if you like
    if one_day_value > one_week_value > one_month_value:
        return 0.05  # Fees are consistently increasing, add 5%
    elif one_day_value < one_week_value < one_month_value:
        return -0.05  # Fees are consistently decreasing, subtract 5%
    else:
        return 0  # Fees are stable or mixed trends


def get_last_forwarding_timestamp(chan_id, config):
    """
    Fetch the last forwarding timestamp for a specific channel from LNDg API.

    Args:
        chan_id: The channel ID to check
        config: Configuration containing LNDg API credentials

    Returns:
        A datetime object of the last forwarding time, or None if no forwarding found
    """
    lndg_api_url = config["lndg"]["lndg_api_url"]
    username = config["credentials"]["lndg_username"]
    password = config["credentials"]["lndg_password"]

    # Query for the most recent forwarding event through this channel (limit=1)
    api_url = f"{lndg_api_url}/api/forwards/?limit=1&chan_id_out={chan_id}"
    try:
        response = requests.get(api_url, auth=(username, password))
        response.raise_for_status()
        data = response.json()

        if data.get("results") and len(data["results"]) > 0:
            # Get the timestamp from the most recent forwarding event
            forward_date_str = data["results"][0]["forward_date"]
            return datetime.strptime(forward_date_str, "%Y-%m-%dT%H:%M:%S")

        # Also check if this channel was an inbound channel for forwarding
        api_url = f"{lndg_api_url}/api/forwards/?limit=1&chan_id_in={chan_id}"
        response = requests.get(api_url, auth=(username, password))
        response.raise_for_status()
        data = response.json()

        if data.get("results") and len(data["results"]) > 0:
            # Get the timestamp from the most recent forwarding event
            forward_date_str = data["results"][0]["forward_date"]
            return datetime.strptime(forward_date_str, "%Y-%m-%dT%H:%M:%S")

        # No forwarding events found
        return None
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching forwarding history for channel {chan_id}: {e}")
        return None


def get_last_forwarding_timestamp_for_peer(channel_ids, config):
    """
    Find the most recent forwarding timestamp across all channels to a peer.

    Args:
        channel_ids: List of channel IDs to check
        config: Configuration containing LNDg API credentials

    Returns:
        A datetime object of the most recent forwarding time across all channels,
        or None if no forwarding found in any channel
    """
    most_recent_forward = None

    for chan_id in channel_ids:
        # Get forwarding timestamp for this channel
        channel_forward_time = get_last_forwarding_timestamp(chan_id, config)

        # Update most recent time if this channel has more recent activity
        if channel_forward_time is not None:
            if (
                most_recent_forward is None
                or channel_forward_time > most_recent_forward
            ):
                most_recent_forward = channel_forward_time
                logging.debug(
                    f"Channel {chan_id} has more recent forwarding: {most_recent_forward}"
                )

    return most_recent_forward


def calculate_stuck_channel_band_adjustment(
    fee_conditions, channel_data, chan_id, config
):
    """
    Calculate the band adjustment for stuck channels that haven't forwarded payments recently.

    Args:
        fee_conditions: Dictionary containing stuck channel settings
        channel_data: Data about the channel
        chan_id: Channel ID
        config: Configuration containing LNDg API credentials

    Returns:
        Number of bands to move down (0 if not stuck or adjustment disabled)
    """
    # Check if stuck channel adjustment is enabled
    stuck_settings = fee_conditions.get("stuck_channel_adjustment", {})
    if not stuck_settings.get("enabled", False):
        return 0  # No adjustment

    # Skip stuck channel adjustment if local liquidity is too low (below 20%)
    # This prevents reducing fees further when the channel is already heavily drained
    capacity = channel_data.get("capacity", 0)
    local_balance = channel_data.get("local_balance", 0)

    if capacity > 0:
        local_ratio = local_balance / capacity
        if local_ratio < 0.2:  # Less than 20% local liquidity
            return 0  # Skip adjustment

    # Get the stuck time period in days
    stuck_time_period = stuck_settings.get("stuck_time_period", 7)

    # Get the last forwarding timestamp
    last_forward_time = get_last_forwarding_timestamp(chan_id, config)

    if last_forward_time is None:
        # If no forwarding history found, consider it fully stuck
        # This will move it to maximum discount
        return 4  # Maximum band adjustment (to band 0)

    # Calculate days since last forwarding
    current_time = datetime.now()
    days_since_last_forward = (current_time - last_forward_time).days

    # Calculate how many stuck periods have passed
    stuck_periods = days_since_last_forward // stuck_time_period

    # Cap at 4 band adjustments
    return min(stuck_periods, 4)


def calculate_fee_band_adjustment(
    fee_conditions, outbound_ratio, stuck_band_adjustment=0
):
    """
    Calculate fee adjustment based on outbound liquidity ratio bands.

    Divides the outbound liquidity into 5 percentile bands and applies a
    graduated adjustment between the discount (high local balance) and
    premium (low local balance).

    Bands:
    - Band 0 (80-100% local): max discount
    - Band 1 (60-80% local): partial discount
    - Band 2 (40-60% local): neutral adjustment
    - Band 3 (20-40% local): max premium (capped)
    - Band 4 (0-20% local): max premium (same as band 3)

    Args:
        fee_conditions: Dictionary containing fee band settings
        outbound_ratio: Ratio of local balance to total capacity (0.0 to 1.0)
        stuck_band_adjustment: Number of bands to move down due to stuck channel (0-4)

    Returns:
        Adjustment factor to be applied to the fee (multiplicative)
    """
    # Check if fee bands are enabled
    if not fee_conditions.get("fee_bands", {}).get("enabled", False):
        return 1.0  # No adjustment

    # Get fee band parameters
    fee_bands = fee_conditions.get("fee_bands", {})
    discount = fee_bands.get("discount", 0)
    premium = fee_bands.get("premium", 0)

    # Calculate the range between discount and premium
    adjustment_range = premium - discount

    # Calculate which band the current outbound ratio falls into (5 bands)
    raw_band = min(4, max(0, int((1 - outbound_ratio) * 5)))

    # Apply stuck channel adjustment (move down bands)
    raw_band = max(0, raw_band - stuck_band_adjustment)

    # Map bands 3 and 4 to have the same premium level
    # This ensures 0-20% and 20-40% liquidity have the same fee
    effective_band = min(
        3, raw_band
    )  # Cap at band 3 (premium maxes out at 40% liquidity)

    # Calculate the adjustment as a percentage between discount and premium
    # Now using effective_band instead of raw_band for adjustment calculation
    adjustment = discount + (effective_band / 3) * adjustment_range

    # Return the multiplicative factor
    return 1 + adjustment, raw_band


def calculate_new_fee_rate(
    amboss_data,
    fee_conditions,
    trend_factor,
    base_adjustment_percentage,
    group_adjustment_percentage,
    outbound_ratio=None,  # Add parameter for outbound liquidity ratio
):
    fee_base = fee_conditions.get("fee_base", "median")
    # Ensure the selected fee base is converted to a float
    base_fee = float(amboss_data.get("TODAY", {}).get(fee_base, 0))
    max_cap = fee_conditions.get("max_cap", 1000)
    trend_sensitivity = fee_conditions.get("trend_sensitivity", 1)

    adjusted_base_percentage = base_adjustment_percentage + (
        trend_factor * trend_sensitivity
    )

    # Calculate basic fee rate with trend and group adjustments
    basic_fee_rate = (
        base_fee * (1 + adjusted_base_percentage) * (1 + group_adjustment_percentage)
    )

    # Apply fee band adjustment if outbound_ratio is provided
    if outbound_ratio is not None:
        fee_band_result = calculate_fee_band_adjustment(fee_conditions, outbound_ratio)
        if isinstance(fee_band_result, tuple):
            fee_band_factor, _ = fee_band_result
        else:
            fee_band_factor = fee_band_result
        basic_fee_rate = basic_fee_rate * fee_band_factor

    # Cap at max and round
    new_fee_rate = min(basic_fee_rate, max_cap)
    return round(new_fee_rate)


# Need to fetch from LNDg since lncli listchannels doesn't provide local_fee
# and want to avoid two lncli subprocesses per pubkey
def get_channels_to_modify(pubkey, config):
    lndg_api_url = config["lndg"]["lndg_api_url"]
    api_url = f"{lndg_api_url}/api/channels?limit=1500"
    username = config["credentials"]["lndg_username"]
    password = config["credentials"]["lndg_password"]
    channels_to_modify = {}
    try:
        response = requests.get(api_url, auth=(username, password))
        response.raise_for_status()
        data = response.json()
        if "results" in data:
            results = data["results"]
            for result in results:
                remote_pubkey = result.get("remote_pubkey", "")
                if remote_pubkey == pubkey:
                    chan_id = result.get("chan_id", "")
                    local_fee_rate = result.get("local_fee_rate", 0)
                    is_open = result.get("is_open", False)
                    alias = result.get("alias", "")
                    capacity = result.get("capacity", 0)
                    local_balance = result.get("local_balance", 0)
                    fees_updated = result.get("fees_updated", "")
                    auto_fees = result.get("auto_fees", False)

                    if is_open:
                        local_balance_ratio = (
                            (local_balance / capacity) * 100 if capacity else 0
                        )
                        fees_updated_datetime = (
                            datetime.strptime(fees_updated, "%Y-%m-%dT%H:%M:%S.%f")
                            if fees_updated
                            else None
                        )
                        channels_to_modify[chan_id] = {
                            "alias": alias,
                            "capacity": capacity,
                            "local_balance": local_balance,
                            "local_balance_ratio": local_balance_ratio,
                            "fees_updated_datetime": fees_updated_datetime,
                            "local_fee_rate": local_fee_rate,
                            "auto_fees": auto_fees,
                        }
        return channels_to_modify
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching LNDg channels: {e}")
        raise LNDGAPIError(f"Error fetching LNDg channels: {e}")


# Write to LNDg
def update_lndg_fee(chan_id, new_fee_rate, channel_data, config):
    lndg_api_url = config["lndg"]["lndg_api_url"]
    username = config["credentials"]["lndg_username"]
    password = config["credentials"]["lndg_password"]
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # First, update auto_fees if needed
    if channel_data["auto_fees"]:
        auto_fees_url = f"{lndg_api_url}/api/channels/{chan_id}/"
        auto_fees_payload = {"chan_id": chan_id, "auto_fees": False}
        try:
            response = requests.put(
                auto_fees_url, json=auto_fees_payload, auth=(username, password)
            )
            response.raise_for_status()
            logging.info(f"{timestamp}: Disabled auto_fees for channel {chan_id}")
        except requests.exceptions.RequestException as e:
            logging.error(f"Error updating auto_fees for channel {chan_id}: {e}")
            raise LNDGAPIError(f"Error updating auto_fees for channel {chan_id}: {e}")

    # Then, update the fee rate
    fee_update_url = f"{lndg_api_url}/api/chanpolicy/"
    fee_payload = {"chan_id": chan_id, "fee_rate": new_fee_rate}
    try:
        response = requests.post(
            fee_update_url, json=fee_payload, auth=(username, password)
        )
        response.raise_for_status()
        logging.info(
            f"{timestamp}: API confirmed changing local fee to {new_fee_rate} for channel {chan_id}"
        )
    except requests.exceptions.RequestException as e:
        logging.error(f"Error updating LNDg fee for channel {chan_id}: {e}")
        raise LNDGAPIError(f"Error updating LNDg fee for channel {chan_id}: {e}")


def write_charge_lnd_file(
    file_path, pubkey, alias, new_fee_rate, is_aggregated
):  # Added is_aggregated
    with open(file_path, "a") as f:
        f.write(
            f"[🤖 {alias}{' (Aggregated)' if is_aggregated else ''}]\n"
        )  # Add note to alias comment
        f.write(f"node.id = {pubkey}\n")
        f.write("strategy = static\n")
        f.write(f"fee_ppm = {new_fee_rate}\n")
        f.write(
            "min_htlc_msat = 1_000\n"
        )  # Consider if these should be configurable per peer
        f.write(
            "max_htlc_msat_ratio = 0.9\n"
        )  # Consider if these should be configurable per peer
        f.write("\n")


def update_channel_notes(
    chan_id,
    alias,
    group_name,
    fee_base,
    fee_conditions,
    outbound_ratio,
    raw_band,
    fee_band_factor,
    config,
    node_definitions,
    new_fee_rate=None,  # Add as optional parameter with default None
    stuck_band_adjustment=0,  # Add with default value 0
):
    """Update the channel notes in LNDg with fee adjuster information."""
    update_channel_notes_enabled = node_definitions.get("update_channel_notes", False)

    if not update_channel_notes_enabled:
        logging.info(f"Channel notes updates are disabled in config")
        return

    lndg_api_url = config["lndg"]["lndg_api_url"]
    username = config["credentials"]["lndg_username"]
    password = config["credentials"]["lndg_password"]

    # Log the API URL we're using
    logging.debug(f"LNDg API URL for note updates: {lndg_api_url}")

    # Build the notes text
    notes = f"🔋 Group: {group_name if group_name else 'None'} | Base: {fee_base}"

    # Add fee bands info in a condensed format
    fee_bands = fee_conditions.get("fee_bands", {})
    if fee_bands.get("enabled", False):
        discount = fee_bands.get("discount", 0)
        premium = fee_bands.get("premium", 0)

        band_names = [
            "Max Discount",
            "High Discount",
            "Neutral",
            "Max Premium",
            "Max Premium",
        ]
        effective_band = min(3, raw_band)
        actual_adjustment = discount + (effective_band / 3) * (premium - discount)

        notes += f"\nCurrent: {band_names[raw_band]} | Adjustment: {actual_adjustment*100:.1f}%"
        notes += f"\nBands: ✅ | Disc: {discount*100:.0f}% | Prem: {premium*100:.0f}% | Bal: {outbound_ratio*100:.0f}%"
    else:
        notes += f"\nFee Bands: Disabled"

    # Add fee rate info if available
    if new_fee_rate is not None:  # Changed to check parameter instead of locals()
        notes += f"\nCurrent Rate: {new_fee_rate} ppm"

    # Add stuck channel info if applicable
    stuck_settings = fee_conditions.get("stuck_channel_adjustment", {})
    if stuck_settings.get("enabled", False):
        stuck_period = stuck_settings.get("stuck_time_period", 7)
        notes += f"\nStuck adjust: {'✅' if stuck_band_adjustment > 0 else '❌'} | Period: {stuck_period}d"

    # Log the notes we're going to send
    logging.debug(f"Channel {chan_id} notes to update: {notes}")

    # Update the channel notes
    payload = {"chan_id": chan_id, "notes": notes}
    try:
        put_url = f"{lndg_api_url}/api/channels/{chan_id}/"
        logging.debug(f"Sending PUT request to: {put_url}")
        logging.debug(f"With payload: {json.dumps(payload)}")

        response = requests.put(put_url, json=payload, auth=(username, password))
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if response.status_code == 200:
            logging.info(
                f"{timestamp}: Successfully updated notes for channel {chan_id}"
            )
        else:
            logging.error(
                f"{timestamp}: Failed to update notes for channel {chan_id}: Status Code {response.status_code}, Response: {response.text}"
            )

    except Exception as e:
        logging.error(f"Error updating notes for channel {chan_id}: {e}")


def print_fee_adjustment(
    alias,
    pubkey,
    channel_ids_list,  # Changed from chan_id
    capacity,  # Now potentially aggregate
    local_balance,  # Now potentially aggregate
    old_fee_rate,  # Display purpose (e.g., from first channel)
    new_fee_rate,
    group_name,
    fee_base,
    fee_conditions,
    trend_factor,
    amboss_data,
    is_aggregated,  # New flag
    stuck_band_adjustment=0,  # Stuck channel adjustment
    raw_band=None,  # Raw band after adjustment
):
    print("-" * 80)
    print(f"Alias: {alias}{' (Aggregated)' if is_aggregated else ''}")
    print(f"Pubkey: {pubkey}")
    if is_aggregated:
        print(f"Channel IDs: {', '.join(channel_ids_list)}")
        print(f"Aggregate Capacity: {capacity}")
        print(
            f"Aggregate Local Balance: {local_balance} | (Outbound: {round(local_balance/capacity*100) if capacity else 0}%)"
        )
    else:
        print(f"Channel ID: {channel_ids_list[0]}")  # Only one ID in the list
        print(f"Capacity: {capacity}")
        print(
            f"Local Balance: {local_balance} | (Outbound: {round(local_balance/capacity*100) if capacity else 0}%)"
        )
    # Display old/new rate - Note: Actual updates depended on individual deltas
    print(f"Old Fee Rate (Example): {old_fee_rate}")
    print(f"New Fee Rate (Applied): {new_fee_rate}")
    # Delta display might be less meaningful in aggregate view, consider removing or clarifying
    # print(f"Delta: {new_fee_rate - old_fee_rate}")
    if group_name:
        print(f"Group: {group_name}")
    else:
        print(f"Group: No Group")
    print(f"Chosen Fee Base: {fee_base}")

    # Add fee bands information if enabled
    fee_bands = fee_conditions.get("fee_bands", {})
    if fee_bands.get("enabled", False):
        outbound_ratio = local_balance / capacity if capacity else 0
        if raw_band is None:
            raw_band = min(4, max(0, int((1 - outbound_ratio) * 5)))

        band_names = [
            "Max Discount (80-100%)",
            "High Discount (60-80%)",
            "Neutral (40-60%)",
            "Max Premium (20-40%)",
            "Max Premium (0-20%)",  # Same premium as band 3
        ]

        # Calculate the actual percentage adjustment for this band
        discount = fee_bands.get("discount", 0)
        premium = fee_bands.get("premium", 0)
        adjustment_range = premium - discount

        # For display purposes, cap at band 3 since bands 3 and 4 use the same adjustment
        effective_band = min(3, raw_band)
        actual_adjustment = discount + (effective_band / 3) * adjustment_range

        print(f"Fee Bands: Enabled")
        print(
            f"  - Discount: {fee_bands.get('discount', 0)*100:.1f}%, Premium: {fee_bands.get('premium', 0)*100:.1f}%"
        )
        print(
            f"  - Current Band: {band_names[raw_band]} (Local Balance: {outbound_ratio*100:.1f}%)"
        )
        print(f"  - Applied Adjustment: {actual_adjustment*100:.1f}%")

        if raw_band == 4:
            print(f"  - Note: Premium capped at 40% liquidity level")
    else:
        print(f"Fee Bands: Disabled")

    # Add stuck channel information if adjustment was applied
    stuck_settings = fee_conditions.get("stuck_channel_adjustment", {})
    if stuck_settings.get("enabled", False):
        stuck_time_period = stuck_settings.get("stuck_time_period", 7)

        # Check if we're in the low-liquidity band where stuck adjustment is skipped
        outbound_ratio = local_balance / capacity if capacity else 0
        is_low_liquidity = outbound_ratio < 0.2

        if is_low_liquidity:
            print(
                f"Stuck Channel Adjustment: Skipped (Low Local Liquidity: {outbound_ratio*100:.1f}%)"
            )
            print(
                f"  - Note: Stuck channel adjustment is disabled when local liquidity is below 20%"
            )
        elif stuck_band_adjustment > 0:
            print(f"Stuck Channel Adjustment: Applied")
            print(f"  - Bands Adjusted Down: {stuck_band_adjustment}")
            print(f"  - Stuck Time Period: {stuck_time_period} days")
            if raw_band is not None:
                original_band = min(4, raw_band + stuck_band_adjustment)
                if original_band < 5:  # Check to avoid index error
                    # Calculate what the adjustment percentage would have been without stuck adjustment
                    original_effective_band = min(3, original_band)
                    original_adjustment = (
                        discount + (original_effective_band / 3) * adjustment_range
                    )

                    print(
                        f"  - Original Band (Before Adjustment): {band_names[original_band]} ({original_adjustment*100:.1f}%)"
                    )
        else:
            print(
                f"Stuck Channel Adjustment: None (Channel Active in the past {stuck_time_period} days)"
            )

    print(f"Fee Conditions: {json.dumps(fee_conditions)}")
    print(f"Trend Factor: {trend_factor}")

    # Amboss data table remains the same
    table = PrettyTable()
    table.field_names = [
        "Time Range",
        "Max",
        "Mean",
        "Median",
        "Weighted",
        "Weighted Corrected",
    ]

    for time_range, fee_info in amboss_data.items():
        table.add_row(
            [
                time_range,
                fee_info.get("max", "N/A"),
                (
                    round(float(fee_info.get("mean", 0)), 1)
                    if fee_info.get("mean")
                    else "N/A"
                ),
                fee_info.get("median", "N/A"),
                (
                    round(float(fee_info.get("weighted", 0)), 1)
                    if fee_info.get("weighted")
                    else "N/A"
                ),
                (
                    round(float(fee_info.get("weighted_corrected", 0)), 1)
                    if fee_info.get("weighted_corrected")
                    else "N/A"
                ),
            ]
        )
    print(table)


def main():
    try:
        config = load_config()
        node_definitions = load_node_definitions()
        groups = node_definitions.get("groups", {})
        write_charge_lnd_file_enabled = node_definitions.get(
            "write_charge_lnd_file", False
        )
        lndg_fee_update_enabled = node_definitions.get("LNDg_fee_update", False)
        terminal_output_enabled = node_definitions.get("Terminal_output", False)

        if write_charge_lnd_file_enabled:
            charge_lnd_file_path = os.path.join(
                config["paths"]["charge_lnd_path"], "fee_adjuster.txt"
            )
            # Open the file in write mode to clear any existing content
            with open(charge_lnd_file_path, "w") as f:
                pass

        for node in node_definitions["nodes"]:
            pubkey = node["pubkey"]
            group_name = node.get("group")
            fee_conditions = None  # Initialize fee_conditions to None
            base_adjustment_percentage = 0  # Initialize base_adjustment_percentage

            if "fee_conditions" in node:
                fee_conditions = node["fee_conditions"]
                base_adjustment_percentage = fee_conditions.get(
                    "base_adjustment_percentage", 0
                )

            group_adjustment_percentage = 0  # Initialize group_adjustment_percentage
            if group_name and group_name in groups:
                group_fee_conditions = groups[group_name]
                group_adjustment_percentage = group_fee_conditions.get(
                    "group_adjustment_percentage", 0
                )
                if not fee_conditions:
                    fee_conditions = group_fee_conditions
            elif not fee_conditions:
                logging.warning(
                    f"No fee conditions found for pubkey {pubkey}. Skipping fee adjustment."
                )
                continue

            fee_base = fee_conditions.get("fee_base", "median")
            fee_delta_threshold = fee_conditions.get("fee_delta_threshold", 20)
            try:
                all_amboss_data = fetch_amboss_data(pubkey, config)
                # print(f"Amboss Data: {all_amboss_data}")  # Debug Amboss Fetcher
                if not all_amboss_data:
                    logging.warning(
                        f"No Amboss data found for pubkey {pubkey}. Skipping fee adjustment."
                    )
                    continue  # Skip to the next node
                trend_factor = analyze_fee_trends(all_amboss_data, fee_base)
                channels_to_modify = get_channels_to_modify(pubkey, config)
                num_channels = len(channels_to_modify)

                if not channels_to_modify:
                    logging.info(
                        f"No open channels found for pubkey {pubkey}. Skipping."
                    )
                    continue

                # --- Aggregate Liquidity Calculation (if multiple channels) ---
                total_capacity = 0
                total_local_balance = 0
                aggregate_local_balance_ratio = 0
                first_channel_data = next(
                    iter(channels_to_modify.values())
                )  # Get data from one channel for later use
                first_chan_id = next(
                    iter(channels_to_modify.keys())
                )  # Get first channel ID

                if num_channels > 1:
                    logging.info(
                        f"Found {num_channels} channels for peer {pubkey}. Aggregating liquidity."
                    )
                    for chan_data in channels_to_modify.values():
                        total_capacity += chan_data["capacity"]
                        total_local_balance += chan_data["local_balance"]
                    aggregate_local_balance_ratio = (
                        (total_local_balance / total_capacity) * 100
                        if total_capacity
                        else 0
                    )
                    check_ratio = (
                        aggregate_local_balance_ratio / 100
                    )  # Use aggregate ratio for check
                else:
                    # Use individual channel's ratio if only one channel
                    total_capacity = first_channel_data["capacity"]
                    total_local_balance = first_channel_data["local_balance"]
                    check_ratio = first_channel_data["local_balance_ratio"] / 100

                # --- Outbound Liquidity Check (using aggregate or individual ratio) ---
                max_outbound = fee_conditions.get("max_outbound", 1.0)
                min_outbound = fee_conditions.get("min_outbound", 0.0)

                if not (check_ratio <= max_outbound and check_ratio >= min_outbound):
                    alias_to_log = first_channel_data[
                        "alias"
                    ]  # Use first alias for logging consistency
                    logging.info(
                        f"Peer {pubkey} (Alias: {alias_to_log}, {num_channels} channels) skipped due to outbound liquidity conditions. "
                        f"Ratio: {check_ratio:.2f}, Max: {max_outbound}, Min: {min_outbound}"
                    )
                    if terminal_output_enabled:
                        print(
                            f"Peer {pubkey} (Alias: {alias_to_log}, {num_channels} channels) skipped due to outbound liquidity conditions."
                        )
                        print(
                            f"Aggregate Ratio: {check_ratio:.2f}, Max: {max_outbound}, Min: {min_outbound}"
                        )
                    continue  # Skip this entire peer

                # Check for stuck channels and calculate adjustment
                # Calculate once per peer rather than per channel
                least_stuck_band_adjustment = 0  # Default to no adjustment

                if fee_conditions.get("stuck_channel_adjustment", {}).get(
                    "enabled", False
                ):
                    # Get all channel IDs for this peer
                    peer_channel_ids = list(channels_to_modify.keys())

                    # Get the most recent forwarding across all channels to this peer
                    last_forward_time = get_last_forwarding_timestamp_for_peer(
                        peer_channel_ids, config
                    )

                    # Calculate stuck adjustment based on most recent forwarding
                    if last_forward_time is None:
                        # If no forwarding history found in any channel, consider it fully stuck
                        least_stuck_band_adjustment = (
                            4  # Maximum band adjustment (to band 0)
                        )
                        logging.info(
                            f"Peer {pubkey} has no forwarding history, applying maximum stuck adjustment"
                        )
                    else:
                        # Calculate days since last forwarding for this peer
                        stuck_time_period = fee_conditions.get(
                            "stuck_channel_adjustment", {}
                        ).get("stuck_time_period", 7)
                        current_time = datetime.now()
                        days_since_last_forward = (
                            current_time - last_forward_time
                        ).days

                        # Calculate how many stuck periods have passed
                        stuck_periods = days_since_last_forward // stuck_time_period

                        # Cap at 4 band adjustments
                        least_stuck_band_adjustment = min(stuck_periods, 4)

                        if least_stuck_band_adjustment > 0:
                            logging.info(
                                f"Peer {pubkey} last forwarded {days_since_last_forward} days ago, applying stuck adjustment: {least_stuck_band_adjustment}"
                            )

                # Calculate new fee rate with liquidity-based fee band adjustment and stuck channel adjustment
                fee_band_result = calculate_fee_band_adjustment(
                    fee_conditions,
                    check_ratio,
                    least_stuck_band_adjustment,  # Apply the stuck channel adjustment
                )
                if isinstance(fee_band_result, tuple):
                    fee_band_factor, raw_band = fee_band_result
                else:
                    fee_band_factor = fee_band_result
                    raw_band = min(
                        4, max(0, int((1 - check_ratio) * 5))
                    )  # Calculate raw_band if not returned

                # Calculate the final fee rate
                new_fee_rate = calculate_new_fee_rate(
                    all_amboss_data,
                    fee_conditions,
                    trend_factor,
                    base_adjustment_percentage,
                    group_adjustment_percentage,
                    check_ratio,  # Pass the outbound ratio for fee band calculation
                )

                # --- Apply updates to individual channels ---
                updated_any_channel = False
                channel_ids_list = list(channels_to_modify.keys())

                for chan_id, channel_data in channels_to_modify.items():
                    fee_delta = abs(new_fee_rate - channel_data["local_fee_rate"])

                    if lndg_fee_update_enabled and fee_delta > fee_delta_threshold:
                        try:
                            update_lndg_fee(chan_id, new_fee_rate, channel_data, config)
                            updated_any_channel = True
                        except LNDGAPIError as api_err:
                            logging.error(
                                f"Failed to update LNDg for {chan_id}: {api_err}"
                            )
                            # Decide if you want to continue with other channels or stop for this peer
                            continue  # Continue with the next channel for this peer

                # --- Update LNDg Channel Notes so a mouseover in the GUI gives quick fee-update synopsis
                update_channel_notes_enabled = node_definitions.get(
                    "update_channel_notes", False
                )
                if update_channel_notes_enabled:
                    logging.info(
                        f"Updating channel notes for {len(channels_to_modify)} channels of peer {pubkey}"
                    )
                    for chan_id, channel_data in channels_to_modify.items():

                        # Update the channel notes
                        update_channel_notes(
                            chan_id,
                            channel_data["alias"],
                            group_name,
                            fee_base,
                            fee_conditions,
                            check_ratio,  # Use aggregate ratio instead of individual channel ratio
                            raw_band,  # Use the already calculated raw band
                            fee_band_factor,
                            config,
                            node_definitions,  # Pass node_definitions as well
                            new_fee_rate,
                            least_stuck_band_adjustment,
                        )

                # --- Output and File Writing (once per peer if any channel was updated or terminal output enabled) ---
                if updated_any_channel or terminal_output_enabled:
                    if terminal_output_enabled:
                        # Use the modified print function
                        print_fee_adjustment(
                            first_channel_data["alias"],  # Use first alias
                            pubkey,
                            channel_ids_list,  # Pass list of all chan IDs
                            total_capacity,  # Pass aggregate capacity
                            total_local_balance,  # Pass aggregate balance
                            # Pass old/new rates from the first channel for display consistency,
                            # actual updates depend on individual deltas.
                            first_channel_data["local_fee_rate"],
                            new_fee_rate,
                            group_name,
                            fee_base,
                            fee_conditions,
                            trend_factor,
                            all_amboss_data,
                            num_channels > 1,  # Flag if aggregated
                            least_stuck_band_adjustment,  # Pass stuck band adjustment
                            raw_band,  # Pass the raw band after adjustment
                        )

                # Write to charge-lnd file once per peer if enabled and any channel updated
                if write_charge_lnd_file_enabled and updated_any_channel:
                    charge_lnd_file_path = os.path.join(
                        config["paths"]["charge_lnd_path"], "fee_adjuster.txt"
                    )
                    # Use modified write function
                    write_charge_lnd_file(
                        charge_lnd_file_path,
                        pubkey,
                        first_channel_data["alias"],  # Use first alias
                        new_fee_rate,
                        num_channels > 1,  # Flag if aggregated
                    )

            except (AmbossAPIError, LNDGAPIError) as e:
                logging.error(f"Error processing node {pubkey}: {e}")
                continue
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
        time.sleep(60)  # Sleep for 1 minute before retrying


if __name__ == "__main__":
    main()
