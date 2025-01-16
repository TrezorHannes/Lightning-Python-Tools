# What it's solving for: For certain channels, an automated fee-algo needs external data-sources.
# These might be internal datasources like your own database / calculation outputs, or mempool onchain fees,
# but for now we'll focus on the Amboss API.
# We'll then populate, per definition, either
# - a txt file for charge-lnd to pull into your existing fee algo, or
# - write a specific fee to LNDg, if you use that for your fee algo setting
# try to avoid using both, because it'll end up in converging updates

# How?
# 1) Specify a set of pubkeys as groups or define specific node in a jason-file
# 2) Identify incoming fee-metric sources (for now, amboss API)
# 3) Adjust weighting conditions groups for those metrics (eg you want to put more influence on median, or always 10% above minimum, etc)
# 4) Apply fee groups to pubkey groups

# Activate
# Options to either set a cronjob
# 2 * * * * /path/Lightning-Python-Tools/.venv/bin/python3 /path/Lightning-Python-Tools/LNDg/disabled_fee-accelerator.py >/dev/null 2>&1
# Or run a python scheduler via systemd

import os
import requests
from datetime import datetime, timedelta
import configparser
import logging
import json
import argparse
import time
import schedule


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
    nodes_file_path = os.path.join(parent_dir, "..", "fee_adjuster.json")
    with open(nodes_file_path, "r") as f:
        node_definitions = json.load(f)
    return node_definitions


def fetch_amboss_data(pubkey, config, time_ranges=["TODAY"]):
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
            )  # Print the raw response
            if data.get("errors"):
                logging.error(f"Amboss API error for {time_range}: {data['errors']}")
                raise AmbossAPIError(
                    f"Amboss API error for {time_range}: {data['errors']}"
                )
            # Adjusted parsing logic
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


def analyze_fee_trends(all_fee_data):
    # Implement your logic to analyze fee trends here
    # This is a placeholder, you'll need to implement your own logic
    # Example:
    today_median = all_fee_data.get("TODAY", {}).get("median", 0)
    one_week_median = all_fee_data.get("ONE_WEEK", {}).get("median", 0)
    one_month_median = all_fee_data.get("ONE_MONTH", {}).get("median", 0)

    # if today_median > one_week_median and one_week_median > one_month_median:
    #    return 0.05  # Fees are increasing, add 5%
    # elif today_median < one_week_median and one_week_median < one_month_median:
    #    return -0.05  # Fees are decreasing, subtract 5%
    # else:
    return 0  # Fees are stable


def calculate_new_fee_rate(amboss_data, fee_conditions, trend_factor):
    # Ensure median_fee is converted to a float
    median_fee = float(amboss_data.get("TODAY", {}).get("median", 0))
    base_adjustment_percentage = fee_conditions.get("base_adjustment_percentage", 0)
    group_adjustment_percentage = fee_conditions.get("group_adjustment_percentage", 0)
    max_cap = fee_conditions.get("max_cap", 1000)
    trend_sensitivity = fee_conditions.get("trend_sensitivity", 1)

    adjusted_base_percentage = base_adjustment_percentage + (
        trend_factor * trend_sensitivity
    )

    new_fee_rate = (
        median_fee * (1 + adjusted_base_percentage) * (1 + group_adjustment_percentage)
    )
    new_fee_rate = min(new_fee_rate, max_cap)
    return round(new_fee_rate)


def get_channels_to_modify(pubkey, config):
    api_url = f"http://localhost:8889/api/channels?limit=1500"
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


# def update_lndg_fee(chan_id, new_fee_rate, config):
#     update_api_url = "http://localhost:8889/api/chanpolicy/"
#     username = config["credentials"]["lndg_username"]
#     password = config["credentials"]["lndg_password"]
#     payload = {"chan_id": chan_id, "fee_rate": new_fee_rate}
#     try:
#         response = requests.post(
#             update_api_url, json=payload, auth=(username, password)
#         )
#         response.raise_for_status()
#         timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
#         logging.info(
#             f"{timestamp}: API confirmed changing local fee to {new_fee_rate} for channel {chan_id}"
#         )
#     except requests.exceptions.RequestException as e:
#         logging.error(f"Error updating LNDg fee for channel {chan_id}: {e}")
#         raise LNDGAPIError(f"Error updating LNDg fee for channel {chan_id}: {e}")


def write_charge_lnd_file(node_data, file_path):
    with open(file_path, "w") as f:
        for chan_id, new_fee_rate in node_data:
            f.write(f"{chan_id}={new_fee_rate}\n")


def print_fee_adjustment(
    pubkey, chan_id, old_fee_rate, new_fee_rate, groups, fee_conditions, trend_factor
):
    print(f"Pubkey: {pubkey}")
    print(f"Channel ID: {chan_id}")
    print(f"Old Fee Rate: {old_fee_rate}")
    print(f"New Fee Rate: {new_fee_rate}")
    print(f"Delta: {new_fee_rate - old_fee_rate}")
    print(f"Groups: {', '.join(groups)}")
    print(f"Fee Conditions: {json.dumps(fee_conditions)}")
    print(f"Trend Factor: {trend_factor}")
    print("-" * 30)


def main():
    parser = argparse.ArgumentParser(description="Automated Fee Adjuster")
    parser.add_argument(
        "--scheduler", action="store_true", help="Run the script with the scheduler"
    )
    args = parser.parse_args()

    config = load_config()
    node_definitions = load_node_definitions()

    for node in node_definitions["nodes"]:
        pubkey = node["pubkey"]
        groups = node["groups"]
        fee_conditions = node["fee_conditions"]
        try:
            all_amboss_data = fetch_amboss_data(pubkey, config)
            print(f"Amboss Data: {all_amboss_data}")  # Add this line
            if not all_amboss_data:  # Check if all_amboss_data is empty
                logging.warning(
                    f"No Amboss data found for pubkey {pubkey}. Skipping fee adjustment."
                )
                continue  # Skip to the next node
            trend_factor = analyze_fee_trends(all_amboss_data)
            new_fee_rate = calculate_new_fee_rate(
                all_amboss_data, fee_conditions, trend_factor
            )
            channels_to_modify = get_channels_to_modify(pubkey, config)
            for chan_id, channel_data in channels_to_modify.items():
                # update_lndg_fee(chan_id, new_fee_rate, config)
                print_fee_adjustment(
                    pubkey,
                    chan_id,
                    channel_data["local_fee_rate"],
                    new_fee_rate,
                    groups,
                    fee_conditions,
                    trend_factor,
                )
            # Example of writing to charge-lnd file
            # charge_lnd_file_path = os.path.join(config['paths']['charge_lnd_path'], f'fee_adjuster_{pubkey}.txt')
            # write_charge_lnd_file(channels_to_modify, charge_lnd_file_path)
        except (AmbossAPIError, LNDGAPIError) as e:
            logging.error(f"Error processing node {pubkey}: {e}")
            continue

    if args.scheduler:
        schedule.every(1).hour.do(main)
        while True:
            schedule.run_pending()
            time.sleep(60)


if __name__ == "__main__":
    main()
