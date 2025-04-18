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
import argparse

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
# Define the aggregate threshold from config
aggregate_liquidity_threshold = float(config["parameters"]["agg_liquidity_threshold"])


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


def get_channels_to_modify(verbose=False):
    channels_to_modify = []  # Initialize the list to return
    peers_data = {}  # Dictionary to group channels by remote_pubkey

    try:
        # Make the API request with authentication
        response = requests.get(api_url, auth=(username, password))
        response.raise_for_status()  # Raise HTTPError for bad responses (4xx or 5xx)
        data = response.json()

        if "results" not in data:
            logging.warning("No 'results' key found in API response.")
            return []

        # --- 1. Group channels by remote_pubkey ---
        for result in data["results"]:
            remote_pubkey = result.get("remote_pubkey")
            if not remote_pubkey:
                logging.debug(
                    f"Skipping channel result without remote_pubkey: {result.get('chan_id', 'N/A')}"
                )
                continue

            # Basic filtering: only consider active, open channels for aggregation
            if not result.get("is_active") or not result.get("is_open"):
                logging.debug(
                    f"Skipping inactive/closed channel {result.get('chan_id')} for peer {remote_pubkey}"
                )
                continue

            if remote_pubkey not in peers_data:
                peers_data[remote_pubkey] = []
            peers_data[remote_pubkey].append(result)

        # --- 2. Process each peer group ---
        for remote_pubkey, channels_list in peers_data.items():
            num_channels = len(channels_list)
            alias = channels_list[0].get("alias", "N/A")  # Use first alias for logging

            # --- 3. Basic Peer Filtering ---
            if remote_pubkey in ignore_remote_pubkeys:
                logging.info(
                    f"Ignoring peer {remote_pubkey} ({alias}, {num_channels} channels) based on ignore list."
                )
                continue

            # --- 4. Calculate Aggregate Stats & Check Flags ---
            total_capacity = 0
            total_local_balance = 0
            oldest_fees_updated_datetime = datetime.now()  # Initialize to now
            lowest_local_fee_rate = float("inf")
            any_initiator = False
            all_active = True  # Assume true until proven otherwise
            all_open = True  # Assume true until proven otherwise
            any_local_disabled = False
            any_remote_disabled = False
            any_auto_fees = False
            any_auto_rebalance = False

            for channel in channels_list:
                total_capacity += channel.get("capacity", 0)
                total_local_balance += channel.get("local_balance", 0)
                any_initiator = any_initiator or channel.get("initiator", False)
                all_active = all_active and channel.get(
                    "is_active", False
                )  # Re-check, although pre-filtered
                all_open = all_open and channel.get(
                    "is_open", False
                )  # Re-check, although pre-filtered
                any_local_disabled = any_local_disabled or channel.get(
                    "local_disabled", False
                )
                any_remote_disabled = any_remote_disabled or channel.get(
                    "remote_disabled", False
                )
                any_auto_fees = any_auto_fees or channel.get("auto_fees", False)
                any_auto_rebalance = any_auto_rebalance or channel.get(
                    "auto_rebalance", False
                )

                # Find lowest fee rate in the group
                current_fee = channel.get("local_fee_rate", float("inf"))
                if current_fee < lowest_local_fee_rate:
                    lowest_local_fee_rate = current_fee

                # Find the oldest fee update time
                fees_updated_str = channel.get("fees_updated")
                if fees_updated_str:
                    try:
                        channel_fees_updated = datetime.strptime(
                            fees_updated_str, "%Y-%m-%dT%H:%M:%S.%f"
                        )
                        if channel_fees_updated < oldest_fees_updated_datetime:
                            oldest_fees_updated_datetime = channel_fees_updated
                    except ValueError:
                        logging.warning(
                            f"Could not parse fees_updated '{fees_updated_str}' for chan {channel.get('chan_id')}"
                        )
                        # Decide how to handle - maybe skip peer or use default? Let's continue for now.

            # Calculate aggregate ratio
            aggregate_local_balance_ratio = (
                (total_local_balance / total_capacity * 100)
                if total_capacity > 0
                else 0
            )

            # Calculate time difference from the oldest update in the group
            time_difference = datetime.now() - oldest_fees_updated_datetime
            fees_timing_condition = time_difference > timedelta(
                hours=float(fee_updated_hours_ago)
            )

            # --- 5. Apply Core Peer-Level Conditions ---
            if (
                aggregate_local_balance_ratio
                < aggregate_liquidity_threshold  # Use the variable read from config
                and fees_timing_condition
                # ... (rest of the conditions) ...
            ):
                logging.info(
                    f"Peer {remote_pubkey} ({alias}, {num_channels} channels) meets aggregate conditions for potential fee bump. Aggregate local: {aggregate_local_balance_ratio:.1f}% < {aggregate_liquidity_threshold}%."  # Use variable in log
                )

                # ... (rest of the logic inside the if block) ...

            else:
                logging.debug(
                    f"Peer {remote_pubkey} ({alias}) does not meet aggregate conditions for fee bump."
                )

            # Log peer info before applying conditions for better debugging
            logging.debug(
                f"Peer: {remote_pubkey} ({alias}, {num_channels} chans), AggCap: {total_capacity}, AggLocal: {total_local_balance} ({aggregate_local_balance_ratio:.1f}%), "
                f"LowestFee: {lowest_local_fee_rate}, OldestUpdate: {oldest_fees_updated_datetime}, FeesTimingOK: {fees_timing_condition}, "
                f"AnyInitiator: {any_initiator}, AnyLocalDisabled: {any_local_disabled}, AnyRemoteDisabled: {any_remote_disabled}, "
                f"AnyAutoFees: {any_auto_fees}, AnyAutoRebal: {any_auto_rebalance}, AllActiveOpen: {all_active and all_open}"
            )

            if (
                aggregate_local_balance_ratio
                < aggregate_liquidity_threshold  # Key condition: Low aggregate local liquidity
                and fees_timing_condition  # Enough time passed since last update in the group
                and any_initiator  # At least one channel was locally initiated
                and all_active  # All channels in group are active
                and all_open  # All channels in group are open
                and not any_remote_disabled  # Peer hasn't disabled any channel towards us
                and any_auto_fees  # At least one channel has auto-fees (implies LNDg manages it)
                and any_auto_rebalance  # At least one channel has auto-rebalance
            ):
                logging.info(
                    f"Peer {remote_pubkey} ({alias}, {num_channels} channels) meets aggregate conditions for potential fee bump. Aggregate local: {aggregate_local_balance_ratio:.1f}% < {aggregate_liquidity_threshold}%."
                )

                # --- 6. Calculate New Fee Rate (based on lowest fee in the group) ---
                if lowest_local_fee_rate == float("inf"):
                    logging.warning(
                        f"Could not determine lowest fee rate for peer {remote_pubkey}. Skipping bump."
                    )
                    continue

                local_new_fee_rate = calculate_new_fee_rate(lowest_local_fee_rate)

                if local_new_fee_rate >= int(capped_ceiling):
                    logging.info(
                        f"Skipping fee bump for peer {remote_pubkey}. Calculated new fee {local_new_fee_rate} meets/exceeds ceiling {capped_ceiling}. Lowest current fee was {lowest_local_fee_rate}."
                    )
                    continue

                # --- 7. Add All Channels of the Peer to Modify List ---
                logging.info(
                    f"Applying new fee rate {local_new_fee_rate} to all {num_channels} channels for peer {remote_pubkey} (based on lowest current fee {lowest_local_fee_rate})."
                )

                # --- VERBOSE OUTPUT ---
                if verbose:
                    try:
                        table = PrettyTable()
                        table.field_names = [
                            "Peer Alias",
                            "Pubkey",
                            "# Channels",
                            "Agg Local %",
                            "Lowest Fee",
                            "New Fee",
                            "Oldest Update",
                        ]
                        table.add_row(
                            [
                                alias,
                                remote_pubkey,
                                num_channels,
                                f"{aggregate_local_balance_ratio:.1f}%",
                                lowest_local_fee_rate,
                                local_new_fee_rate,
                                oldest_fees_updated_datetime.strftime("%Y-%m-%d %H:%M"),
                            ]
                        )
                        print("\n--- Proposed Fee Bump ---")
                        print(table)
                    except Exception as table_err:
                        print(
                            f"Error generating verbose table for {remote_pubkey}: {table_err}"
                        )
                # --- END VERBOSE OUTPUT ---

                for channel in channels_list:
                    chan_id = channel.get("chan_id")
                    if chan_id:
                        channels_to_modify.append((chan_id, local_new_fee_rate))

            else:
                logging.debug(
                    f"Peer {remote_pubkey} ({alias}) does not meet aggregate conditions for fee bump."
                )

    except requests.exceptions.RequestException as e:
        logging.error(f"API request failed: {e}")
    except Exception as e:
        logging.exception(
            f"An unexpected error occurred in get_channels_to_modify: {e}"
        )  # Log full traceback

    logging.info(
        f"Found {len(channels_to_modify)} individual channel updates based on peer aggregation."
    )
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
    # --- Argument Parsing ---
    parser = argparse.ArgumentParser(
        description="Accelerate fees for depleted channels based on peer aggregate liquidity."
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",  # Sets args.verbose to True if flag is present
        help="Print proposed fee bumps to the terminal.",
    )
    args = parser.parse_args()
    # --- End Argument Parsing ---

    # Pass the verbose flag to the function
    channels_to_modify = get_channels_to_modify(verbose=args.verbose)

    if channels_to_modify:
        # Add a confirmation step if verbose
        if args.verbose:
            confirm = input(
                f"Proceed with updating fees for {len(channels_to_modify)} channels? (y/N): "
            )
            if confirm.lower() != "y":
                print("Aborting.")
                exit()
        modify_channels(channels_to_modify)
    elif args.verbose:
        print("\nNo channels met the criteria for fee acceleration.")
