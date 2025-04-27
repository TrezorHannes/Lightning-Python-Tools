"""
Guidelines for the Fee Adjuster Script

This script automates the adjustment of channel fees based on network conditions, peer behavior,
and local liquidity using data from the Amboss API and LNDg API.

Configuration Settings (see fee_adjuster_config_docs.txt for details):
- base_adjustment_percentage: Percentage adjustment applied to the selected fee base (median, mean, etc.).
- group_adjustment_percentage: Additional adjustment based on node group membership.
- max_cap: Maximum allowed fee rate (in ppm).
- trend_sensitivity: Multiplier for the influence of Amboss fee trends on the adjustment.
- fee_base: Statistical measure from Amboss used as the fee calculation base ("median", "mean", etc.).
- groups: Node categories for applying differentiated strategies via group_adjustment_percentage.
- max_outbound: (Optional) Maximum outbound liquidity percentage (0.0-1.0). Script only applies adjustments if channel's outbound liquidity is *below* this value.
- min_outbound: (Optional) Minimum outbound liquidity percentage (0.0-1.0). Script only applies adjustments if channel's outbound liquidity is *above* this value.
- fee_bands: (Optional) Dynamic fee adjustments based on local liquidity.
  - enabled: true/false.
  - discount: Negative percentage adjustment for high local balance (80-100%).
  - premium: Positive percentage adjustment for low local balance (0-40%).
- stuck_channel_adjustment: (Optional) Gradually reduces fees for channels without recent forwards.
  - enabled: true/false.
  - stuck_time_period: Number of days defining one 'stuck period' interval (e.g., 7).

Groups and group_adjustment_percentage:
Allows tailored fee strategies for nodes in specific categories (e.g., "sink", "expensive").

Fee Bands:
Adjusts fees based on local balance ratio, dividing liquidity into 5 bands (0-20%, 20-40%, 40-60%, 60-80%, 80-100%). A graduated adjustment is applied between the configured discount (high local balance) and premium (low local balance). The premium is capped at the 20-40% liquidity band to avoid excessively high fees on nearly drained channels.

Stuck Channel Adjustment:
This feature incrementally reduces fees for channels that haven't forwarded payments recently.
For each multiple of the `stuck_time_period` (in days) that a peer's channels have gone without an *outbound* forwarding, the fee band is moved down by one level (towards the maximum discount).
The adjustment is capped at moving down 4 bands (reaching the maximum discount band).
If an outbound forward is detected for any channel of the peer, the stuck adjustment is reset to 0 bands down.
This adjustment is automatically skipped if the aggregate local liquidity for the peer is below 20%, preventing discounts on heavily imbalanced channels needing rebalancing. The script queries the LNDg API to find the timestamp of the last outbound forward for the peer.

Usage:
- Configure nodes and their settings in `feeConfig.json`.
- Run the script to automatically adjust fees based on configured settings.
- Requires a running LNDg instance for local channel details and fee updates.

Command Line Arguments:
- --debug: Enable detailed debug output, including stuck channel check results.

Charge-lnd Details:
Configure charge-lnd to use the output file:
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
    channel_data,  # Used for liquidity check
    last_forward_timestamp,  # datetime of last outbound forward (or None)
    stuck_time_period,  # configured stuck period in days
    checked_window_days,  # New parameter: The number of days checked back by the API call
):
    """
    Calculate the number of bands to move down based on how long the peer
    has been stuck, applying incremental adjustment per stuck_time_period.

    Args:
        fee_conditions: Dictionary containing stuck channel settings.
        channel_data: Data about the channel (used for liquidity check).
        last_forward_timestamp: datetime object of the last outbound forward, or None.
        stuck_time_period: The configured stuck time period in days.
        checked_window_days: The number of days the API checked back for forwards.

    Returns:
        tuple: (bands_to_move_down: int, days_stuck: int)
               bands_to_move_down is the calculated number of bands (0-4).
               days_stuck is the number of days since the last forward, or
               checked_window_days + 1 if no forward was found in the window.
    """
    stuck_settings = fee_conditions.get("stuck_channel_adjustment", {})
    if not stuck_settings.get("enabled", False):
        # If disabled, days_stuck can be represented as 0 or None, let's use 0 for simplicity
        return 0, 0

    # Skip stuck channel adjustment if local liquidity is too low (below 20%)
    capacity = channel_data.get("capacity", 0)
    local_balance = channel_data.get("local_balance", 0)
    outbound_ratio = (local_balance / capacity) if capacity > 0 else 0

    if outbound_ratio < 0.2:  # Less than 20% local liquidity
        # If liquidity is low, calculate days stuck for logging/notes if timestamp is available,
        # otherwise indicate it's beyond the checked window.
        if last_forward_timestamp is None:
            # No forward in the window, and low liquidity skips adjustment
            days_stuck = checked_window_days + 1  # Indicate beyond the window
        else:
            # Forward found, but low liquidity skips adjustment
            days_stuck = (datetime.now() - last_forward_timestamp).days

        return 0, days_stuck  # Return 0 bands down if liquidity is low

    if last_forward_timestamp is None:
        # No outbound forward found within the checked_window_days.
        # Calculate bands based on the minimum possible stuck duration beyond the window,
        # and represent days stuck as beyond the window.
        days_stuck = checked_window_days + 1  # Indicate beyond the window

        # Since no forward was found in the window, the channel is stuck for at least checked_window_days.
        # Calculate how many stuck_time_period intervals fit into checked_window_days.
        calculated_bands_to_move_down = checked_window_days // stuck_time_period
        # If checked_window_days is exactly a multiple, it means it's stuck for that many periods.
        # If it's more, it's stuck for more periods. We add 1 to ensure we count the *start*
        # of the interval just passed.
        # Example: stuck_period=7, checked_window=12. days_stuck would be 13.
        # calculated_bands_to_move_down = 12 // 7 = 1. This is correct for days 7-13.
        # If checked_window=14, days_stuck would be 15. 14 // 7 = 2. Correct for days 14-20.
        # This logic seems sound for calculating minimum bands down when timestamp is None.

    else:
        # Outbound forward found within the checked_window_days.
        time_difference = datetime.now() - last_forward_timestamp
        days_stuck = time_difference.days

        # Calculate bands to move down based on actual days stuck.
        calculated_bands_to_move_down = days_stuck // stuck_time_period

    # Cap the total bands moved down at 4 (as Band 4 is the furthest premium band)
    bands_to_move_down = min(calculated_bands_to_move_down, 4)  # Cap at 4 bands down

    logging.debug(
        f"Stuck calculation (within {checked_window_days}d window): days_stuck={days_stuck}, stuck_period={stuck_time_period}, calculated_bands={calculated_bands_to_move_down}, final_bands_to_move_down={bands_to_move_down}"
    )

    return bands_to_move_down, days_stuck


def calculate_fee_band_adjustment(
    fee_conditions,
    outbound_ratio,
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
    fee_conditions,  # Keep fee_conditions to access stuck settings
    outbound_ratio,
    initial_raw_band,  # Pass initial raw band for notes clarity
    fee_band_factor,  # Pass the factor if needed for debug in notes (optional)
    config,
    node_definitions,
    new_fee_rate=None,
    stuck_bands_to_move_down=0,  # Renamed parameter
    days_stuck=None,  # New parameter: days since last forward
    is_low_liquidity_for_stuck=False,  # New parameter: flag if low liquidity skipped
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
        stuck_notes_part = (
            f"\nStuck adjust: {'✅' if stuck_bands_to_move_down > 0 else '❌'}"
        )
        stuck_notes_part += f" | Period: {stuck_period}d"
        if days_stuck is not None:
            stuck_notes_part += f" | Stuck: {days_stuck}d"
        if is_low_liquidity_for_stuck:
            stuck_notes_part += " (Low Liq Skip)"
        if stuck_bands_to_move_down > 0:
            stuck_notes_part += f" | Bands Down: {stuck_bands_to_move_down}"

        notes += stuck_notes_part

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
    initial_raw_band,  # Initial band based on liquidity
    stuck_adj_bands_applied,  # How many bands were moved down due to stuck status
    final_raw_band,  # Final band used for adjustment calculation
    fee_band_adj_pct,  # The resulting percentage adjustment from fee bands + stuck
    # Stuck Context # Add these new parameters
    stuck_adj_enabled,
    stuck_period,
    peer_stuck_status,  # Now a detailed status string
    is_low_liquidity_for_stuck,  # Flag indicating if low liquidity skipped adjustment
    days_stuck,  # Days since last outbound forward
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
        print(f"    - Days Stuck: {days_stuck} days")  # Print actual days stuck
        print(f"    - Peer Status: {peer_stuck_status}")  # Print detailed status
        if is_low_liquidity_for_stuck:
            print(
                f"    - Adjustment Skipped: Low Liquidity ({outbound_ratio*100:.1f}% < 20%)"
            )
        elif stuck_adj_bands_applied > 0:
            print(f"    - Adjustment Applied: {stuck_adj_bands_applied} bands down")
        else:
            # This case means stuck is enabled, not low liquidity, but 0 bands applied.
            # This happens if days_stuck < stuck_period.
            print(f"    - Adjustment Applied: 0 bands (Days stuck < Period)")

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
                            f"\033[91mPeer {pubkey} (Alias: {alias_to_log}, {num_channels} channels) skipped due to outbound liquidity conditions.\033[0m"
                        )
                        print(
                            f"\033[91mAggregate Ratio: {check_ratio:.2f}, Max: {max_outbound}, Min: {min_outbound}\033[0m"
                        )
                        print()
                    continue  # Skip this entire peer

                # --- Stuck Check & Adjustment Calculation ---
                stuck_bands_to_move_down = 0
                days_stuck = 0  # Initialize days stuck
                last_forward_timestamp = None  # Initialize last forward timestamp

                is_low_liquidity_for_stuck = (
                    check_ratio < 0.2
                )  # Check liquidity for stuck skip

                peer_stuck_status = "N/A (Disabled)"  # Default status for printing

                if stuck_adj_enabled:
                    peer_stuck_status = "Checking..."
                    # Check within the dynamically calculated window
                    # get_last_peer_forwarding_timestamp will log details when --debug is enabled
                    last_forward_timestamp = get_last_peer_forwarding_timestamp(
                        pubkey, stuck_check_window_days, channel_ids_list, config
                    )

                    # Calculate the number of bands to move down based on the check result
                    bands_calc_result = calculate_stuck_channel_band_adjustment(
                        fee_conditions,
                        first_channel_data,  # Pass channel_data for liquidity check inside func
                        last_forward_timestamp,  # Use the timestamp from the check
                        stuck_period,
                        stuck_check_window_days,  # Pass the checked_window_days to the function
                    )
                    stuck_bands_to_move_down, days_stuck = (
                        bands_calc_result  # Unpack the two values
                    )

                    # Determine the final peer status string for printing/notes
                    if last_forward_timestamp is not None:
                        peer_stuck_status = "Active"
                    elif is_low_liquidity_for_stuck:
                        peer_stuck_status = "Stuck (Low Liquidity Skip)"
                    else:
                        # If stuck_adj_enabled, not low liquidity, and no timestamp found,
                        # it's stuck beyond the check window. days_stuck is already set
                        # to checked_window_days + 1 in calculate_stuck_channel_band_adjustment.
                        peer_stuck_status = f"Stuck (>{stuck_check_window_days} days, {stuck_bands_to_move_down} bands down)"

                # Add the debug print block here
                if show_debug_output and stuck_adj_enabled:
                    print("-" * 80)  # Use separator like in main output
                    print(f"--- Stuck Check Debug for Peer {pubkey[:10]}... ---")
                    print(f"  Configured stuck_time_period: {stuck_period} days")
                    print(f"  API Check Window: {stuck_check_window_days} days")
                    print(
                        f"  Last outbound forward timestamp found: {last_forward_timestamp if last_forward_timestamp else 'None'}"
                    )
                    print(
                        f"  Calculated days since last forward: {days_stuck}"
                    )  # This will now show >window_size if stuck
                    print(f"  Aggregate Outbound Ratio: {check_ratio*100:.1f}%")
                    print(
                        f"  Low liquidity skip check (ratio < 20%): {is_low_liquidity_for_stuck}"
                    )
                    print(f"  Calculated stuck bands down: {stuck_bands_to_move_down}")
                    print(f"  Final Peer Status determination: {peer_stuck_status}")
                    print("--- End Stuck Check Debug ---")
                    sys.stdout.flush()  # Ensure output is shown immediately

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

                # --- Apply Updates & Notes ---
                updated_any_channel = False
                # Loop through channels to apply updates and notes
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
                                is_low_liquidity_for_stuck,  # Pass the low liquidity flag for notes
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
                            peer_stuck_status=peer_stuck_status,  # Pass the status string
                            is_low_liquidity_for_stuck=is_low_liquidity_for_stuck,  # Pass low liquidity flag
                            days_stuck=days_stuck,  # Pass days stuck
                            # Amboss Data
                            amboss_data=all_amboss_data,
                        )
                        sys.stdout.flush()  # Ensure output is shown immediately

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
