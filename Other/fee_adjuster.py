"""
Guidelines for the Fee Adjuster Script

This script automates the adjustment of channel fees based on network conditions, peer behavior,
and local liquidity using data from the Amboss API and LNDg API.

Configuration Settings (see fee_adjuster_config_docs.txt for details):
- base_adjustment_percentage: Percentage adjustment applied to the selected fee base (median, mean, etc.).
- group_adjustment_percentage: Additional adjustment based on node group membership.
- max_cap: Maximum allowed fee rate (in ppm) for outgoing fees.
- trend_sensitivity: Multiplier for the influence of Amboss fee trends on the adjustment.
- fee_base: Statistical measure from Amboss used as the fee calculation base ("median", "mean", etc.).
- fee_delta_threshold: Minimum change (in ppm) required for either outgoing or inbound fees to trigger an API update to LNDg.
- groups: Node categories for applying differentiated strategies via group_adjustment_percentage.
- fee_bands: (Optional) Dynamic fee adjustments based on local liquidity for outgoing fees.
  - enabled: true/false.
  - discount: Negative percentage adjustment for high local balance (80-100%).
  - premium: Positive percentage adjustment for low local balance (0-40%).
- stuck_channel_adjustment: (Optional) Gradually reduces outgoing fees for channels without recent forwards.
  - enabled: true/false.
  - stuck_time_period: Number of days defining one 'stuck period' interval (e.g., 7).
  - min_local_balance_for_stuck_discount: (Optional) If the peer's aggregate local balance ratio is below this threshold (e.g., 0.2 for 20%), the stuck discount will not be applied.
  - min_updates_for_discount: (Optional) If the channel's `num_updates` is below this threshold, the fee band discount will not be applied. This is useful to prevent applying a discount to a newly opened channel.

- inbound_auto_fee_enabled: (Optional) Enables dynamic inbound fee adjustments. Can be set per-node or per-group. Defaults to false if not specified.
  - If enabled, the script will attempt to set a negative inbound fee (a discount) to incentivize rebalancing towards your node.
  - The discount is calculated based on the channel's `ar_max_cost` (auto-rebalancer max cost percentage, fetched from LNDg) and the current local liquidity band.
  - Higher discounts are applied when your local liquidity is lower (bands 2, 3, and 4), scaled by the `ar_max_cost`. No discount is applied in bands 0 and 1 (high local liquidity).
  - The `ar_max_cost` must be set in LNDg for the channel for inbound fees to be applied.

Groups and group_adjustment_percentage:
Allows tailored fee strategies for nodes in specific categories (e.g., "sink", "expensive").
Settings like `inbound_auto_fee_enabled` can also be defined at the group level and will be inherited by nodes in that group unless overridden at the node level.

Fee Bands (Outgoing Fees):
Adjusts outgoing fees based on local balance ratio, dividing liquidity into 5 bands (0-20%, 20-40%, 40-60%, 60-80%, 80-100%). A graduated adjustment is applied between the configured discount (high local balance) and premium (low local balance). The premium is capped at the 20-40% liquidity band to avoid excessively high fees on nearly drained channels.

Stuck Channel Adjustment (Outgoing Fees):
This feature incrementally reduces outgoing fees for channels that haven't forwarded payments recently.
For each multiple of the `stuck_time_period` (in days) that a peer's channels have gone without an *outbound* forwarding, the fee band is moved down by one level (towards the maximum discount).
The adjustment is capped at moving down 4 bands. If an outbound forward is detected, the stuck adjustment is reset.
If `min_local_balance_for_stuck_discount` is set and liquidity is below this, this discount mechanism is skipped.

Inbound Auto Fee Adjustment:
When enabled for a node/group and the channel has an `ar_max_cost` set in LNDg:
- Bands 0 & 1 (80-100% & 60-80% local liquidity): No inbound discount.
- Band 2 (40-60% local liquidity): Small inbound discount (e.g., 20% of `ar_max_cost` applied to current outgoing fee).
- Band 3 (20-40% local liquidity): Medium inbound discount (e.g., 50% of `ar_max_cost` applied to current outgoing fee).
- Band 4 (0-20% local liquidity): Large inbound discount (e.g., 80% of `ar_max_cost` applied to current outgoing fee).
The script queries LNDg for channel details including `ar_max_cost` and current inbound/outbound fees.

Usage:
- Configure nodes, groups, and their settings in `feeConfig.json`.
- Run the script to automatically adjust fees.
- Requires a running LNDg instance.

Command Line Arguments:
- --debug: Enable detailed debug output. Disables LNDg and charge-lnd file updates.

Charge-lnd Details:
Configure charge-lnd to use the output file for outgoing fees:
```ini
# charge-lnd.config
[🤖 FeeAdjuster Import]
strategy = use_config
config_file = file:///path/to/your/charge-lnd/.config/fee_adjuster.txt
```

Installation:
Add to crontab or use the systemd installer script:
```bash
crontab -e
# Add the following line (adjust path as needed)
0 * * * * /path/Lightning-Python-Tools/.venv/bin/python3 /path/Lightning-Python-Tools/Other/fee_adjuster.py >/dev/null 2>&1
```
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


def get_last_peer_forwarding_timestamp(pubkey, days_to_check_back, channel_ids, config):
    """
    Finds the timestamp of the most recent *outbound* forward for *any* channel
    belonging to the specified peer within the last `days_to_check_back`.
    Queries a batch of recent forwards for each channel and checks if any were outbound.

    Args:
        pubkey (str): The pubkey of the peer (used for logging/context).
        days_to_check_back (int): The number of days to check back for forwards.
        channel_ids (list): List of channel IDs belonging to the peer.
        config (configparser.ConfigParser): The configuration object.

    Returns:
        datetime or None: The datetime object of the last outbound forward across
                          all of the peer's channels, or None if no outbound forward
                          was found within the period among the fetched batches.
    """
    lndg_api_url = config["lndg"]["lndg_api_url"]
    username = config["credentials"]["lndg_username"]
    password = config["credentials"]["lndg_password"]
    cutoff_date = datetime.now() - timedelta(days=days_to_check_back)
    cutoff_date_str = cutoff_date.strftime("%Y-%m-%dT%H:%M:%S")

    latest_outbound_timestamp = None

    logging.debug(
        f"Checking last outbound forward for peer {pubkey} across {len(channel_ids)} channels in last {days_to_check_back} days."
    )

    for chan_id in channel_ids:
        # Query LNDg API for a batch of recent forwards involving this specific channel
        # within the time window, ordered by date descending.
        # Using a larger limit (e.g., 1000) to increase the chance of finding the
        # most recent outbound forward within the batch, even if interleaved with inbound.
        api_url = f"{lndg_api_url}/api/forwards/?format=json&chan_in_or_out={chan_id}&forward_date__gt={cutoff_date_str}&ordering=-forward_date&limit=1000"

        # Use logging.debug for per-channel check details, visible with --debug
        logging.debug(f"Querying LNDg for channel {chan_id}: {api_url}")

        try:
            response = requests.get(api_url, auth=(username, password))
            response.raise_for_status()
            data = response.json()

            # Iterate through the batch of forwards for this channel, looking for the most recent outbound one
            found_outbound_for_channel = False
            for result in data["results"]:
                forward_timestamp_str = result.get("forward_date")
                forward_id = result.get("id", "N/A")  # Get forward ID for debug

                if forward_timestamp_str and result.get("chan_id_out") == str(chan_id):
                    # Found an outbound forward for THIS channel within the fetched batch.
                    # Since results are ordered by date descending, this is the most recent *outbound*
                    # forward for this specific channel within the checked time window.
                    try:
                        forward_timestamp = datetime.strptime(
                            forward_timestamp_str, "%Y-%m-%dT%H:%M:%S.%f"
                        )
                    except ValueError:
                        forward_timestamp = datetime.strptime(
                            forward_timestamp_str, "%Y-%m-%dT%H:%M:%S"
                        )

                    logging.debug(
                        f"  [Chan {chan_id}] Found most recent outbound fwd (ID: {forward_id}) at {forward_timestamp_str}"
                    )

                    # Update the overall latest_outbound_timestamp for the peer if this one is more recent
                    if (
                        latest_outbound_timestamp is None
                        or forward_timestamp > latest_outbound_timestamp
                    ):
                        latest_outbound_timestamp = forward_timestamp
                        logging.debug(
                            f"  [Peer {pubkey[:8]}] Updated overall latest outbound timestamp to {latest_outbound_timestamp}"
                        )

                    found_outbound_for_channel = (
                        True  # Mark that we found at least one outbound in the batch
                    )
                    break  # Stop checking older forwards for *this channel* and move to the next channel

            if not found_outbound_for_channel:
                logging.debug(
                    f"  [Chan {chan_id}] No outbound forwards found in last {days_to_check_back} days among the top 1000 recent forwards."
                )

        except requests.exceptions.RequestException as e:
            logging.error(f"  [Chan {chan_id}] API Error checking forward: {e}")
            # Continue to the next channel on error, don't fail the whole peer check

        except Exception as e:
            logging.error(
                f"  [Chan {chan_id}] Unexpected error processing forward: {e}"
            )
            # Continue to the next channel on error

    # After checking all channels, return the single latest outbound timestamp found for the peer.
    return latest_outbound_timestamp


def calculate_stuck_channel_band_adjustment(
    fee_conditions,
    outbound_ratio,  # Added: The current outbound ratio for the peer
    last_forward_timestamp,  # datetime of last outbound forward (or None)
    stuck_time_period,  # configured stuck period in days
    checked_window_days,  # New parameter: The number of days checked back by the API call
    min_local_balance_for_stuck_discount,  # NEW: Minimum local balance to apply stuck discount
):
    """
    Calculate the number of bands to move down based on how long the peer
    has been stuck, applying incremental adjustment per stuck_time_period.

    Args:
        fee_conditions: Dictionary containing stuck channel settings.
        outbound_ratio: The aggregate outbound liquidity ratio for the peer (0.0-1.0).
        last_forward_timestamp: datetime object of the last outbound forward, or None.
        stuck_time_period: The configured stuck time period in days.
        checked_window_days: The number of days the API checked back for forwards.
        min_local_balance_for_stuck_discount: Minimum local balance ratio to apply discount.


    Returns:
        tuple: (bands_to_move_down: int, days_stuck: int, skip_reason: str|None)
               bands_to_move_down is the calculated number of bands (0-4).
               days_stuck is the number of days since the last forward, or
               checked_window_days + 1 if no forward was found in the window.
               skip_reason is a string if adjustment was skipped, else None.
    """
    stuck_settings = fee_conditions.get("stuck_channel_adjustment", {})
    if not stuck_settings.get("enabled", False):
        return 0, 0, "Stuck adjustment disabled in config"

    # NEW: Skip stuck channel discount if local liquidity is too low
    if outbound_ratio < min_local_balance_for_stuck_discount:
        days_stuck_for_low_liq = 0
        if last_forward_timestamp is None:
            days_stuck_for_low_liq = checked_window_days + 1
        else:
            days_stuck_for_low_liq = (datetime.now() - last_forward_timestamp).days
        return (
            0,
            days_stuck_for_low_liq,
            f"Low liquidity ({outbound_ratio*100:.1f}% < {min_local_balance_for_stuck_discount*100:.1f}%)",
        )

    if last_forward_timestamp is None:
        days_stuck = checked_window_days + 1
        calculated_bands_to_move_down = checked_window_days // stuck_time_period
    else:
        time_difference = datetime.now() - last_forward_timestamp
        days_stuck = time_difference.days
        calculated_bands_to_move_down = days_stuck // stuck_time_period

    bands_to_move_down = min(calculated_bands_to_move_down, 4)

    logging.debug(
        f"Stuck calculation (within {checked_window_days}d window): days_stuck={days_stuck}, stuck_period={stuck_time_period}, calculated_bands={calculated_bands_to_move_down}, final_bands_to_move_down={bands_to_move_down}"
    )

    return bands_to_move_down, days_stuck, None  # No skip reason if we got this far


def calculate_fee_band_adjustment(
    fee_conditions,
    outbound_ratio,
    num_updates,
    stuck_bands_to_move_down=0,  # New parameter: Number of bands to move down due to stuck status
):
    """
    Calculate fee adjustment based on outbound liquidity ratio bands, applying
    an additional adjustment based on how many bands to move down due to stuck status.

    Args:
        fee_conditions: Dictionary containing fee band settings.
        outbound_ratio: Ratio of local balance to total capacity (0.0 to 1.0).
        stuck_bands_to_move_down: Number of bands to move down due to stuck channel (0-4).

    Returns:
        tuple: (adjustment_factor: float, initial_raw_band: int, final_raw_band: int)
               adjustment_factor is the multiplicative factor to apply to the fee.
               initial_raw_band is the band based on liquidity before stuck adjustment.
               final_raw_band is the band after applying stuck adjustment (used for calc).
    """
    # Check if fee bands are enabled
    if not fee_conditions.get("fee_bands", {}).get("enabled", False):
        # Return 1.0, 0, 0 if disabled (bands don't apply)
        return 1.0, 0, 0

    # Get fee band parameters
    fee_bands = fee_conditions.get("fee_bands", {})
    discount = fee_bands.get("discount", 0)
    premium = fee_bands.get("premium", 0)

    # Get min_updates_for_discount from stuck_channel_adjustment section
    stuck_settings = fee_conditions.get("stuck_channel_adjustment", {})
    min_updates_for_discount = stuck_settings.get("min_updates_for_discount", 0)

    # Calculate the initial band based on current liquidity (0-4)
    # Band 0 (80-100% local) -> index 0
    # Band 1 (60-80% local) -> index 1
    # Band 2 (40-60% local) -> index 2
    # Band 3 (20-40% local) -> index 3
    # Band 4 (0-20% local) -> index 4
    initial_raw_band = min(4, max(0, int((1 - outbound_ratio) * 5)))

    # Apply stuck channel adjustment: move down bands towards Band 0 (max discount)
    # Cap the resulting band index at 0 (cannot move below Band 0).
    adjusted_raw_band = max(0, initial_raw_band - stuck_bands_to_move_down)

    # Map bands 3 and 4 to have the same premium level for fee calculation
    # The calculation is based on the adjusted_raw_band.
    effective_band_for_calc = min(
        3, adjusted_raw_band
    )  # Cap calculation basis at band 3

    # Calculate the adjustment percentage: linearly interpolate between discount (band 0)
    # and premium (band 3).
    adjustment_range = premium - discount
    if adjustment_range == 0:
        adjustment = discount  # Or 0, since premium == discount
    else:
        # Calculate adjustment percentage based on the effective band (0-3)
        adjustment_per_band_step = adjustment_range / 3.0
        adjustment = discount + effective_band_for_calc * adjustment_per_band_step

    # If the channel is new, don't apply the discount
    if num_updates < min_updates_for_discount and adjustment < 0:
        adjustment = 0

    # Return the multiplicative factor and the initial/final bands for printing/notes
    return 1 + adjustment, initial_raw_band, adjusted_raw_band


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
            fee_band_factor, initial_raw_band, final_raw_band = fee_band_result
            basic_fee_rate = basic_fee_rate * fee_band_factor
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
                    ar_max_cost = result.get("ar_max_cost")
                    local_inbound_fee_rate = result.get("local_inbound_fee_rate")
                    num_updates = result.get("num_updates", 0)

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
                            "ar_max_cost": ar_max_cost,
                            "local_inbound_fee_rate": local_inbound_fee_rate,
                            "num_updates": num_updates,
                        }
        return channels_to_modify
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching LNDg channels: {e}")
        raise LNDGAPIError(f"Error fetching LNDg channels: {e}")


def calculate_inbound_fee_discount_ppm(
    calculated_final_outgoing_fee_ppm, initial_raw_band, ar_max_cost_percent
):
    """
    Calculates the inbound fee discount in PPM.
    Returns a negative PPM value for discount, or 0 if no discount.
    """
    if ar_max_cost_percent is None or ar_max_cost_percent == 0:
        return 0

    inbound_fee_discount_ppm = 0
    ar_max_cost_fraction = ar_max_cost_percent / 100.0

    if initial_raw_band == 2:  # Neutral Liquidity (40-60% local)
        inbound_fee_discount_ppm = -round(
            calculated_final_outgoing_fee_ppm * ar_max_cost_fraction * 0.20
        )
    elif initial_raw_band == 3:  # Low Local Liquidity (20-40% local)
        inbound_fee_discount_ppm = -round(
            calculated_final_outgoing_fee_ppm * ar_max_cost_fraction * 0.55
        )
    elif initial_raw_band == 4:  # Very Low Local Liquidity (0-20% local)
        inbound_fee_discount_ppm = -round(
            calculated_final_outgoing_fee_ppm * ar_max_cost_fraction * 0.90
        )

    # Ensure the effective fee (outgoing + inbound_discount) isn't negative
    if calculated_final_outgoing_fee_ppm + inbound_fee_discount_ppm < 0:
        inbound_fee_discount_ppm = (
            -calculated_final_outgoing_fee_ppm
        )  # Max possible discount to make effective fee 0

    return inbound_fee_discount_ppm


# Write to LNDg
def update_lndg_fee(
    chan_id,
    new_outgoing_fee_rate,
    new_inbound_fee_rate_ppm,
    channel_data,
    config,
    log_api_response=False,
):
    lndg_api_url = config["lndg"]["lndg_api_url"]
    username = config["credentials"]["lndg_username"]
    password = config["credentials"]["lndg_password"]
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # First, update auto_fees if needed (for outgoing)
    if channel_data["auto_fees"]:
        auto_fees_url = f"{lndg_api_url}/api/channels/{chan_id}/"
        auto_fees_payload = {"chan_id": chan_id, "auto_fees": False}
        try:
            response = requests.put(
                auto_fees_url, json=auto_fees_payload, auth=(username, password)
            )
            response.raise_for_status()
            logging.info(
                f"{timestamp}: Disabled auto_fees for channel {chan_id} (for outgoing)"
            )
        except requests.exceptions.RequestException as e:
            logging.error(f"Error updating auto_fees for channel {chan_id}: {e}")
            # Continue to fee policy update even if this fails

    # Then, update the fee policy (outgoing and inbound)
    fee_update_url = f"{lndg_api_url}/api/chanpolicy/"

    # Fix: Send -0 instead of 0 for inbound fees to ensure LNDg properly sets it to zero
    inbound_fee_to_send = new_inbound_fee_rate_ppm
    if inbound_fee_to_send == 0:
        inbound_fee_to_send = -0  # Explicitly set to negative zero

    fee_payload = {
        "chan_id": chan_id,
        "fee_rate": new_outgoing_fee_rate,
        "inbound_fee_rate": inbound_fee_to_send,
    }

    try:
        # ADDED: Log the exact payload being sent
        logging.debug(
            f"{timestamp}: Sending LNDg fee update payload for {chan_id}: {json.dumps(fee_payload)}"
        )

        response = requests.post(
            fee_update_url, json=fee_payload, auth=(username, password)
        )

        if log_api_response:  # Only log verbose response if specifically requested
            logging.debug(
                f"{timestamp}: LNDg fee update response for {chan_id}: Status={response.status_code}, Text={response.text}"
            )

        response.raise_for_status()  # This will raise an exception for 4xx/5xx responses
        logging.info(
            f"{timestamp}: API confirmed changing outgoing fee to {new_outgoing_fee_rate} ppm "
            f"and inbound fee discount to {new_inbound_fee_rate_ppm} ppm for channel {chan_id}"
        )
    except requests.exceptions.RequestException as e:
        logging.error(f"Error updating LNDg fee policy for channel {chan_id}: {e}")
        # If response was available but an error occurred, log its details
        if hasattr(e, "response") and e.response is not None:
            logging.error(
                f"LNDg API Error Response Details: Status={e.response.status_code}, Text={e.response.text}"
            )
        raise LNDGAPIError(f"Error updating LNDg fee policy for channel {chan_id}: {e}")


def write_charge_lnd_file(
    file_path,
    pubkey,
    alias,
    new_fee_rate,
    is_aggregated,
    # New parameters for inbound fees for charge-lnd
    inbound_auto_fee_enabled_for_node,
    calculated_inbound_ppm_for_charge_lnd,  # Pass the specific inbound PPM for this peer/aggregation
):
    with open(file_path, "a") as f:
        f.write(f"[🤖 {alias}{' (Aggregated)' if is_aggregated else ''}]\n")
        f.write(f"node.id = {pubkey}\n")
        f.write("strategy = static\n")
        f.write(f"fee_ppm = {new_fee_rate}\n")
        # Add inbound fee details if the feature is enabled for this node
        if inbound_auto_fee_enabled_for_node:
            f.write(f"inbound_fee_ppm = {calculated_inbound_ppm_for_charge_lnd}\n")
            f.write(
                "inbound_base_fee_msat = 0\n"
            )  # Defaulting inbound base to 0 for now
        # Default base fees and HTLC settings, consider making these configurable
        f.write("base_fee_msat = 1000\n")  # Default for outgoing, might need review
        f.write("min_htlc_msat = 1_000\n")
        f.write("max_htlc_msat_ratio = 0.9\n")
        f.write("\n")


def update_channel_notes(
    chan_id,
    alias,
    group_name,
    fee_base,
    fee_conditions,
    outbound_ratio,
    initial_raw_band,
    fee_band_factor,
    config,
    node_definitions,
    new_fee_rate=None,
    stuck_bands_to_move_down=0,
    days_stuck=None,
    stuck_skip_reason=None,
    inbound_auto_fee_enabled=False,
    calculated_inbound_ppm=0,
    ar_max_cost=None,
):
    """Update the channel notes in LNDg with fee adjuster information."""
    update_channel_notes_enabled = node_definitions.get("update_channel_notes", False)

    if not update_channel_notes_enabled:
        logging.info(f"Channel notes updates are disabled in config")
        return

    lndg_api_url = config["lndg"]["lndg_api_url"]
    username = config["credentials"]["lndg_username"]
    password = config["credentials"]["lndg_password"]

    # Build the notes text
    notes = f"🔋 Group: {group_name if group_name else 'None'} | Base: {fee_base}"

    # Add fee bands info in a condensed format
    fee_bands = fee_conditions.get("fee_bands", {})
    fee_band_enabled = fee_bands.get("enabled", False)
    if fee_band_enabled:
        discount = fee_bands.get("discount", 0)
        premium = fee_bands.get("premium", 0)

        # Calculate the effective adjustment percentage based on the final band
        adjustment_range = premium - discount
        # Recalculate adjusted band based on initial and stuck_bands_to_move_down
        adjusted_raw_band = max(0, initial_raw_band - stuck_bands_to_move_down)
        effective_band_for_calc = min(3, adjusted_raw_band)
        if adjustment_range == 0:
            actual_adjustment = discount
        else:
            adjustment_per_band_step = adjustment_range / 3.0
            actual_adjustment = (
                discount + effective_band_for_calc * adjustment_per_band_step
            )

        notes += f"\nCurrent Adj: {actual_adjustment*100:.1f}%"
        notes += f"\nBands: ✅ | Disc: {discount*100:.0f}% | Prem: {premium*100:.0f}% | Bal: {outbound_ratio*100:.0f}%"
        # Add initial and final band info to notes
        band_names_short = [
            "D+",
            "D",
            "N",
            "P",
            "P+",
        ]  # MaxDisc, HighDisc, Neutral, Prem, MaxPrem
        notes += f" | Initial Band: {band_names_short[initial_raw_band]} | Final Band: {band_names_short[adjusted_raw_band]}"  # Use adjusted_raw_band for final position

    else:
        notes += f"\nFee Bands: Disabled"

    # Add fee rate info if available
    if new_fee_rate is not None:
        notes += f"\nCurrent Rate: {new_fee_rate} ppm"

    # Add stuck channel info if applicable
    stuck_settings = fee_conditions.get("stuck_channel_adjustment", {})
    if stuck_settings.get("enabled", False):
        stuck_period = stuck_settings.get("stuck_time_period", 7)
        min_stuck_balance_threshold = stuck_settings.get(
            "min_local_balance_for_stuck_discount", 0.0
        )  # Get the threshold for notes

        stuck_notes_part = f"\nStuck adjust: "
        if stuck_skip_reason:
            stuck_notes_part += f"❌ ({stuck_skip_reason})"
        elif stuck_bands_to_move_down > 0:
            stuck_notes_part += f"✅"
        else:  # Enabled, no skip reason, but 0 bands down (e.g. active or days_stuck < period)
            stuck_notes_part += f"✔ (Active or <{stuck_period}d)"

        stuck_notes_part += f" | Period: {stuck_period}d"
        if (
            days_stuck is not None
        ):  # days_stuck will be populated even if skipped for low liquidity
            stuck_notes_part += f" | Stuck: {days_stuck}d"

        # Add threshold to notes if it's greater than 0 for clarity
        if min_stuck_balance_threshold > 0:
            stuck_notes_part += f" | MinLiq: {min_stuck_balance_threshold*100:.0f}%"

        if (
            stuck_bands_to_move_down > 0 and not stuck_skip_reason
        ):  # Only show bands down if applied
            stuck_notes_part += f" | Bands Down: {stuck_bands_to_move_down}"

        notes += stuck_notes_part
    else:
        notes += "\nStuck adjust: Disabled"

    # Add inbound fee info
    if inbound_auto_fee_enabled:
        notes += f"\nInbound AF: ✅"
        if calculated_inbound_ppm != 0:
            notes += f" | Discount: {calculated_inbound_ppm} ppm (AR Max Cost: {ar_max_cost if ar_max_cost is not None else 'N/A'}%)"
        else:
            notes += f" | Discount: 0 ppm (No discount for band/AR cost)"
    else:
        notes += "\nInbound AF: Disabled"

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
    fee_band_factor,  # Pass the factor calculated by calculate_fee_band_adjustment
    rate_after_fee_band,  # The rate after applying fee band factor
    max_cap,
    rate_before_rounding,  # rate_after_fee_band capped at max_cap
    final_rate,  # The actual applied rate (new_fee_rate)
    # Context
    group_name,
    fee_conditions,
    # Fee Band Context
    fee_band_enabled,
    fee_band_discount,
    fee_band_premium,
    num_updates,
    min_updates_for_discount,
    initial_raw_band,  # Initial band based on liquidity
    stuck_adj_bands_applied,  # How many bands were moved down due to stuck status
    final_raw_band,  # Final band used for adjustment calculation
    fee_band_adj_pct,  # The resulting percentage adjustment from fee bands + stuck
    # Stuck Context # Add these new parameters
    stuck_adj_enabled,
    stuck_period,
    peer_stuck_status,  # Now a detailed status string
    stuck_skip_reason,  # NEW: Reason for skipping stuck adjustment
    min_local_balance_for_stuck_discount,  # NEW: The threshold used
    days_stuck,  # Days since last outbound forward
    # Amboss Data
    amboss_data,
    # New Inbound Fee Parameters
    inbound_auto_fee_enabled=False,
    calculated_inbound_ppm=0,
    ar_max_cost=None,
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
    print(f"Num Updates: {num_updates}")
    print(f"Min Updates for Discount: {min_updates_for_discount}")

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
        if stuck_adj_enabled:  # Only show stuck band adjustment if stuck is enabled
            print(f"    - Stuck Adjustment Applied: -{stuck_adj_bands_applied} bands")
            print(f"    - Final Band Used for Calc: {band_names[final_raw_band]}")
        else:
            print(
                f"    - Final Band Used for Calc: {band_names[final_raw_band]} (No Stuck Adj)"
            )

        print(f"    - Resulting Adjustment: {fee_band_adj_pct*100:+.1f}%")

    print(f"  Stuck Adjustment: {'Enabled' if stuck_adj_enabled else 'Disabled'}")
    if stuck_adj_enabled:
        print(f"    - Period: {stuck_period} days")
        print(
            f"    - Min Local Balance for Discount: {min_local_balance_for_stuck_discount*100:.1f}%"
        )  # Show the threshold
        print(f"    - Days Stuck: {days_stuck} days")
        print(f"    - Peer Status: {peer_stuck_status}")
        if stuck_skip_reason:  # Use the new skip_reason
            print(f"    - Adjustment Skipped: {stuck_skip_reason}")
        elif stuck_adj_bands_applied > 0:
            print(f"    - Adjustment Applied: {stuck_adj_bands_applied} bands down")
        else:
            print(
                f"    - Adjustment Applied: 0 bands (Peer active or days stuck < period)"
            )

    # --- Inbound Fee Auto Adjustment ---
    print(f"\n--- Inbound Auto Fee Adjustment ---")
    print(
        f"  Inbound Auto Fee Global Setting: {'Enabled' if inbound_auto_fee_enabled else 'Disabled'}"
    )
    if inbound_auto_fee_enabled:
        print(
            f"    - Channel AR Max Cost Target: {ar_max_cost if ar_max_cost is not None else 'N/A'}%"
        )
        print(f"    - Calculated Inbound Fee Discount: {calculated_inbound_ppm} ppm")
        if calculated_inbound_ppm != 0:
            print(
                f"    - Effective Rate (Outgoing + Inbound Discount): {final_rate + calculated_inbound_ppm} ppm"
            )
        else:
            print(f"    - Effective Rate: Same as Outgoing ({final_rate} ppm)")

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
        help="Enable detailed debug output for stuck channel checks. Disables LNDg and charge-lnd file updates.",
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
        skip_charge_lnd_file_write = False
        if args.debug:
            terminal_output_enabled = True
            lndg_fee_update_enabled = False
            skip_charge_lnd_file_write = True

        for node in node_definitions["nodes"]:
            pubkey = node["pubkey"]
            group_name = node.get("group")
            fee_conditions = None
            base_adjustment_percentage = 0

            # Determine inbound_auto_fee_enabled for this node (node -> group -> default false)
            inbound_auto_fee_enabled_for_node = node.get(
                "inbound_auto_fee_enabled"
            )  # Check node first

            if "fee_conditions" in node:
                fee_conditions = node["fee_conditions"]
                base_adjustment_percentage = fee_conditions.get(
                    "base_adjustment_percentage", 0
                )
                # Check fee_conditions for inbound_auto_fee if not directly on node
                if inbound_auto_fee_enabled_for_node is None:
                    inbound_auto_fee_enabled_for_node = fee_conditions.get(
                        "inbound_auto_fee_enabled"
                    )

            group_adjustment_percentage = 0
            if group_name and group_name in groups:
                group_fee_conditions = groups[group_name]
                group_adjustment_percentage = group_fee_conditions.get(
                    "group_adjustment_percentage", 0
                )
                if (
                    not fee_conditions
                ):  # If node has no specific conditions, use group's
                    fee_conditions = group_fee_conditions

                # If still not set, check group for inbound_auto_fee_enabled
                if inbound_auto_fee_enabled_for_node is None:
                    inbound_auto_fee_enabled_for_node = group_fee_conditions.get(
                        "inbound_auto_fee_enabled"
                    )

            elif not fee_conditions:
                logging.warning(f"No fee conditions for {pubkey}, skipping.")
                continue

            # Default to False if not found anywhere
            if inbound_auto_fee_enabled_for_node is None:
                inbound_auto_fee_enabled_for_node = False

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
            min_local_balance_for_stuck_discount = stuck_settings.get(
                "min_local_balance_for_stuck_discount", 0.0
            )

            # Calculate the maximum relevant historical window for stuck checks
            # 4 is the maximum number of bands down, so we only need to check back 4 * stuck_time_period days
            stuck_check_window_days = stuck_period * 4

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

                ## --- Liquidity Bounds Check (REMOVED) ---
                # The max_outbound check has been removed.
                # The script will now always proceed unless other skip conditions are met.

                # --- Stuck Check & Adjustment Calculation ---
                stuck_bands_to_move_down = 0
                days_stuck = 0
                last_forward_timestamp = None
                stuck_skip_reason = None  # Initialize skip reason

                peer_stuck_status = "N/A (Stuck Adj. Disabled)"

                if stuck_adj_enabled:
                    peer_stuck_status = "Checking..."
                    last_forward_timestamp = get_last_peer_forwarding_timestamp(
                        pubkey, stuck_check_window_days, channel_ids_list, config
                    )

                    bands_calc_result, days_stuck_calc, skip_reason_calc = (
                        calculate_stuck_channel_band_adjustment(
                            fee_conditions,
                            check_ratio,  # Pass the aggregate outbound ratio
                            last_forward_timestamp,
                            stuck_period,
                            stuck_check_window_days,
                            min_local_balance_for_stuck_discount,  # Pass the new threshold
                        )
                    )
                    stuck_bands_to_move_down = bands_calc_result
                    days_stuck = days_stuck_calc
                    stuck_skip_reason = skip_reason_calc

                    # Determine the final peer status string for printing/notes
                    if stuck_skip_reason:
                        peer_stuck_status = f"Stuck Adj. Skipped ({stuck_skip_reason})"
                    elif last_forward_timestamp is not None:
                        peer_stuck_status = "Active (Recent Outbound Forward)"
                    else:  # No skip reason, no timestamp -> stuck beyond window
                        peer_stuck_status = f"Stuck (>{stuck_check_window_days} days, {stuck_bands_to_move_down} bands down)"

                if (
                    terminal_output_enabled and stuck_adj_enabled
                ):  # Debug output for stuck check
                    print("-" * 80)
                    print(f"--- Stuck Check Debug for Peer {pubkey[:10]}... ---")
                    print(f"  Configured stuck_time_period: {stuck_period} days")
                    print(
                        f"  Configured min_local_balance_for_stuck_discount: {min_local_balance_for_stuck_discount*100:.1f}%"
                    )
                    print(f"  API Check Window: {stuck_check_window_days} days")
                    print(
                        f"  Last outbound forward timestamp found: {last_forward_timestamp if last_forward_timestamp else 'None'}"
                    )
                    print(f"  Calculated days since last forward: {days_stuck}")
                    print(
                        f"  Aggregate Outbound Ratio for Peer: {check_ratio*100:.1f}%"
                    )
                    if stuck_skip_reason:
                        print(f"  Stuck Adjustment Skip Reason: {stuck_skip_reason}")
                    print(
                        f"  Calculated stuck bands down (after checks): {stuck_bands_to_move_down}"
                    )
                    print(
                        f"  Final Peer Status determination for stuck logic: {peer_stuck_status}"
                    )
                    print("--- End Stuck Check Debug ---")
                    sys.stdout.flush()

                # --- Calculate Adjustments ---
                # Call calculate_fee_band_adjustment with the determined stuck_bands_to_move_down
                # It now returns factor, initial_raw_band, final_raw_band
                fee_band_result = calculate_fee_band_adjustment(
                    fee_conditions, check_ratio, stuck_bands_to_move_down
                )
                fee_band_factor, initial_raw_band, final_raw_band = fee_band_result
                fee_band_adj_pct = fee_band_factor - 1.0

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

                # --- Inbound Fee Calculation (per peer, but uses channel's ar_max_cost if different) ---
                # For aggregated peers, this assumes ar_max_cost would be similar or we'd use first channel's.
                # The current loop is per-channel for updates, so this fits.

                calculated_inbound_ppm_for_peer = 0

                # Use the resolved inbound_auto_fee_enabled
                if inbound_auto_fee_enabled_for_node:
                    first_chan_ar_max_cost = first_channel_data.get("ar_max_cost")
                    if first_chan_ar_max_cost is not None:
                        calculated_inbound_ppm_for_peer = (
                            calculate_inbound_fee_discount_ppm(
                                final_rate, initial_raw_band, first_chan_ar_max_cost
                            )
                        )

                # --- Apply Updates & Notes ---
                updated_any_channel = False
                # Loop through channels to apply updates and notes
                for chan_id, channel_data in channels_to_modify.items():
                    # Calculate final_rate (outgoing) - this is already done for the peer
                    # Calculate inbound PPM specifically for this channel
                    current_chan_calculated_inbound_ppm = 0
                    chan_ar_max_cost = channel_data.get("ar_max_cost")

                    # Use the resolved inbound_auto_fee_enabled
                    if (
                        inbound_auto_fee_enabled_for_node
                        and chan_ar_max_cost is not None
                    ):
                        current_chan_calculated_inbound_ppm = (
                            calculate_inbound_fee_discount_ppm(
                                final_rate, initial_raw_band, chan_ar_max_cost
                            )
                        )

                    # Determine if an update to LNDg is needed based on deltas
                    should_update_lndg_for_this_channel = False

                    # Outbound fee check
                    current_outbound_fee_on_channel = channel_data["local_fee_rate"]
                    outbound_fee_delta = abs(
                        final_rate - current_outbound_fee_on_channel
                    )
                    if outbound_fee_delta > fee_delta_threshold:
                        should_update_lndg_for_this_channel = True

                    # Inbound fee check (only if not already decided by outbound change and feature is enabled)
                    if (
                        not should_update_lndg_for_this_channel
                        and inbound_auto_fee_enabled_for_node
                    ):
                        current_inbound_fee_on_channel = int(
                            channel_data.get("local_inbound_fee_rate", 0) or 0
                        )
                        current_chan_calculated_inbound_ppm = int(
                            current_chan_calculated_inbound_ppm
                        )

                        logging.debug(
                            f"Comparing inbound fee: current={current_inbound_fee_on_channel} (type {type(current_inbound_fee_on_channel)}), new={current_chan_calculated_inbound_ppm} (type {type(current_chan_calculated_inbound_ppm)})"
                        )

                        inbound_fee_delta = abs(
                            current_chan_calculated_inbound_ppm
                            - current_inbound_fee_on_channel
                        )

                        if inbound_fee_delta > fee_delta_threshold:
                            should_update_lndg_for_this_channel = True

                    if lndg_fee_update_enabled and should_update_lndg_for_this_channel:
                        try:
                            update_lndg_fee(
                                chan_id,
                                final_rate,
                                current_chan_calculated_inbound_ppm,
                                channel_data,
                                config,
                                log_api_response=True,
                            )
                            updated_any_channel = True
                        except LNDGAPIError as api_err:
                            logging.error(
                                f"Failed LNDg update for {chan_id}: {api_err}"
                            )
                            continue  # Continue to the next channel for notes update

                    # Update notes regardless of fee change if enabled
                    update_channel_notes_enabled = node_definitions.get(
                        "update_channel_notes", False
                    )
                    if update_channel_notes_enabled:
                        try:
                            # Pass all relevant stuck info to update_channel_notes
                            update_channel_notes(
                                chan_id,
                                channel_data["alias"],
                                group_name,
                                fee_base,
                                fee_conditions,  # Pass full fee_conditions
                                check_ratio,  # Use aggregate ratio for notes
                                initial_raw_band,  # Pass initial raw band
                                fee_band_factor,  # Pass fee band factor for debug in notes function if needed
                                config,
                                node_definitions,
                                final_rate,  # Pass the final calculated fee rate
                                stuck_bands_to_move_down,  # Pass the calculated stuck bands applied
                                days_stuck,  # Pass the calculated days stuck
                                stuck_skip_reason,  # Pass the skip reason to notes
                                inbound_auto_fee_enabled=inbound_auto_fee_enabled_for_node,  # Pass resolved value
                                calculated_inbound_ppm=current_chan_calculated_inbound_ppm,
                                ar_max_cost=chan_ar_max_cost,
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
                            alias=first_channel_data.get(
                                "alias", pubkey[:8]
                            ),  # Use first alias, fallback to pubkey
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
                            fee_band_factor=fee_band_factor,  # Pass fee_band_factor here
                            rate_after_fee_band=rate_after_fee_band,  # Pass rate_after_fee_band here
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
                            initial_raw_band=initial_raw_band,  # Pass initial raw band
                            stuck_adj_bands_applied=stuck_bands_to_move_down,  # Pass stuck bands applied
                            final_raw_band=final_raw_band,  # Pass final raw band used for calc
                            fee_band_adj_pct=fee_band_adj_pct,  # Pass fee band adj pct
                            # Stuck Context
                            stuck_adj_enabled=stuck_adj_enabled,
                            stuck_period=stuck_period,
                            peer_stuck_status=peer_stuck_status,
                            stuck_skip_reason=stuck_skip_reason,  # Pass to print function
                            min_local_balance_for_stuck_discount=min_local_balance_for_stuck_discount,  # Pass to print
                            days_stuck=days_stuck,
                            # Amboss Data
                            amboss_data=all_amboss_data,
                            inbound_auto_fee_enabled=inbound_auto_fee_enabled_for_node,  # Pass resolved value
                            calculated_inbound_ppm=calculated_inbound_ppm_for_peer,
                            ar_max_cost=first_channel_data.get("ar_max_cost"),
                            # New num_updates parameter
                            num_updates=first_channel_data["num_updates"],
                            min_updates_for_discount=stuck_settings.get(
                                "min_updates_for_discount", 0
                            ),
                        )
                        sys.stdout.flush()  # Ensure output is shown immediately

                # Write to charge-lnd file once per peer if enabled and any channel updated
                if (
                    write_charge_lnd_file_enabled
                    and updated_any_channel
                    and not skip_charge_lnd_file_write
                ):
                    charge_lnd_file_path = os.path.join(
                        config["paths"]["charge_lnd_path"], "fee_adjuster.txt"
                    )
                    # Pass the inbound fee details to write_charge_lnd_file
                    # calculated_inbound_ppm_for_peer is the peer-level summary calculated earlier
                    write_charge_lnd_file(
                        charge_lnd_file_path,
                        pubkey,
                        first_channel_data["alias"],
                        final_rate,  # This is the outgoing fee rate
                        num_channels > 1,
                        inbound_auto_fee_enabled_for_node,  # Pass the resolved enable status
                        calculated_inbound_ppm_for_peer,  # Pass the peer-level calculated inbound ppm
                    )

            except (AmbossAPIError, LNDGAPIError) as e:
                logging.error(f"Error processing node {pubkey}: {e}")
                continue
    except Exception as e:
        logging.error(f"An unexpected error occurred: {e}")
        time.sleep(60)  # Sleep for 1 minute before retrying


if __name__ == "__main__":
    main()
