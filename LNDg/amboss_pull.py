# Purpose: This script downloads a list of pubkeys which have an active channel-buy from Magma.
# It writes the pubkeys into a directory so my charge-LND can use this as a different ruleset.
# It'll remove the pubkeys once the channel-buy is expired, and then activate AutoFees in LNDg.
# It also enters Magma Active or Expired into the LNDg API as 'note' so it'll show in your Channel Dashboard

# Improvements: Identified too late into the script that I should naviate via channel-IDs instead of Pubkeys.
# This probably needs a whole refactor, which is deprioritized for now.

import requests
import json
import os
import time
import datetime
import logging  # For more structured debugging
import configparser

# Get the path to the parent directory
parent_dir = os.path.dirname(os.path.abspath(__file__))

# path to the config.ini file located in the parent directory
config_file_path = os.path.join(parent_dir, '..', 'config.ini')
config = configparser.ConfigParser()
config.read(config_file_path)

# Define the API endpoint
amboss_url = 'https://api.amboss.space/graphql'

# LNDg API credentials and endpoints. Retrievable from lndg/data/lndg-admin.txt
username = config['credentials']['lndg_username']
password = config['credentials']['lndg_password']
lndg_api_url = 'http://localhost:8889/api/channels'

# Define the output paths
active_file_path = os.path.expanduser('~/.chargelnd/.config/magma-channels.txt') # Production
finished_file_path = os.path.expanduser('~/.chargelnd/.config/magma-finished.txt') # Production

# path for the log file
log_file_path = os.path.join(parent_dir, '..', 'logs', 'amboss-LNDg_changes.log')

# Logfile definition
logging.basicConfig(filename=log_file_path, level=logging.DEBUG) 

# Get the current timestamp
def get_current_timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# Define the headers, grab your API Key from Magma and enter it here
headers = {
    'Authorization': f"Bearer {config['credentials']['amboss_authorization']}",
    'Content-Type': 'application/json',
}

# Define the query
query = '''
    {
      getUser {
        market {
          offer_orders {
            list {
              endpoints {
                destination
              }
              status
              channel_id
              blocks_until_can_be_closed
              created_at
              id
            }
          }
        }
      }
    }
    '''

# Define the payload
payload = {
    "query": query
}

# Function to move pubkeys from active file to finished file
def move_finished_pubkeys():
    # Make the request
    response = requests.post(amboss_url, json=payload, headers=headers)
    response.raise_for_status()  # Raise an exception for 4xx and 5xx status codes

    data = response.json()

    active_pubkeys = []
    non_active_pubkeys = []
    non_valid_pubkeys = []

    # Extract pubkeys from the response data
    for order in data['data']['getUser']['market']['offer_orders']['list']:
        pubkey = order['endpoints']['destination']
        blocks_until_close = order['blocks_until_can_be_closed']
        status = order['status']

        if status == "CHANNEL_MONITORING_FINISHED":
            if pubkey not in active_pubkeys and pubkey not in non_valid_pubkeys:
                non_active_pubkeys.append(pubkey)
        elif status == "VALID_CHANNEL_OPENING":
            if pubkey not in non_active_pubkeys and pubkey not in non_valid_pubkeys:
                active_pubkeys.append(pubkey)
        else:
            if pubkey not in active_pubkeys and pubkey not in non_active_pubkeys:
                non_valid_pubkeys.append(pubkey)

    # Write the unique active pubkeys to the active file
    with open(active_file_path, 'w') as active_file:
        unique_active_pubkeys = list(set(active_pubkeys))
        for pubkey in unique_active_pubkeys:
            active_file.write(pubkey + '\n')

    # Write the unique non-active pubkeys to a separate file
    with open(finished_file_path, 'w') as finished_file:
        unique_non_active_pubkeys = list(set(non_active_pubkeys))
        for pubkey in unique_non_active_pubkeys:
            finished_file.write(pubkey + '\n')
    # Print the lists of unique active, non-active, and non-valid pubkeys
    # print("Active Pubkeys (blocks until close > 0):", unique_active_pubkeys)
    # print("Non-Active Pubkeys (blocks until close = 0):", unique_non_active_pubkeys)
    # print("Non-Valid Pubkeys (blocks until close = '-'): ", non_valid_pubkeys)
    return active_pubkeys, non_active_pubkeys  # Modify the return statement to return both active and non-active pubkeys

