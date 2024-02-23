# This script filters all channels which are deemed swap-out candidates.
# It does this by retrieving all channels from the LNDg database with
# 0 ppm fee from our side, active, not in our config.ini blacklist and
# more liquidity than the capacity threshold available defined here in the header

# This is shown in the terminal, but also written as channel-IDs into a file found 
# in directory data. From there, it can be picked up by swap-out scripts.

import os
import requests
import json
import time
import configparser
from prettytable import PrettyTable

# Define the capacity threshold
CAPACITY_THRESHOLD = 5000000

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

# File path for storing data. This export can be used for swap-out
file_path = os.path.join(parent_dir, '..', 'data', 'low-fee-high-local.log')

# Remote pubkey to ignore. Add pubkey or reference in config.ini if you want to use it.
ignore_remote_pubkey = config['no-swapout']['swapout_blacklist']

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

                sorted_results = sorted(results, key=lambda x: (x.get('local_balance', 0) / x.get('capacity', 1)), reverse=True)

                for result in sorted_results:
                    remote_pubkey = result.get('remote_pubkey', '')
                    local_fee_rate = result.get('local_fee_rate', 0)
                    capacity = result.get('capacity', '')
                    local_balance = result.get('local_balance', 0)
                    chan_id = result.get('chan_id', '')
                    if local_fee_rate == 0 and remote_pubkey != ignore_remote_pubkey and local_balance > CAPACITY_THRESHOLD:
                        chan_ids_to_write.append(chan_id)
        else:
            print(f"API request failed with status code: {response.status_code}")

    except Exception as e:
        print(f"Error: {e}")

    return chan_ids_to_write

def terminal_output():
    try:
        response = requests.get(api_url, auth=(username, password))

        if response.status_code == 200:
            data = response.json()
            if 'results' in data:
                results = data['results']

                table = PrettyTable()
                table.field_names = ["Alias", "Is Active", "Capacity", "Local Balance", "AR Out Target", "Auto Rebalance", "Channel-ID"]

                sorted_results = sorted(results, key=lambda x: (x.get('local_balance', 0) / x.get('capacity', 1)), reverse=True)

                for result in sorted_results:
                    alias = result.get('alias', '')
                    remote_pubkey = result.get('remote_pubkey', '')
                    is_active = result.get('is_active', '')
                    capacity = result.get('capacity', '')
                    local_fee_rate = result.get('local_fee_rate', '')
                    local_balance = result.get('local_balance', '')
                    ar_out_target = result.get('ar_out_target', '')
                    auto_rebalance = result.get('auto_rebalance', '')
                    channel_id = result.get('chan_id','')

                    if local_fee_rate == 0 and remote_pubkey != ignore_remote_pubkey and local_balance > CAPACITY_THRESHOLD:
                        local_balance_ratio = (local_balance / capacity) * 100
                        table.add_row([alias, is_active, capacity, f"{local_balance_ratio:.2f}%", ar_out_target, auto_rebalance, channel_id])

                print(table)
        else:
            print(f"API request failed with status code: {response.status_code}")

    except Exception as e:
        print(f"Error: {e}")

terminal_output()

chan_ids = get_chan_ids_to_write()

if chan_ids:
    with open(file_path, 'w') as file:
        file.write(', '.join(chan_ids) + '\n')
        print("Data written to file.")
