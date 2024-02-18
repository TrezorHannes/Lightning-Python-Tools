import os
import requests
import json
import time
import configparser

# Get the path to the parent directory
parent_dir = os.path.dirname(os.path.abspath(__file__))

# Construct the path to the config.ini file
config_file_path = os.path.join(parent_dir, '..', 'config.ini')
config = configparser.ConfigParser()
config.read(config_file_path)

# API endpoint URL
api_url = 'http://localhost:8889/api/channels?limit=500&is_open=true'

# Authentication credentials
username = config['credentials']['lndg_username']
password = config['credentials']['lndg_password']

# File path for storing data. This txt is populated to have charge-lnd pick it up
file_path = os.path.expanduser('~/.chargelnd/.config/0_fee.txt')

# Remote pubkey to ignore. Add pubkey or reference in config.ini if you want to use it.
ignore_remote_pubkey = config['pubkey']['fee_ignore_pubkey']

def get_chan_ids_to_write():
    chan_ids_to_write = []  # Initialize the list
    try:
        # Make the API request with authentication
        response = requests.get(api_url, auth=(username, password))

        # Check if the request was successful (status code 200)
        if response.status_code == 200:
            data = response.json()
            if 'results' in data:
                results = data['results']
                for result in results:
                    remote_pubkey = result.get('remote_pubkey', '')
                    local_fee_rate = result.get('local_fee_rate', 0)
                    chan_id = result.get('chan_id', '')
                    if local_fee_rate == 0 and remote_pubkey != ignore_remote_pubkey:
                        chan_ids_to_write.append(chan_id)
        else:
            print(f"API request failed with status code: {response.status_code}")

    except Exception as e:
        print(f"Error: {e}")

    return chan_ids_to_write

chan_ids = get_chan_ids_to_write()

if chan_ids:
    with open(file_path, 'w') as file:
        for chan_id in chan_ids:
            file.write(f"{chan_id}\n")
        print("Data written to file.")
