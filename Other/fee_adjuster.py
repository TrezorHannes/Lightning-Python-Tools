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
- max_outbound: (Optional) Maximum outbound liquidity percentage (0.0 to 1.0). Only applies if the channel's outbound liquidity is *below* this value.
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

Command Line Arguments:
- --debug: Enable detailed debug output, including stuck channel check results for each peer.

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
import sys
import requests
from datetime import datetime, timedelta
import configparser
import logging
import json
import time
import argparse
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


def check_recent_outbound_forwarding(pubkey, chan_id, stuck_time_period, config):
    """
    Checks for recent outbound forwarding for a single channel.

    Returns:
        dict: {'status': 'active'|'stuck'|'error', 'reason': str,
               'checked_chan_id': chan_id, 'api_url': str, 'api_response_data': dict|str}
    """
    lndg_api_url = config["lndg"]["lndg_api_url"]
    username = config["credentials"]["lndg_username"]
    password = config["credentials"]["lndg_password"]
    cutoff_date = datetime.now() - timedelta(days=stuck_time_period)
    cutoff_date_str = cutoff_date.strftime("%Y-%m-%dT%H:%M:%S")
    api_url = f"{lndg_api_url}/api/forwards/?limit=1&chan_in_or_out={chan_id}&forward_date__gt={cutoff_date_str}"
    result = {
        "status": "stuck",  # Default to stuck
        "reason": "unknown",
        "checked_chan_id": chan_id,
        "api_url": api_url,
        "api_response_data": None,
    }

    try:
        response = requests.get(api_url, auth=(username, password))
        response.raise_for_status()
        data = response.json()
        result["api_response_data"] = data  # Store response data

        if data.get("results") and len(data["results"]) > 0:
            forward_event = data["results"][0]
            if str(forward_event.get("chan_id_out")) == str(chan_id):
                result["status"] = "active"
                result["reason"] = (
                    f"outbound_found ({forward_event.get('forward_date', 'N/A')})"
                )
            elif str(forward_event.get("chan_id_in")) == str(chan_id):
                result["status"] = "stuck"  # Still stuck for outbound purposes
                result["reason"] = (
                    f"inbound_found ({forward_event.get('forward_date', 'N/A')})"
                )
            else:
                result["status"] = "stuck"  # Should not happen with filter
                result["reason"] = "match_error"
        else:
            result["status"] = "stuck"
            result["reason"] = "none_found"

    except requests.exceptions.RequestException as e:
        logging.error(f"API Error checking fwd for {chan_id}: {e}")
        result["status"] = "error"
        result["reason"] = f"api_error: {e}"
        result["api_response_data"] = str(e)
    except Exception as e:
        logging.error(f"Unexpected error checking fwd for {chan_id}: {e}")
        result["status"] = "error"
        result["reason"] = f"unexpected_error: {e}"
        result["api_response_data"] = str(e)

    return result


def get_last_forwarding_timestamp_for_peer(
    pubkey, channel_ids, stuck_time_period, config
):
    """
    Checks peer stuck status based on outbound forwards in any channel.

    Returns:
        tuple: (overall_status: 'active'|'stuck'|'error', channel_details: list[dict])
               overall_status is 'error' if any channel check resulted in error.
    """
    channel_details = []
    overall_status = "stuck"  # Default to stuck
    has_error = False

    for chan_id in channel_ids:
        check_result = check_recent_outbound_forwarding(
            pubkey, chan_id, stuck_time_period, config
        )
        channel_details.append(check_result)
        if check_result["status"] == "active":
            overall_status = (
                "active"  # Finding one active channel makes the peer active
            )
        elif check_result["status"] == "error":
            has_error = True

    # If we found an active channel, status is active, otherwise it remains stuck,
    # unless an error occurred during checks.
    if has_error:
        overall_status = "error"
    elif overall_status == "stuck":
        # If no channel was active and no errors, the peer is definitively stuck
        pass  # overall_status is already 'stuck'
    # else: overall_status is 'active'

    return overall_status, channel_details


