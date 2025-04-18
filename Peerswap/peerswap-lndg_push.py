# .venv/bin/python Peerswap/peerswap-lndg_push.py -h for help

import datetime
import os
import requests
import json
import subprocess
import logging  # For more structured debugging
import configparser
import argparse
import locale

locale.setlocale(locale.LC_ALL, "")

# Get the path to the parent directory
parent_dir = os.path.dirname(os.path.abspath(__file__))

# Construct the path to the config.ini file
config_file_path = os.path.join(parent_dir, "..", "config.ini")
config = configparser.ConfigParser()
config.read(config_file_path)

# pscli path and get peers information
pscli_path = config["paths"]["pscli_path"]
pscli_command = [pscli_path, "listpeers"]

# lncli path from the config file
lncli_path = config["paths"]["lncli_path"]

# LNDg API credentials and endpoints
username = config["credentials"]["lndg_username"]
password = config["credentials"]["lndg_password"]

lndg_api_url = config["lndg"]["lndg_api_url"] + "/api/channels"

# File path for the log file
log_file_path = os.path.join(parent_dir, "..", "logs", "peerswap-LNDg_changes.log")

# Logfile definition
logging.basicConfig(filename=log_file_path, level=logging.DEBUG)


def get_lncli_listchannels_output():
    result = subprocess.run([lncli_path, "listchannels"], stdout=subprocess.PIPE)
    channels_data = json.loads(result.stdout)
    return channels_data["channels"]


def find_alias_by_chan_id(chan_id):
    channels_data = get_lncli_listchannels_output()
    for channel in channels_data:
        if channel["chan_id"] == chan_id:
            return channel["peer_alias"]
    return None