# let's update the notes in LNDg with the Amboss buying details for active channels
# We'll also populate the list of non-active channels to activate LNDg AF once they expired
def matchtable_pubkey_to_chan_id(active_pubkeys, non_active_pubkeys):
    active_chan_ids = []
    non_active_chan_ids = []

    response = requests.get(f"{lndg_api_url}?limit=500", auth=(username, password))
    if response.status_code == 200:
        data = response.json()
        if 'results' in data:
            results = data['results']
            for result in results:
                short_channel_id = result.get('short_chan_id', '')
                chan_id = result.get('chan_id', '')
                remote_pubkey = result.get('remote_pubkey', '')
                auto_fee = result.get('auto_fees', '')
                is_open = result.get('is_open', '')

                if remote_pubkey in active_pubkeys and is_open:
                    active_chan_ids.append(chan_id)
                # to improve performance, you can set and not auto_fee to the below to only update channels with AF false
                elif remote_pubkey in non_active_pubkeys and is_open:
                    non_active_chan_ids.append(chan_id)

    return active_chan_ids, non_active_chan_ids

# Update the fee for channels with expired magma sales lease time
def update_autofees(non_active_chan_ids):
    global lndg_api_url

    for chan_id in non_active_chan_ids:
        
        notes = f"Status: ⛰️ Magma Channel Buy Order Expired"
        
        payload = {
            "chan_id": chan_id,
            "auto_fees": True,
            "notes": notes
        }
        try:
            response = requests.put(f"{lndg_api_url}/{chan_id}/", json=payload, auth=(username, password))

            timestamp = get_current_timestamp()
            # logging.debug(f"Channel-ID: {chan_id}")
            # logging.debug(f"Payload: {payload}")
            # logging.debug(f"API Response: {response.text}")
            # logging.debug(f"API Status Code: {response.status_code}")

            if response.status_code == 200:
                with open(log_file_path, 'a') as log_file:
                    log_file.write(f"{timestamp}: Updated auto_fees for channel {chan_id}\n")
                logging.debug(f"Updated auto_fees for channel {chan_id}")
            else:
                logging.error(f"{timestamp}: Failed to update auto_fees for channel {chan_id}: Status Code {response.status_code}")

        except Exception as e:
            logging.error(f"Error updating auto_fees for channel {chan_id}: {e}")

def update_notes_for_active_channels(active_chan_ids, query_data):
    global lndg_api_url

    for chan_id in active_chan_ids:

        notes = f"Status: 🌋 Magma Channel Buy Order Active"

        payload = {
            "chan_id": chan_id,
            "notes": notes
        }
        try:
            response = requests.put(f"{lndg_api_url}/{chan_id}/", json=payload, auth=(username, password))

            timestamp = get_current_timestamp()
            # logging.debug(f"Channel-ID: {chan_id}")
            # logging.debug(f"Payload: {payload}")
            # logging.debug(f"API Response: {response.text}")
            # logging.debug(f"API Status Code: {response.status_code}")

            if response.status_code == 200:
                # Log the changes
                with open(log_file_path, 'a') as log_file:
                    log_file.write(f"{timestamp}: Updated notes for channel {chan_id}\n")
                logging.debug(f"Updated notes for channel {chan_id}")
            else:
                logging.error(f"{timestamp}: Failed to update notes for channel {chan_id}: Status Code {response.status_code}")

        except Exception as e:
            logging.error(f"Error updating notes for channel {chan_id}: {e}")


# Call move_finished_pubkeys to get active and non-active pubkeys
active_pubkeys, non_active_pubkeys = move_finished_pubkeys()

# Get the list of channel IDs based on active and non-active pubkeys
chan_id_list = matchtable_pubkey_to_chan_id(active_pubkeys, non_active_pubkeys)

# Update auto fees for the channels with expired magma sales lease time
update_autofees(chan_id_list[1])

# Define active_chan_ids globally
active_chan_ids = chan_id_list[0]
query_data = {}

# Call update_notes_for_active_channels with active_chan_ids
update_notes_for_active_channels(active_chan_ids, query_data)