def calculate_stuck_channel_band_adjustment(
    fee_conditions, channel_data, chan_id, config, is_peer_stuck
):
    """
    Calculate the band adjustment based on whether the peer is considered stuck.

    Args:
        fee_conditions: Dictionary containing stuck channel settings
        channel_data: Data about the channel (used for liquidity check)
        chan_id: Channel ID (for logging/context)
        config: Configuration containing LNDg API credentials
        is_peer_stuck: Boolean indicating if the peer (any channel) is stuck

    Returns:
        Number of bands to move down (typically 4 if stuck, 0 otherwise)
    """
    # Check if stuck channel adjustment is enabled globally in the config
    stuck_settings = fee_conditions.get("stuck_channel_adjustment", {})
    if not stuck_settings.get("enabled", False):
        print(f"DEBUG: Stuck adjustment disabled for chan_id={chan_id}")
        return 0  # No adjustment if disabled

    # Skip stuck channel adjustment if local liquidity is too low (below 20%)
    capacity = channel_data.get("capacity", 0)
    local_balance = channel_data.get("local_balance", 0)
    if capacity > 0:
        local_ratio = local_balance / capacity
        if local_ratio < 0.2:  # Less than 20% local liquidity
            print(
                f"DEBUG: Stuck adjustment skipped for chan_id={chan_id} due to low local liquidity ({local_ratio*100:.1f}%)"
            )
            return 0  # Skip adjustment

    # If the peer is determined to be stuck (no recent outbound forwards on any channel)
    if is_peer_stuck:
        print(
            f"DEBUG: Applying max stuck adjustment (4 bands) for chan_id={chan_id} because peer is stuck."
        )
        return 4  # Apply maximum band adjustment (to band 0)
    else:
        # Peer is not stuck (had recent outbound forwarding)
        print(
            f"DEBUG: No stuck adjustment needed for chan_id={chan_id} because peer is active."
        )
        return 0


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


