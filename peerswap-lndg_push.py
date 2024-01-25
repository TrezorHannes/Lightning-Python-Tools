## This script is still missing a serializer for notes in LNDg being updated via API
## Until this is implemented, it's not going to work for automated updates
## However, you can uncomment the debug-entries in the main() function to get a quick overview for manual copy & paste

import datetime
import os
import requests
import json
import subprocess
import logging  # For more structured debugging

# Define the command to get peers information
pscli_command = ['pscli', 'listpeers']

# LNDg API credentials and endpoints
username = 'lndg-admin'
password = '{{ LNDG-PASSWORD }}'
get_api_url = 'http://localhost:8889/api/channels'
update_api_url = 'http://localhost:8889/api/chanpolicy/'

# File path for the log file
log_file_path = os.path.expanduser('~/peerswap-LNDg_changes.log')

# Logfile definition
logging.basicConfig(filename=log_file_path, level=logging.DEBUG) 

# Alt function to get the output of 'lncli listchannels'
def get_lncli_listchannels_output():
    result = subprocess.run(['lncli', 'listchannels'], stdout=subprocess.PIPE)
    channels_data = json.loads(result.stdout)
    return channels_data['channels']

# Alt function to match alias using channel-id
def find_alias_by_chan_id(chan_id):
    channels_data = get_lncli_listchannels_output()
    for channel in channels_data:
        if channel['chan_id'] == chan_id:
            return channel['peer_alias']
    return None

# Function to get peers information from peerswap
def get_peerswap_info():
    try:
        # Run the pscli command and capture the output - needs to be in path
        result = subprocess.run(pscli_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            peers_info = json.loads(result.stdout)
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON: {e}")
            return None 
        if result.returncode == 0:
            # Parse the JSON output
            peers_info = json.loads(result.stdout)
            # Extract the required information
            formatted_info = []
            for peer in peers_info['peers']:
                if peer.get('channels'):  # Check if the 'channels' list exists
                    channel_id = peer['channels'][0]['channel_id'] 
                else:
                    channel_id = ""  # when there's no channel

                swaps_allowed = peer['swaps_allowed']
                supported_assets = ', '.join(peer['supported_assets'])
                swaps_out = sum(int(ch['swaps_out']) for ch in [peer['as_sender'], peer['as_receiver']])
                swaps_in = sum(int(ch['swaps_in']) for ch in [peer['as_sender'], peer['as_receiver']])
                paid_fee = int(peer['paid_fee'])

                new_notes = (
                    f"Swaps Allowed: {swaps_allowed}\n"
                    f"Assets Allowed: {supported_assets}\n"
                    f"SUM of Swap-Outs: {swaps_out}\n"
                    f"SUM of Swap-Ins: {swaps_in}\n"
                    f"Paid fee: {paid_fee}"
                )
                
                formatted_info.append((channel_id, new_notes))  # Append individual peer objects
            return formatted_info
        else:
            logging.error(f"Error running pscli: {result.stderr}")
            return None
    except Exception as e:
        print(f"Error: {e}")
        logging.error(f"Error: {e}")

        return None

def get_current_timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# Function to get current notes from LNDg API
def get_current_notes(channel_id):
    # Use the base API URL and append the specific channel ID
    api_url = f"{get_api_url}/{channel_id}/"

    try:
        # Make the API request with authentication
        response = requests.get(api_url, auth=(username, password))

        # Check if the request was successful (status code 200)
        if response.status_code == 200:
            data = response.json()
            # Extract the 'notes' field from the JSON response
            # logging.debug(f"Data type: {type(data)}, value: {data}")
            notes = data.get('notes', '')
            return notes
        else:
            logging.error(f"API request for channel {channel_id} failed with status code: {response.status_code}")
            return None

    except Exception as e:
        logging.error(f"Error retrieving notes for channel {channel_id}: {e}")
        return None


# Function to update notes on LNDg API
def update_notes(channel_id, notes):
    payload = {
        "chan_id": channel_id,
        "notes": notes
    }
    try:
        # Make a POST request to the LNDg API to update the notes
        response = requests.post(update_api_url, json=payload, auth=(username, password))

        timestamp = get_current_timestamp()
        logging.debug(f"Channel-ID: {channel_id}")
        logging.debug(f"Payload: {payload}")
        logging.debug(f"API Response: {response.text}")
        logging.debug(f"API Status Code: {response.status_code}")
        logging.debug(f"Timestamp: {timestamp}")
        if response.status_code == 200:
            # Log the changes
            with open(log_file_path, 'a') as log_file:
                log_file.write(f"{timestamp}: Updated notes for channel {channel_id}\n")
            print(f"{timestamp}: Updated notes for channel {channel_id}")
        else:
            logging.error(f"{timestamp}: Failed to update notes for channel {channel_id}: Status Code {response.status_code}")

    except Exception as e:
        logging.error(f"Error updating notes for channel {channel_id}: {e}")

# Main function
def main():
    channels_data = get_lncli_listchannels_output()
    peers_info = get_peerswap_info()
    if peers_info:
        for channel_id, new_notes in peers_info:
            alias = find_alias_by_chan_id(channel_id) 
            current_notes = get_current_notes(channel_id)
            if not current_notes:
                print("============================================================================")
                print(f"No existing notes stored in LNDg for {alias} Channel {channel_id}.")
                print(f"Overwriting with new notes:\n{new_notes}.")
                # update_notes(channel_id, new_notes)
            else:
                print("============================================================================")
                print(f"Existing notes stored in LNDg for {alias} Channel {channel_id}:\n{current_notes}")
                action = input("Do you want to overwrite the existing notes (o) or append the new notes (a)? (o/a): ")
                if action.lower() == 'o':
                    print(f"Overwriting the existing notes with new notes:\n{new_notes}.")
                    # debug_api_url = f"{update_api_url}{channel_id}/"
                    # print(f"Debug: Would PUT {channel_id} to {debug_api_url} with notes:\n{new_notes}")
                    update_notes(channel_id, new_notes)
                elif action.lower() == 'a':
                    print(f"Appending the existing notes with new notes:\n{current_notes}\n{new_notes}.")
                    # debug_api_url = f"{update_api_url}{channel_id}/"
                    # print(f"Debug: Would PUT {channel_id} to {debug_api_url} with notes:\n{current_notes}\n{new_notes}")
                    update_notes(channel_id, current_notes + new_notes)
                else:
                    print(f"Invalid action. Skipping update for this channel.")

if __name__ == "__main__":
    main()
