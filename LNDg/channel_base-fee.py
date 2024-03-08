import os
import requests
import datetime  # Import datetime module
import configparser

# Get the path to the parent directory
parent_dir = os.path.dirname(os.path.abspath(__file__))

# Construct the path to the config.ini file
config_file_path = os.path.join(parent_dir, '..', 'config.ini')
config = configparser.ConfigParser()
config.read(config_file_path)

# API endpoint URL for retrieving channels
api_url = 'http://localhost:8889/api/channels?limit=500'

# API endpoint URL for updating channels
update_api_url = 'http://localhost:8889/api/chanpolicy/'

# Authentication credentials
username = config['credentials']['lndg_username']
password = config['credentials']['lndg_password']

# File path for the log file
log_file_path = os.path.join(parent_dir, '..', 'logs', 'lndg-channel_base-fee.log')

# Remote pubkey to ignore. Add pubkey or reference in config.ini if you want to use it.
ignore_remote_pubkeys = config['pubkey']['base_fee_ignore'].split(',')

def get_channels_to_modify():
    channels_to_modify = []  # Initialize the list
    try:
        # Make the API request with authentication
        response = requests.get(api_url, auth=(username, password))

        # Check if the request was successful (status code 200)
        if response.status_code == 200:
            data = response.json()
            if 'results' in data:
                results = data['results']
                for result in results:
                    # Safely get the values with defaults if not found
                    remote_pubkey = result.get('remote_pubkey', '')
                    local_base_fee = result.get('local_base_fee', 0)
                    local_fee_rate = result.get('local_fee_rate', 0)
                    is_open = result.get('is_open', False)
                    chan_id = result.get('chan_id', '')

                    if remote_pubkey in ignore_remote_pubkeys:
                        print(f"Ignoring channel {chan_id} with remote_pubkey {remote_pubkey}")
                    else:
                        print(f"Processing channel {chan_id} - local_base_fee: {local_base_fee}, local_fee_rate: {local_fee_rate}, is_open: {is_open}")
                        if local_base_fee > 0 and local_fee_rate > 0 and is_open:
                            channels_to_modify.append((chan_id, local_base_fee))
        else:
            print(f"API request failed with status code: {response.status_code}")

    except Exception as e:
        print(f"Error: {e}")

    return channels_to_modify

def modify_channels(channels):
    try:
        for chan_id, local_base_fee in channels:
            # Define the payload to update the channel
            # Definition of fields: https://github.com/cryptosharks131/lndg/blob/a01fb26b0c67587b62312615236482c2c9610aa4/gui/serializers.py#L128-L135
            payload = {
                "chan_id": chan_id,
                "base_fee": 0
            }
            
            # Make a PUT request to update the channel details
            response = requests.post(update_api_url, json=payload, auth=(username, password))

            # Get the current timestamp
            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            if response.status_code == 200:
                # Log the changes
                with open(log_file_path, 'a') as log_file:
                    log_file.write(f"{timestamp}: Changed 'local_base_fee' to 0 for channel {chan_id}\n")
                print(f"{timestamp}: Changed 'local_base_fee' to 0 for channel {chan_id}")
            else:
                print(f"{timestamp}: Failed to update channel {chan_id}: Status Code {response.status_code}")

    except Exception as e:
        print(f"Error modifying channels: {e}")

if __name__ == "__main__":
    channels_to_modify = get_channels_to_modify()
    if channels_to_modify:
        modify_channels(channels_to_modify)