# --- Redesigned print_fee_adjustment ---
def print_fee_adjustment(
    # Basic Info
    alias,
    pubkey,
    channel_ids_list,
    is_aggregated,
    capacity,
    local_balance,
    outbound_ratio,
    old_fee_rate,
    # Waterfall Steps
    base_fee,
    fee_base,
    base_adjust_pct,
    rate_after_base,
    group_adjust_pct,
    rate_after_group,
    trend_factor,
    trend_sensitivity,
    trend_adjust_pct,
    rate_after_trend,
    fee_band_factor,
    rate_after_fee_band,
    max_cap,
    rate_before_rounding,  # rate_after_fee_band capped
    final_rate,  # The actual applied rate (new_fee_rate)
    # Context
    group_name,
    fee_conditions,
    # Fee Band Context
    fee_band_enabled,
    fee_band_discount,
    fee_band_premium,
    initial_raw_band,
    stuck_adj_bands_applied,
    final_raw_band,
    fee_band_adj_pct,
    # Stuck Context
    stuck_adj_enabled,
    stuck_period,
    peer_stuck_status,
    is_low_liquidity_for_stuck,
    # Amboss Data
    amboss_data,
):
    print("-" * 80)
    print(f"Alias: {alias}{' (Aggregated)' if is_aggregated else ''}")
    print(f"Pubkey: {pubkey}")
    if is_aggregated:
        print(f"Channel IDs: {', '.join(channel_ids_list)}")
        print(f"Aggregate Capacity: {capacity:,.0f}")
        print(
            f"Aggregate Local Balance: {local_balance:,.0f} | (Outbound: {outbound_ratio*100:.1f}%)"
        )
    else:
        print(f"Channel ID: {channel_ids_list[0]}")  # Only one ID in the list
        print(f"Capacity: {capacity:,.0f}")
        print(
            f"Local Balance: {local_balance:,.0f} | (Outbound: {outbound_ratio*100:.1f}%)"
        )
    print(f"Old Fee Rate (LNDg): {old_fee_rate}")

    # --- Waterfall ---
    print("\n--- Fee Calculation Waterfall ---")
    print(f"  Start Fee (Amboss {fee_base.upper()}): {base_fee:,.1f} ppm")
    print(f"  +/- Base Adj ({base_adjust_pct*100:+.1f}%): {rate_after_base:,.1f} ppm")
    print(
        f"  +/- Group Adj ({group_adjust_pct*100:+.1f}%): {rate_after_group:,.1f} ppm"
    )
    print(
        f"  +/- Trend Adj ({trend_adjust_pct*100:+.1f}%): {rate_after_trend:,.1f} ppm"
    )
    if fee_band_enabled:
        print(
            f"  * Fee Band Adj ({fee_band_adj_pct*100:+.1f}%): {rate_after_fee_band:,.1f} ppm"
        )
    else:
        print(f"  * Fee Band Adj: Disabled")
    if rate_before_rounding < max_cap:
        print(f"  Max Cap ({max_cap} ppm): Not Applied")
        print(f"  Calculated Rate (Rounded): {final_rate} ppm")
    else:
        print(f"  Max Cap ({max_cap} ppm): Applied")
        print(f"  Calculated Rate (Capped & Rounded): {final_rate} ppm")
    print("---------------------------------")

    # --- Context ---
    print("\n--- Context & Settings ---")
    print(f"  Group: {group_name if group_name else 'N/A'}")
    print(f"  Fee Base Used: {fee_base}")
    print(
        f"  Trend Factor Used: {trend_factor:.2f} (Sensitivity: {trend_sensitivity:.2f})"
    )

    band_names = [
        "Max Discount (80-100%)",
        "High Discount (60-80%)",
        "Neutral (40-60%)",
        "Max Premium (20-40%)",
        "Max Premium (0-20%)",
    ]
    print(f"  Fee Bands: {'Enabled' if fee_band_enabled else 'Disabled'}")
    if fee_band_enabled:
        print(
            f"    - Discount: {fee_band_discount*100:.1f}%, Premium: {fee_band_premium*100:.1f}%"
        )
        print(
            f"    - Initial Band (Liquidity {outbound_ratio*100:.1f}%): {band_names[initial_raw_band]}"
        )
        print(f"    - Stuck Adjustment Applied: -{stuck_adj_bands_applied} bands")
        print(f"    - Final Band Used for Calc: {band_names[final_raw_band]}")
        print(f"    - Resulting Adjustment: {fee_band_adj_pct*100:+.1f}%")

    print(f"  Stuck Adjustment: {'Enabled' if stuck_adj_enabled else 'Disabled'}")
    if stuck_adj_enabled:
        print(f"    - Period: {stuck_period} days")
        print(f"    - Peer Status: {peer_stuck_status}")
        if is_low_liquidity_for_stuck:
            print(
                f"    - Adjustment Skipped: Low Liquidity ({outbound_ratio*100:.1f}% < 20%)"
            )
        elif stuck_adj_bands_applied > 0:
            print(f"    - Adjustment Applied: {stuck_adj_bands_applied} bands down")
        else:
            print(f"    - Adjustment Applied: 0 bands (Peer Active)")

    # print(f"  Full Fee Conditions: {json.dumps(fee_conditions)}") # Keep optional?

    # --- Amboss Table ---
    print("\n--- Amboss Peer Fee Data (Remote Perspective) ---")
    table = PrettyTable()
    table.field_names = ["Time", "Max", "Mean", "Median", "Weighted", "W.Corrected"]
    for time_range, fee_info in amboss_data.items():
        table.add_row(
            [
                time_range,
                fee_info.get("max", "N/A"),
                (
                    f"{float(fee_info.get('mean', 0)):.1f}"
                    if fee_info.get("mean") is not None
                    else "N/A"
                ),
                fee_info.get("median", "N/A"),
                (
                    f"{float(fee_info.get('weighted', 0)):.1f}"
                    if fee_info.get("weighted") is not None
                    else "N/A"
                ),
                (
                    f"{float(fee_info.get('weighted_corrected', 0)):.1f}"
                    if fee_info.get("weighted_corrected") is not None
                    else "N/A"
                ),
            ]
        )
    print(table)


