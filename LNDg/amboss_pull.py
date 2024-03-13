# Purpose: This script downloads a list of active and outdated channels from a channel-buy from Magma.
# It writes the long-channel-IDs into a directory so charge-LND can use this as a different ruleset.
# It'll remove the channels once the channel-buy is expired, and then activate AutoFees in LNDg.
# It also enters Magma Active or Expired into the LNDg API as 'note' so it'll show in LNDg Dashboard mouseover and channel card

import requests
import os
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
active_file_path = os.path.expanduser('~/.chargelnd/.config/magma-channels_new.txt') # Production
finished_file_path = os.path.expanduser('~/.chargelnd/.config/magma-finished_new.txt') # Production

# path for the log file
log_file_path = os.path.join(parent_dir, '..', 'logs', 'amboss-LNDg_changes.log')

# Logfile definition
logging.basicConfig(filename=log_file_path, level=logging.DEBUG) 

# Get the current timestamp
def get_current_timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# Define the headers, grab your API Key from Magma and enter it into config.ini in the parent folder
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


# Converts short channel IDs to long channel IDs using the Amboss API. 
# Optimized for bulk conversion
def convert_short_to_long_chan_id(short_chan_ids):  # Now accepts a list 
    
    bulk_query = """
    query GetEdgeInfoBatch($ids: [String!]!) {
        getEdgeInfoBatch(ids: $ids) {
            long_channel_id
            short_channel_id
        }
    }
    """

    variables = {
        "ids": list(short_chan_ids)
    }

    payload = {
        "query": bulk_query,
        "variables": variables
    }

    try:
        response = requests.post(amboss_url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()

        long_chan_id_map = {
            edge['short_channel_id']: edge['long_channel_id'] 
            for edge in data['data']['getEdgeInfoBatch'] 
        }

        return long_chan_id_map

    except requests.exceptions.HTTPError as e:
        print(f"HTTP Error: {e}")
        return {}
    except Exception as e:
        print(f"An error occurred: {e}")
        return {} 
    

# Function to move channels from active file to finished file
def move_finished_channels():
    response = requests.post(amboss_url, json=payload, headers=headers)
    response.raise_for_status()  
    data = response.json()

    active_channels_info = []  
    non_active_chan_ids = []

    short_id_info = {
        order['channel_id']: {'status': order['status'], 'blocks_until_close': order['blocks_until_can_be_closed']}
        for order in data['data']['getUser']['market']['offer_orders']['list'] if order['channel_id'] is not None
    }

    # Filter out None values and convert short IDs to long IDs
    short_chan_ids = list(short_id_info.keys())
    long_chan_id_map = convert_short_to_long_chan_id(short_chan_ids)

    # Process orders using long channel IDs
    for short_chan_id, info in short_id_info.items():
        long_chan_id = long_chan_id_map.get(short_chan_id)
        if not long_chan_id:
            print(f"Warning: No long channel ID found for short channel ID {short_chan_id}")
            continue

        status = info['status']
        blocks_until_close = info['blocks_until_close']

        if status == "CHANNEL_MONITORING_FINISHED" or blocks_until_close == 0:
            non_active_chan_ids.append(long_chan_id)
        elif status == "VALID_CHANNEL_OPENING":
            active_channels_info.append((long_chan_id, blocks_until_close))

    # Write channel IDs to the active and finished files
    with open(active_file_path, 'w') as active_file:
        for chan_id, _ in active_channels_info:
            active_file.write(chan_id + '\n')

    with open(finished_file_path, 'w') as finished_file:
        for chan_id in non_active_chan_ids:
            finished_file.write(chan_id + '\n')

    return active_channels_info, non_active_chan_ids


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

            if response.status_code == 200:
                with open(log_file_path, 'a') as log_file:
                    log_file.write(f"{timestamp}: Updated auto_fees for channel {chan_id}\n")
                logging.debug(f"Updated auto_fees for channel {chan_id}")
            else:
                logging.error(f"{timestamp}: Failed to update auto_fees for channel {chan_id}: Status Code {response.status_code}")

        except Exception as e:
            logging.error(f"Error updating auto_fees for channel {chan_id}: {e}")


def update_notes_for_active_channels(active_channels_info):
    global lndg_api_url

    for item in active_channels_info:
        try:
            chan_id, blocks_until_close = item
        except ValueError:
            print(f"Error unpacking item: {item}. Expected a tuple with 2 elements.")
            continue

        notes = f"Status: 🌋 Magma Channel Buy Order Active (Lease Expiration: {blocks_until_close} blocks)"

        payload = {
            "chan_id": chan_id,
            "auto_fees": False,
            "notes": notes
        }
        try:
            response = requests.put(f"{lndg_api_url}/{chan_id}/", json=payload, auth=(username, password))

            timestamp = get_current_timestamp()

            if response.status_code == 200:
                with open(log_file_path, 'a') as log_file:
                    log_file.write(f"{timestamp}: Updated notes for channel {chan_id}\n")
                logging.debug(f"Updated notes for channel {chan_id}")
            else:
                logging.error(f"{timestamp}: Failed to update notes for channel {chan_id}: Status Code {response.status_code}")

        except Exception as e:
            logging.error(f"Error updating notes for channel {chan_id}: {e}")


# Call move_finished_pubkeys to get active and non-active pubkeys
active_channels_info, non_active_chan_ids = move_finished_channels()

# Update auto fees for the channels with expired magma sales lease time
# Comment this out if you only want to leverage note-updates in LNDg
update_autofees(non_active_chan_ids)

# Call update_notes_for_active_channels with active_chan_ids
update_notes_for_active_channels(active_channels_info)