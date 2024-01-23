## This needs further debug, not working yet 

import datetime
import os
import requests
import json
import subprocess

# Define the command to get peers information
pscli_command = ['pscli', 'listpeers']

# LNDg API credentials and endpoints
username = 'lndg-admin'
password = '${{ secrets.PASSWORD }}'
get_api_url = 'http://debian-nuc.local:8889/api/channels?limit=500'
update_api_url = 'http://debian-nuc.local:8889/api/chanpolicy/'

# File path for the log file
log_file_path = os.path.expanduser('~/peerswap-LNDg_changes.log')

'''
# Function to execute the command and return JSON output
def get_pscli_listpeers_output():
    result = subprocess.run(['pscli', 'listpeers'], stdout=subprocess.PIPE)
    return json.loads(result.stdout)
'''

# Function to get peers information from peerswap
def get_peerswap_info():
    try:
        # Run the pscli command and capture the output
        result = subprocess.run(pscli_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0:
            # Parse the JSON output
            peers_info = json.loads(result.stdout)
            # Extract the required information
            formatted_info = []
            for peer in peers_info:
                swaps_allowed = peer['swaps_allowed']
                supported_assets = ', '.join(peer['supported_assets'])
                swaps_out = sum(int(ch['swaps_out']) for ch in [peer['as_sender'], peer['as_receiver']])
                swaps_in = sum(int(ch['swaps_in']) for ch in [peer['as_sender'], peer['as_receiver']])
                paid_fee = int(peer['paid_fee'])

                notes = (
                    f"Swaps Allowed: {swaps_allowed}\n"
                    f"Assets Allowed: {supported_assets}\n"
                    f"SUM of Swap-Outs: {swaps_out}\n"
                    f"SUM of Swap-Ins: {swaps_in}\n"
                    f"Paid fee: {paid_fee}"
                )
                formatted_info.append((peer['channels'][0]['channel_id'], notes))
            return formatted_info
        else:
            print(f"Error running pscli: {result.stderr}")
            return None
    except Exception as e:
        print(f"Error: {e}")
        return None

# Function to get current notes from LNDg API
def get_current_notes(chan_id):
    # Use the base API URL and append the specific channel ID
    api_url = f"{get_api_url}/{chan_id}/"

    try:
        # Make the API request with authentication
        response = requests.get(api_url, auth=(username, password))

        # Check if the request was successful (status code 200)
        if response.status_code == 200:
            data = response.json()
            # Extract the 'notes' field from the JSON response
            notes = data.get('notes', '')
            return notes
        else:
            print(f"API request for channel {chan_id} failed with status code: {response.status_code}")
            return None

    except Exception as e:
        print(f"Error retrieving notes for channel {chan_id}: {e}")
        return None


# Function to update notes on LNDg API
def update_notes(chan_id, notes):
    payload = {
        "chan_id": chan_id,
        "notes": notes
    }
    try:
        # Make a PUT request to the LNDg API to update the notes
        response = requests.put(update_api_url, json=payload, auth=(username, password))

        # Get the current timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if response.status_code == 200:
            # Log the changes
            with open(log_file_path, 'a') as log_file:
                log_file.write(f"{timestamp}: Updated notes for channel {chan_id}\n")
            print(f"{timestamp}: Updated notes for channel {chan_id}")
        else:
            print(f"{timestamp}: Failed to update notes for channel {chan_id}: Status Code {response.status_code}")

    except Exception as e:
        print(f"Error updating notes for channel {chan_id}: {e}")

# Main function
def main():
    peers_info = get_peerswap_info()
    if peers_info:
        for chan_id, new_notes in peers_info:
            current_notes = get_current_notes(chan_id)
            print(f"Channel-ID: {chan_id}")
            print(f"Notes old: {current_notes}")
            print(f"Notes new: {new_notes}")
            overwrite = input("Do you want to overwrite the existing notes? (y/n): ")
            if overwrite.lower() == 'y':
                #update_notes(chan_id, new_notes)
                # Instead of updating, print the API URL and the new notes for debugging
                debug_api_url = f"{update_api_url}{chan_id}/"
                print(f"Debug: Would PUT to {debug_api_url} with notes: {new_notes}")
            else:
                print("Skipping update for this channel.")

if __name__ == "__main__":
    main()