def main():
    parser = argparse.ArgumentParser(
        description="Adjust LND channel fees based on Amboss data and local conditions."
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable detailed debug output for stuck channel checks.",
    )
    args = parser.parse_args()
    try:
        config = load_config()
        node_definitions = load_node_definitions()
        groups = node_definitions.get("groups", {})
        write_charge_lnd_file_enabled = node_definitions.get(
            "write_charge_lnd_file", False
        )
        lndg_fee_update_enabled = node_definitions.get("LNDg_fee_update", False)
        terminal_output_enabled = node_definitions.get("Terminal_output", False)
        show_debug_output = args.debug  # Use debug flag directly

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

            # --- Group overrides/defaults ---
            group_adjustment_percentage = 0
            if group_name and group_name in groups:
                group_fee_conditions = groups[group_name]
                group_adjustment_percentage = group_fee_conditions.get(
                    "group_adjustment_percentage", 0
                )
                # If node has no specific conditions, use group's; otherwise, node conditions take precedence
                if not fee_conditions:
                    fee_conditions = group_fee_conditions
            elif not fee_conditions:  # Node has no group and no specific conditions
                logging.warning(f"No fee conditions for {pubkey}, skipping.")
                continue

            # --- Extract key parameters ---
            fee_base = fee_conditions.get("fee_base", "median")
            fee_delta_threshold = fee_conditions.get("fee_delta_threshold", 20)
            max_cap = fee_conditions.get("max_cap", 1000)  # Default max_cap
            trend_sensitivity = fee_conditions.get(
                "trend_sensitivity", 1.0
            )  # Default sensitivity

            stuck_settings = fee_conditions.get("stuck_channel_adjustment", {})
            stuck_adj_enabled = stuck_settings.get("enabled", False)
            stuck_period = stuck_settings.get("stuck_time_period", 7)

            fee_bands_settings = fee_conditions.get("fee_bands", {})
            fee_band_enabled = fee_bands_settings.get("enabled", False)
            fee_band_discount = fee_bands_settings.get("discount", 0)
            fee_band_premium = fee_bands_settings.get("premium", 0)

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
                channel_ids_list = list(channels_to_modify.keys())

                if not channels_to_modify:
                    logging.info(
                        f"No open channels found for pubkey {pubkey}. Skipping."
                    )
                    continue

                # --- Aggregate Liquidity Calculation (if multiple channels) ---
                total_capacity = 0
                total_local_balance = 0
                first_channel_data = next(iter(channels_to_modify.values()))
                if num_channels > 1:
                    for chan_data in channels_to_modify.values():
                        total_capacity += chan_data["capacity"]
                        total_local_balance += chan_data["local_balance"]
                else:
                    total_capacity = first_channel_data["capacity"]
                    total_local_balance = first_channel_data["local_balance"]

                check_ratio = (
                    (total_local_balance / total_capacity) if total_capacity else 0
                )

                ## --- Liquidity Bounds Check ---
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
                        print("-" * 80)
                        print()
                        print(
                            f"\033[91mPeer {pubkey} (Alias: {alias_to_log}, {num_channels} channels) skipped due to outbound liquidity conditions.\033[0m"  # ADDED ANSI color codes
                        )
                        print(
                            f"\033[91mAggregate Ratio: {check_ratio:.2f}, Max: {max_outbound}, Min: {min_outbound}\033[0m"  # ADDED ANSI color codes
                        )
                    continue  # Skip this entire peer

                # --- Stuck Check ---
                peer_stuck_status = "not_applicable"  # If disabled
                channel_check_details = []
                is_peer_stuck = False
                if stuck_adj_enabled:
                    peer_stuck_status, channel_check_details = (
                        get_last_forwarding_timestamp_for_peer(
                            pubkey, channel_ids_list, stuck_period, config
                        )
                    )
                    if peer_stuck_status == "error":
                        logging.warning(
                            f"Error checking stuck status for {pubkey}, treating as stuck."
                        )
                        is_peer_stuck = True
                    else:
                        is_peer_stuck = peer_stuck_status == "stuck"

                # --- Calculate Adjustments ---
                potential_stuck_adjustment_bands = 0
                is_low_liquidity_for_stuck = check_ratio < 0.2
                if (
                    stuck_adj_enabled
                    and is_peer_stuck
                    and not is_low_liquidity_for_stuck
                ):
                    potential_stuck_adjustment_bands = 4  # Apply max discount

                fee_band_factor = 1.0
                initial_raw_band = 0
                final_raw_band = 0
                fee_band_adj_pct = 0.0
                if fee_band_enabled:
                    initial_raw_band = min(4, max(0, int((1 - check_ratio) * 5)))
                    fee_band_result = calculate_fee_band_adjustment(
                        fee_conditions, check_ratio, potential_stuck_adjustment_bands
                    )
                    # Unpack only if fee bands are enabled
                    if isinstance(fee_band_result, tuple):  # ADDED check for tuple type
                        fee_band_factor, final_raw_band = fee_band_result
                    else:  # If fee bands are disabled, fee_band_result is a float
                        fee_band_factor = fee_band_result  # Should be 1.0
                        final_raw_band = (
                            0  # Initialize as it won't be used in this case
                        )

                    fee_band_adj_pct = (
                        fee_band_factor - 1.0
                    )  # Get the percentage adjustment

                # --- Waterfall Calculation ---
                base_fee = float(all_amboss_data.get("TODAY", {}).get(fee_base, 0))
                trend_adjust_pct = trend_factor * trend_sensitivity

                rate_after_base = base_fee * (1 + base_adjustment_percentage)
                rate_after_group = rate_after_base * (1 + group_adjustment_percentage)
                rate_after_trend = rate_after_group * (1 + trend_adjust_pct)

                # Apply fee band factor only if bands are enabled
                rate_after_fee_band = (
                    rate_after_trend * fee_band_factor
                    if fee_band_enabled
                    else rate_after_trend
                )

                rate_before_rounding = min(rate_after_fee_band, max_cap)
                final_rate = round(rate_before_rounding)

                # --- Apply Updates & Notes ---
                updated_any_channel = False
                # ... (loops for update_lndg_fee and update_channel_notes using final_rate and potential_stuck_adjustment_bands) ...
                for chan_id, channel_data in channels_to_modify.items():
                    # Apply update if delta is sufficient
                    fee_delta = abs(final_rate - channel_data["local_fee_rate"])
                    if lndg_fee_update_enabled and fee_delta > fee_delta_threshold:
                        try:
                            update_lndg_fee(chan_id, final_rate, channel_data, config)
                            updated_any_channel = True
                        except LNDGAPIError as api_err:
                            logging.error(
                                f"Failed LNDg update for {chan_id}: {api_err}"
                            )
                            continue

                    # Update notes regardless of fee change if enabled
                    update_channel_notes_enabled = node_definitions.get(
                        "update_channel_notes", False
                    )
                    if update_channel_notes_enabled:
                        try:
                            update_channel_notes(
                                chan_id,
                                channel_data["alias"],
                                group_name,
                                fee_base,
                                fee_conditions,
                                check_ratio,  # Use aggregate ratio for notes
                                final_raw_band,  # Pass final band after stuck adjust
                                fee_band_factor,
                                config,
                                node_definitions,
                                final_rate,
                                potential_stuck_adjustment_bands,  # Pass stuck bands applied
                            )
                        except Exception as note_err:
                            logging.error(
                                f"Failed to update notes for {chan_id}: {note_err}"
                            )

                # --- Terminal Output ---
                if updated_any_channel or terminal_output_enabled:
                    if terminal_output_enabled:
                        print_fee_adjustment(
                            # Basic Info
                            alias=first_channel_data["alias"],
                            pubkey=pubkey,
                            channel_ids_list=channel_ids_list,
                            is_aggregated=(num_channels > 1),
                            capacity=total_capacity,
                            local_balance=total_local_balance,
                            outbound_ratio=check_ratio,
                            old_fee_rate=first_channel_data["local_fee_rate"],
                            # Waterfall
                            base_fee=base_fee,
                            fee_base=fee_base,
                            base_adjust_pct=base_adjustment_percentage,
                            rate_after_base=rate_after_base,
                            group_adjust_pct=group_adjustment_percentage,
                            rate_after_group=rate_after_group,
                            trend_factor=trend_factor,
                            trend_sensitivity=trend_sensitivity,
                            trend_adjust_pct=trend_adjust_pct,
                            rate_after_trend=rate_after_trend,
                            fee_band_factor=fee_band_factor,
                            rate_after_fee_band=rate_after_fee_band,  # Pass fee_band_factor instead of adj_pct here
                            max_cap=max_cap,
                            rate_before_rounding=rate_before_rounding,
                            final_rate=final_rate,
                            # Context
                            group_name=group_name,
                            fee_conditions=fee_conditions,
                            # Fee Band Context
                            fee_band_enabled=fee_band_enabled,
                            fee_band_discount=fee_band_discount,
                            fee_band_premium=fee_band_premium,
                            initial_raw_band=initial_raw_band,
                            stuck_adj_bands_applied=potential_stuck_adjustment_bands,
                            final_raw_band=final_raw_band,
                            fee_band_adj_pct=fee_band_adj_pct,
                            # Stuck Context
                            stuck_adj_enabled=stuck_adj_enabled,
                            stuck_period=stuck_period,
                            peer_stuck_status=peer_stuck_status,
                            is_low_liquidity_for_stuck=is_low_liquidity_for_stuck,
                            # Amboss Data
                            amboss_data=all_amboss_data,
                        )
                        sys.stdout.flush()  # Flush main block

                    # Conditional Debug Output
                    if (
                        show_debug_output and stuck_adj_enabled
                    ):  # Check debug flag and if stuck adj is enabled
                        print(f"\n--- Stuck Check Debug for Peer {pubkey[:10]}... ---")
                        # ... (debug printing logic as before) ...
                        for detail in channel_check_details:
                            short_api_url = detail["api_url"].split("?")[0] + "?..."
                            print(
                                f"  ChanID: {detail['checked_chan_id']} -> Status: {detail['status']}, Reason: {detail['reason']} (API: {short_api_url})"
                            )
                        print(f"  Overall Peer Status: {peer_stuck_status}")
                        if is_peer_stuck:
                            adj_reason = (
                                f"Applying {potential_stuck_adjustment_bands} bands (Liquidity {check_ratio*100:.1f}% >= 20%)"
                                if potential_stuck_adjustment_bands > 0
                                else f"Skipped (Liquidity {check_ratio*100:.1f}% < 20%)"
                            )
                            print(f"  Potential Stuck Adjustment: {adj_reason}")
                        else:
                            print(
                                f"  Potential Stuck Adjustment: 0 bands (Peer Active)"
                            )
                        print(
                            f"--- End Stuck Check Debug for Peer {pubkey[:10]}... ---"
                        )
                        sys.stdout.flush()  # Flush debug block

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
                        final_rate,
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
