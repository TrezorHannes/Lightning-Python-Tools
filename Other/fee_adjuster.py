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
                        }
        return channels_to_modify
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching LNDg channels: {e}")
        raise LNDGAPIError(f"Error fetching LNDg channels: {e}")


# Write to LNDg
def update_lndg_fee(chan_id, new_fee_rate, config):
    lndg_api_url = config["lndg"]["lndg_api_url"]
    update_api_url = f"{lndg_api_url}/api/chanpolicy/"
    username = config["credentials"]["lndg_username"]
    password = config["credentials"]["lndg_password"]
    payload = {"chan_id": chan_id, "fee_rate": new_fee_rate}
    try:
        response = requests.post(
            update_api_url, json=payload, auth=(username, password)
        )
        response.raise_for_status()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logging.info(
            f"{timestamp}: API confirmed changing local fee to {new_fee_rate} for channel {chan_id}"
        )
    except requests.exceptions.RequestException as e:
        logging.error(f"Error updating LNDg fee for channel {chan_id}: {e}")
        raise LNDGAPIError(f"Error updating LNDg fee for channel {chan_id}: {e}")


def write_charge_lnd_file(file_path, pubkey, alias, new_fee_rate):
    with open(file_path, "a") as f:
        f.write(f"[🤖 {alias}]\n")
        f.write(f"node.id = {pubkey}\n")
        f.write("strategy = static\n")
        f.write(f"fee_ppm = {new_fee_rate}\n")
        f.write("min_htlc_msat = 1_000\n")
        f.write("max_htlc_msat_ratio = 0.9\n")
        f.write("\n")


def print_fee_adjustment(
    alias,
    pubkey,
    chan_id,
    capacity,
    local_balance,
    old_fee_rate,
    new_fee_rate,
    group_name,
    fee_base,
    fee_conditions,
    trend_factor,
    amboss_data,
):
    print("-" * 80)
    print(f"Alias: {alias}")
    print(f"Pubkey: {pubkey}")
    print(f"Channel ID: {chan_id}")
    print(f"Local Capacity: {capacity}")
    print(
        f"Local Capacity: {local_balance} | (Outbound: {round(local_balance/capacity*100)}%)"
    )
    print(f"Old Fee Rate: {old_fee_rate}")
    print(f"New Fee Rate: {new_fee_rate}")
    print(f"Delta: {new_fee_rate - old_fee_rate}")
    if group_name:
        print(f"Group: {group_name}")
    else:
        print(f"Group: No Group")
    print(f"Chosen Fee Base: {fee_base}")
    print(f"Fee Conditions: {json.dumps(fee_conditions)}")
    print(f"Trend Factor: {trend_factor}")

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
    while True:
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

                group_adjustment_percentage = (
                    0  # Initialize group_adjustment_percentage
                )
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
                    for chan_id, channel_data in channels_to_modify.items():
                        fee_delta = abs(new_fee_rate - channel_data["local_fee_rate"])
                        # set the fee_delta to X to avoid spamming LN gossip with unnecessary updates
                        if lndg_fee_update_enabled and fee_delta > 10:
                            update_lndg_fee(chan_id, new_fee_rate, config)
                        if terminal_output_enabled:
                            print_fee_adjustment(
                                channel_data["alias"],
                                pubkey,
                                chan_id,
                                channel_data["capacity"],
                                channel_data["local_balance"],
                                channel_data["local_fee_rate"],
                                new_fee_rate,
                                group_name,
                                fee_base,
                                fee_conditions,
                                trend_factor,
                                all_amboss_data,
                            )
                        if write_charge_lnd_file_enabled:
                            charge_lnd_file_path = os.path.join(
                                config["paths"]["charge_lnd_path"], "fee_adjuster.txt"
                            )
                            write_charge_lnd_file(
                                charge_lnd_file_path,
                                pubkey,
                                channel_data["alias"],
                                new_fee_rate,
                            )

                except (AmbossAPIError, LNDGAPIError) as e:
                    logging.error(f"Error processing node {pubkey}: {e}")
                    continue
        except Exception as e:
            logging.error(f"An unexpected error occurred: {e}")
            time.sleep(60)  # Sleep for 1 minute before retrying


if __name__ == "__main__":
    main()
