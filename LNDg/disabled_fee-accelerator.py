# What it's solving for: charge-lnd disables my local initiator channels once liquidity is < 5%
# This causes incoming HTLCs towards that channel to stop (which is desired), however, since
# LNDg uses incoming HTLCs as signal for fee-adjustments, worst case the channel is forever stuck
# and can't get rebalanced.
# This script can be executed via cronjob every X hours to bump the local_fee_rate in LNDg

# 3 functions:
# i) Calculate a fee bump which is slower pacing slope as higher the fee is.
# ii) Pull all disabled and local initiated, active, but not remotely disabled channel list and local_fee_rate
# iii) add X % (minimum 10 sat) to our local fee and log the changes in ../logs/lndg-fee-accelerator.log

# Cronjob
# 2 * * * * /path/Lightning-Python-Tools/.venv/bin/python3 /path/Lightning-Python-Tools/LNDg/disabled_fee-accelerator.py >/dev/null 2>&1

import os
import requests
from datetime import datetime, timedelta
import configparser
import logging  # For more structured debugging
from prettytable import PrettyTable

# Get the path to the parent directory
parent_dir = os.path.dirname(os.path.abspath(__file__))

# Construct the path to the config.ini file
config_file_path = os.path.join(parent_dir, "..", "config.ini")
config = configparser.ConfigParser()
config.read(config_file_path)

# only update if the fee update is > 24 hrs ago
fee_updated_hours_ago = config["parameters"]["fee_updated_hours_ago"]
# stop increasing fee at ppm
capped_ceiling = config["parameters"]["capped_ceiling"]

# API endpoint URL for retrieving channels
api_url = config["lndg"]["lndg_api_url"] + "/api/channels?limit=1500"

# API endpoint URL for updating channels
update_api_url = config["lndg"]["lndg_api_url"] + "/api/chanpolicy/"

# Authentication credentials
username = config["credentials"]["lndg_username"]
password = config["credentials"]["lndg_password"]

# File path for the log file
log_file_path = os.path.join(parent_dir, "..", "logs", "lndg-fee-accelerator.log")

# Logfile definition
logging.basicConfig(filename=log_file_path, level=logging.DEBUG)

# Remote pubkey to ignore. Add pubkey or reference in config.ini if you want to use it.
ignore_remote_pubkeys = config["pubkey"]["base_fee_ignore"].split(",")


def calculate_new_fee_rate(local_fee_rate):
    # Convert local_fee_rate to an integer
    local_fee_rate = int(local_fee_rate)
    # Define thresholds and increments
    lower_threshold = 200
    upper_threshold = int(capped_ceiling)
    max_increment_percentage = 0.03
    min_increment = 10

    if local_fee_rate <= lower_threshold:
        increment_percentage = max_increment_percentage
    elif local_fee_rate >= upper_threshold:
        increment_percentage = min_increment / local_fee_rate
    else:
        # decrease the increment percentage between lower and upper thresholds
        slope = (min_increment / upper_threshold - max_increment_percentage) / (
            upper_threshold - lower_threshold
        )
        increment_percentage = max_increment_percentage + slope * (
            local_fee_rate - lower_threshold
        )

    # Calculate the increment based on the dynamic percentage
    increment = max(min_increment, local_fee_rate * increment_percentage)
    local_new_fee_rate = min(local_fee_rate + increment, upper_threshold)

    return round(local_new_fee_rate)