# Function to get peers information from peerswap
def get_peerswap_info():
    try:
        # Run the pscli command and capture the output - needs to be in path
        result = subprocess.run(
            pscli_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        try:
            peers_info = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON: {e}")
            return None
        if result.returncode == 0:
            peers_info = json.loads(result.stdout)
            formatted_info = []
            for peer in peers_info["peers"]:
                if peer.get("channels"):  # Check if the 'channels' list exists
                    channel_id = peer["channels"][0]["channel_id"]
                else:
                    channel_id = ""  # when there's no channel

                swaps_allowed = peer["swaps_allowed"]
                supported_assets = ", ".join(peer["supported_assets"])
                swaps_out = sum(
                    int(ch["swaps_out"])
                    for ch in [peer["as_sender"], peer["as_receiver"]]
                )
                swaps_in = sum(
                    int(ch["swaps_in"])
                    for ch in [peer["as_sender"], peer["as_receiver"]]
                )
                sats_out = sum(
                    int(ch["sats_out"])
                    for ch in [peer["as_sender"], peer["as_receiver"]]
                )
                sats_in = sum(
                    int(ch["sats_in"])
                    for ch in [peer["as_sender"], peer["as_receiver"]]
                )

                paid_fee = int(peer["paid_fee"])

                new_notes = (
                    f"Swaps Allowed: {swaps_allowed} with {supported_assets}\n"
                    f"⚡Swap-ins: {locale.format_string('%d', sats_in, grouping=True)}sats with {swaps_in} swaps \n"
                    f"⚡Swap-outs: {locale.format_string('%d', sats_out, grouping=True)}sats with {swaps_out} swaps \n"
                    f"Paid fee: {paid_fee}"
                )

                formatted_info.append(
                    (channel_id, new_notes)
                )  # Append individual peer objects
            return formatted_info
        else:
            logging.error(f"Error running pscli: {result.stderr}")
            return None
    except Exception as e:
        print(f"Error: {e}")
        logging.error(f"Error: {e}")

        return None


# Filters channels based on pscli output and deletes notes for non-existent channels
def filter_and_delete_notes(peers_info):

    pscli_channel_ids = [peer_info[0] for peer_info in peers_info if peer_info]

    for channel in get_lncli_listchannels_output():
        channel_id = channel["chan_id"]
        if channel_id not in pscli_channel_ids:
            current_notes = get_current_notes(channel_id)
            if current_notes and current_notes.startswith("Swaps Allowed"):
                update_notes(channel_id, "")  # Clear the notes
                print(
                    f"Deleted notes for channel {channel_id}. Is their daemon running?"
                )
                logging.info(
                    f"Deleted notes channel {channel_id} Is their daemon running?"
                )


def get_current_timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# Function to get current notes from LNDg API
def get_current_notes(channel_id):
    # Use the base API URL and append the specific channel ID
    api_url = f"{lndg_api_url}/{channel_id}/"

    try:
        # Make the API request with authentication
        response = requests.get(api_url, auth=(username, password))

        # Check if the request was successful (status code 200)
        if response.status_code == 200:
            data = response.json()
            # Extract the 'notes' field from the JSON response
            notes = data.get("notes", "")
            return notes
        else:
            logging.error(
                f"API request for channel {channel_id} failed with status code: {response.status_code}"
            )
            return None

    except Exception as e:
        logging.error(f"Error retrieving notes for channel {channel_id}: {e}")
        return None


# Function to update notes on LNDg API
def update_notes(channel_id, notes):
    global lndg_api_url
    payload = {"chan_id": channel_id, "notes": notes}
    try:
        response = requests.put(
            f"{lndg_api_url}/{channel_id}/", json=payload, auth=(username, password)
        )

        timestamp = get_current_timestamp()
        """
        logging.debug(f"Channel-ID: {channel_id}")
        logging.debug(f"Payload: {payload}")
        logging.debug(f"API Response: {response.text}")
        logging.debug(f"API Status Code: {response.status_code}")
        logging.debug(f"Timestamp: {timestamp}")
        """
        if response.status_code == 200:
            with open(log_file_path, "a") as log_file:
                log_file.write(f"{timestamp}: Updated notes for channel {channel_id}\n")
        else:
            logging.error(
                f"{timestamp}: Failed to update notes for channel {channel_id}: Status Code {response.status_code}"
            )

    except Exception as e:
        logging.error(f"Error updating notes for channel {channel_id}: {e}")


def main():
    channels_data = get_lncli_listchannels_output()
    peers_info = get_peerswap_info()

    parser = argparse.ArgumentParser(
        description="Script to update notes for LNDg channels."
    )
    parser.add_argument(
        "-o",
        "--overwrite",
        action="store_true",
        help="Overwrite existing notes with new notes.",
    )
    parser.add_argument(
        "-a",
        "--append",
        action="store_true",
        help="Append new notes to existing notes.",
    )
    parser.add_argument(
        "-d",
        "--delete",
        action="store_true",
        help="Delete swap notes from channels not offering PS anymore.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose mode."
    )
    args = parser.parse_args()

    if args.delete:
        filter_and_delete_notes(peers_info)

    if peers_info:

        for channel_id, new_notes in peers_info:
            alias = find_alias_by_chan_id(channel_id)

            action = None

            if args.overwrite:
                action = "o"
            elif args.append:
                action = "a"

            current_notes = get_current_notes(channel_id)

            if args.verbose:
                if action == "o":
                    print(
                        "============================================================================"
                    )
                    print(f"{alias} with channel {channel_id}.")
                    print(
                        f"Overwriting the existing notes with new notes:\n{new_notes}."
                    )
                elif action == "a":
                    print(
                        "============================================================================"
                    )
                    print(f"{alias} with channel {channel_id}.")
                    print(
                        f"Appending the existing notes with new notes:\n{current_notes}\n{new_notes}."
                    )

            if action == "o":
                update_notes(channel_id, new_notes)
            elif action == "a":
                if not current_notes or current_notes.startswith("Swaps Allowed"):
                    update_notes(channel_id, new_notes)
                else:
                    update_notes(channel_id, current_notes + "\n" + new_notes)
            else:
                # Add automatic overwrite if current_notes start with "Swaps Allowed"
                if not current_notes or current_notes.startswith("Swaps Allowed"):
                    update_notes(channel_id, new_notes)

                else:
                    print(
                        "============================================================================"
                    )
                    print(
                        f"Existing notes stored in LNDg for {alias} Channel {channel_id}:\n{current_notes}"
                    )
                    action = input(
                        "Do you want to overwrite the existing notes (o) or append the new notes (a)? (o/a): "
                    )
                    if action.lower() == "o":
                        print(
                            f"Overwriting the existing notes with new notes:\n{new_notes}."
                        )
                        update_notes(channel_id, new_notes)
                    elif action.lower() == "a":
                        print(
                            f"Appending the existing notes with new notes:\n{current_notes}\n{new_notes}."
                        )
                        update_notes(channel_id, current_notes + "\n" + new_notes)
                    else:
                        print(f"Invalid action. Skipping update for this channel.")


if __name__ == "__main__":
    main()
