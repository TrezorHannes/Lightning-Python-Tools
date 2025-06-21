import os
import requests
import datetime  # Import datetime module
import configparser
from prettytable import PrettyTable

# Get the path to the parent directory
parent_dir = os.path.dirname(os.path.abspath(__file__))

# Construct the path to the config.ini file
config_file_path = os.path.join(parent_dir, "..", "config.ini")
config = configparser.ConfigParser()
config.read(config_file_path)

# Updated to pre-filter for open and active channels and increase the limit
api_url = config["lndg"]["lndg_api_url"] + "/api/channels?limit=5000&is_open=true&is_active=true"

# API endpoint URL for updating channels
update_api_url = config["lndg"]["lndg_api_url"] + "/api/chanpolicy/"

# Authentication credentials
username = config["credentials"]["lndg_username"]
password = config["credentials"]["lndg_password"]

# File path for the log file
log_file_path = os.path.join(parent_dir, "..", "logs", "lndg-channel_base-fee.log")

# Remote pubkey to ignore. Add pubkey or reference in config.ini if you want to use it.
ignore_remote_pubkeys = config["pubkey"]["base_fee_ignore"].split(",")


def get_channels_to_modify():
    channels_to_modify = []  # Initialize the list of channels to update
    all_channels_details = [] # Initialize list to store details of all channels from API
    try:
        # Make the API request with authentication
        response = requests.get(api_url, auth=(username, password))

        # Check if the request was successful (status code 200)
        if response.status_code == 200:
            data = response.json()
            if "results" in data:
                results = data["results"]
                for result in results:
                    # Safely get the values with defaults if not found
                    alias = result.get("alias", "N/A")
                    chan_id = result.get("chan_id", "")
                    remote_pubkey = result.get("remote_pubkey", "")
                    local_base_fee = result.get("local_base_fee", 0)
                    local_fee_rate = result.get("local_fee_rate", 0)
                    # The user asked for is_active. LNDg API might use 'active'.
                    # We'll try 'is_active' first, then 'active'.
                    is_active = result.get("is_active", result.get("active", False))
                    is_open = result.get("is_open", False)

                    selected_for_update = False
                    reason_for_not_selecting = []

                    if remote_pubkey in ignore_remote_pubkeys:
                        reason_for_not_selecting.append(f"Pubkey ignored ({remote_pubkey[:10]}...)")
                    
                    if not (local_base_fee > 0):
                        reason_for_not_selecting.append("Base fee not > 0")
                    if not (local_fee_rate > 0):
                        reason_for_not_selecting.append("Fee rate not > 0")
                    if not is_open:
                        reason_for_not_selecting.append("Not open")

                    if not reason_for_not_selecting and remote_pubkey not in ignore_remote_pubkeys:
                        # All conditions met
                        print(
                            f"Channel {chan_id} will be processed - local_base_fee: {local_base_fee}, local_fee_rate: {local_fee_rate}, is_open: {is_open}"
                        )
                        channels_to_modify.append((chan_id, local_base_fee))
                        selected_for_update = True
                    elif remote_pubkey in ignore_remote_pubkeys:
                         print(
                            f"Ignoring channel {chan_id} with remote_pubkey {remote_pubkey} (in ignore list)"
                        )
                    else:
                        print(
                            f"Skipping channel {chan_id} ({alias}) - Reasons: {'; '.join(reason_for_not_selecting)}"
                        )


                    all_channels_details.append({
                        "alias": alias,
                        "chan_id": chan_id,
                        "remote_pubkey": remote_pubkey,
                        "local_base_fee": local_base_fee,
                        "local_fee_rate": local_fee_rate,
                        "is_active": is_active,
                        "is_open": is_open,
                        "selected_for_update": selected_for_update,
                        "reason": '; '.join(reason_for_not_selecting) if reason_for_not_selecting else "Selected"
                    })
        else:
            print(f"API request failed with status code: {response.status_code}")
            # Add failed API response to details for debugging if needed
            all_channels_details.append({
                "alias": "API ERROR", "chan_id": str(response.status_code), 
                "remote_pubkey": response.text[:50], "local_base_fee": -1, 
                "local_fee_rate": -1, "is_active": False, "is_open": False, 
                "selected_for_update": False, "reason": "API request failed"
            })


    except Exception as e:
        print(f"Error in get_channels_to_modify: {e}")
        all_channels_details.append({
            "alias": "SCRIPT ERROR", "chan_id": "N/A", 
            "remote_pubkey": str(e)[:50], "local_base_fee": -1, 
            "local_fee_rate": -1, "is_active": False, "is_open": False, 
            "selected_for_update": False, "reason": "Exception during processing"
        })


    return channels_to_modify, all_channels_details


def print_all_channel_details_table(all_channels_data):
    if not all_channels_data:
        print("No channel data to display.")
        return

    table = PrettyTable()
    table.field_names = [
        "Alias", "Chan ID", "Remote Pubkey (short)", "Local Base Fee",
        "Local Fee Rate", "Is Active", "Is Open", "Selected", "Reason/Status"
    ]
    table.align["Alias"] = "l"
    table.align["Reason/Status"] = "l"

    filtered_channels_count = 0
    for channel in all_channels_data:
        # Apply filters: only show if open, active, and local_base_fee > 0
        is_open = channel.get("is_open", False)
        is_active = channel.get("is_active", False)
        local_base_fee = channel.get("local_base_fee", 0)

        if is_open and is_active and local_base_fee > 0:
            table.add_row([
                channel.get("alias", "N/A"),
                channel.get("chan_id", "N/A"),
                channel.get("remote_pubkey", "N/A")[:10] + "..." if channel.get("remote_pubkey") else "N/A",
                local_base_fee,
                channel.get("local_fee_rate", "N/A"),
                is_active,
                is_open,
                "Yes" if channel.get("selected_for_update") else "No",
                channel.get("reason", "N/A")
            ])
            filtered_channels_count += 1
    
    if filtered_channels_count > 0:
        print("\n--- Filtered Channel Details from API (Open, Active, Base Fee > 0) ---")
        print(table)
    else:
        print("\nNo channels met the filtering criteria (Open, Active, Base Fee > 0) for display.")

def modify_channels(channels):
    try:
        for chan_id, local_base_fee in channels:
            # Define the payload to update the channel
            # Definition of fields: https://github.com/cryptosharks131/lndg/blob/a01fb26b0c67587b62312615236482c2c9610aa4/gui/serializers.py#L128-L135
            payload = {"chan_id": chan_id, "base_fee": 0}

            # Make a PUT request to update the channel details
            response = requests.post(
                update_api_url, json=payload, auth=(username, password)
            )

            # Get the current timestamp
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if response.status_code == 200:
                # Log the changes
                with open(log_file_path, "a") as log_file:
                    log_file.write(
                        f"{timestamp}: Changed 'local_base_fee' to 0 for channel {chan_id}\n"
                    )
                print(
                    f"{timestamp}: Changed 'local_base_fee' to 0 for channel {chan_id}"
                )
            else:
                print(
                    f"{timestamp}: Failed to update channel {chan_id}: Status Code {response.status_code}"
                )

    except Exception as e:
        print(f"Error modifying channels: {e}")


if __name__ == "__main__":
    channels_to_modify, all_channel_details = get_channels_to_modify()
    
    # Print the table with all channel details for debugging
    print_all_channel_details_table(all_channel_details)

    if channels_to_modify:
        print(f"\nFound {len(channels_to_modify)} channel(s) to update.")
        modify_channels(channels_to_modify)
    else:
        print("\nNo channels met the criteria for modification.")
