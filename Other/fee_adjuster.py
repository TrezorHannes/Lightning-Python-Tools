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


Groups and group_adjustment_percentage:
Nodes can belong to multiple groups, such as "sink" or "expensive". The group_adjustment_percentage is applied to nodes based on their group membership, allowing for tailored fee strategies. For example, nodes in the "expensive" group might have higher fees to reflect their role in the network.

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


def calculate_new_fee_rate(
    amboss_data,
    fee_conditions,
    trend_factor,
    base_adjustment_percentage,
    group_adjustment_percentage,
):
    fee_base = fee_conditions.get("fee_base", "median")
    # Ensure the selected fee base is converted to a float
    base_fee = float(amboss_data.get("TODAY", {}).get(fee_base, 0))
    max_cap = fee_conditions.get("max_cap", 1000)
    trend_sensitivity = fee_conditions.get("trend_sensitivity", 1)

    adjusted_base_percentage = base_adjustment_percentage + (
        trend_factor * trend_sensitivity
    )

    new_fee_rate = (
        base_fee * (1 + adjusted_base_percentage) * (1 + group_adjustment_percentage)
    )
    new_fee_rate = min(new_fee_rate, max_cap)
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
                new_fee_rate = calculate_new_fee_rate(
                    all_amboss_data,
                    fee_conditions,
                    trend_factor,
                    base_adjustment_percentage,
                    group_adjustment_percentage,
                )
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