def get_channels_to_modify():
    channels_to_modify = []  # Initialize the list
    try:
        # Make the API request with authentication
        response = requests.get(api_url, auth=(username, password))

        # Check if the request was successful (status code 200)
        if response.status_code == 200:
            data = response.json()
            if "results" in data:
                results = data["results"]
                for result in results:
                    remote_pubkey = result.get("remote_pubkey", "")
                    local_fee_rate = result.get("local_fee_rate", 0)
                    capacity = result.get("capacity", 0)
                    initiator = result.get("initiator", False)
                    is_active = result.get("is_active", False)
                    local_disabled = result.get("local_disabled", False)
                    remote_disabled = result.get("remote_disabled", False)
                    fees_updated = result.get("fees_updated", "")
                    ar_out_target = result.get("ar_out_target", 0)
                    ar_in_target = result.get("ar_in_target", 0)
                    auto_fees = result.get("auto_fees", False)
                    auto_rebalance = result.get("auto_rebalance", False)
                    is_open = result.get("is_open", False)
                    chan_id = result.get("chan_id", "")
                    alias = result.get("alias", "")

                    # Parse the fees_updated string into a datetime object and calc the difference
                    fees_updated_datetime = datetime.strptime(
                        fees_updated, "%Y-%m-%dT%H:%M:%S.%f"
                    )
                    time_difference = datetime.now() - fees_updated_datetime

                    local_new_fee_rate = calculate_new_fee_rate(local_fee_rate)
                    # Check a few conditions upfront
                    if time_difference > timedelta(hours=float(fee_updated_hours_ago)):
                        fees_timing_condition = True
                    else:
                        fees_timing_condition = False

                    if remote_pubkey in ignore_remote_pubkeys and is_active:
                        logging.info(
                            f"Ignoring channel {chan_id} with {alias} and current fee-rate of {local_fee_rate}"
                        )
                    else:
                        # add more filters into the if condition to losen or tighten which channels you want to update periodically
                        # eg if capacity >= 5000000 will only add channels with more than 5M

                        # furthermore, the local_disabled is a custom setup I run since charge-lnd disables local initiator channels
                        # with < 5% local liquidity available.
                        if (
                            initiator
                            and is_active
                            and local_disabled
                            and is_open
                            and ar_in_target < int(95)
                            and auto_fees
                            and auto_rebalance
                            and fees_timing_condition
                            and local_new_fee_rate < int(capped_ceiling)
                            and ar_out_target > int(50)
                            and not remote_disabled
                        ):

                            logging.info(
                                f"Processing channel for {alias} - current fee: {local_fee_rate}, new fee: {local_new_fee_rate}, is_open: {is_open}"
                            )

                            # uncomment in case you want a one-off table overview of changes
                            """
                            local_balance = result.get('local_balance', '')
                            local_balance_ratio = (local_balance / capacity) * 100
                            table = PrettyTable()
                            table.field_names = ["Alias", "Is Active", "Capacity", "Local Balance", "Local PPM", "AR Out Target", "Auto Rebalance", "New Fee Rate"]
                            table.add_row([alias, is_active, capacity, f"{local_balance_ratio:.2f}%", local_fee_rate, ar_out_target, auto_rebalance, local_new_fee_rate])
                            print(table)
                            """

                            channels_to_modify.append((chan_id, local_new_fee_rate))
        else:
            logging.error(
                f"API request failed with status code: {response.status_code}"
            )

    except Exception as e:
        logging.exception(f"Error: {e}")

    return channels_to_modify


def modify_channels(channels):
    try:
        for chan_id, local_new_fee_rate in channels:
            # Define the payload to update the channel
            # Definition of fields: https://github.com/cryptosharks131/lndg/blob/a01fb26b0c67587b62312615236482c2c9610aa4/gui/serializers.py#L128-L135
            payload = {"chan_id": chan_id, "fee_rate": local_new_fee_rate}

            # Make a POST request to update the channel details via chanpolicy (not possible via channels-PUT)
            response = requests.post(
                update_api_url, json=payload, auth=(username, password)
            )

            # Get the current timestamp
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if response.status_code == 200:
                # Log the changes
                logging.info(
                    f"{timestamp}: API confirmed changing local fee to {local_new_fee_rate} for channel {chan_id}\n"
                )
            else:
                logging.error(
                    f"{timestamp}: Failed to update channel {chan_id}: Status Code {response.status_code}"
                )

    except Exception as e:
        logging.exception(f"Error modifying channels: {e}")


if __name__ == "__main__":
    channels_to_modify = get_channels_to_modify()
    if channels_to_modify:
        modify_channels(channels_to_modify)
