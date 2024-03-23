# This script filters all channels which are deemed swap-out candidates.
# It does this by retrieving all channels from the LNDg database with
# 0 ppm fee from our side, active, not in our config.ini blacklist and
# more liquidity than the capacity threshold available defined here in the header

# This is shown in the terminal, but also written as channel-IDs into a file found 
# in directory data. From there, it can be picked up by swap-out scripts.
# swap_out-candidates.py -h for help on the different options

import os
import requests
import json
import configparser
from prettytable import PrettyTable
import argparse

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
# File path for storing BOS tags. Create symlink to homedir with ln -s ~/.bos bos
file_path_to_bos = os.path.join(parent_dir, '..', 'bos', 'tags.json')

# Remote pubkey to ignore. Add pubkey or reference in config.ini if you want to use it.
ignore_remote_pubkeys = config['no-swapout']['swapout_blacklist'].split(',')

parser = argparse.ArgumentParser(description='Script to manage swap-out candidates.')
parser.add_argument('-b', '--bos', action='store_true', help='Export bos tags.json file for easy probing.')
parser.add_argument('-e', '--file-export', action='store_true', help='Write into defined file.log for easy pickup of swap-out automations like litd.')
parser.add_argument('-p', '--pubkey', action='store_true', help='Show remote pubkey instead of channel ID in the table.')
parser.add_argument('-c', '--capacity', type=int, default=5000000, help='Set the capacity threshold for swap-out candidates.')
parser.add_argument('-f', '--fee-limit', type=int, default=60, help='Maximum local fee rate for swap-out candidates.')
args = parser.parse_args()

# Set the CAPACITY_THRESHOLD based on the parsed argument
CAPACITY_THRESHOLD = args.capacity

def get_chan_ids_to_write():
    chan_ids_to_write = []  # Initialize the list
    try:
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
                    if local_fee_rate <= args.fee_limit and remote_pubkey not in ignore_remote_pubkeys and local_balance > CAPACITY_THRESHOLD:
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
                if args.pubkey:
                    table.field_names = ["Alias", "Is Active", "Capacity", "Local Balance", "Local PPM", "AR Out Target", "Auto Rebalance", "Pubkey"]
                else:
                    table.field_names = ["Alias", "Is Active", "Capacity", "Local Balance", "Local PPM", "AR Out Target", "Auto Rebalance", "Channel ID"]

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

                    if local_fee_rate <= args.fee_limit and remote_pubkey not in ignore_remote_pubkeys and local_balance > CAPACITY_THRESHOLD:
                        local_balance_ratio = (local_balance / capacity) * 100
                        if args.pubkey:
                            table.add_row([alias, is_active, capacity, f"{local_balance_ratio:.2f}%", local_fee_rate, ar_out_target, auto_rebalance, remote_pubkey])
                        else:
                            table.add_row([alias, is_active, capacity, f"{local_balance_ratio:.2f}%", local_fee_rate, ar_out_target, auto_rebalance, channel_id])

                print(table)
        else:
            print(f"API request failed with status code: {response.status_code}")

    except Exception as e:
        print(f"Error: {e}")

def write_bos_tags():
    try:
        response = requests.get(api_url, auth=(username, password))
        if response.status_code == 200:
            data = response.json()
            if 'results' in data:
                results = data['results']
                # Filter and sort results based on the same criteria
                filtered_sorted_results = [
                    result for result in sorted(results, key=lambda x: (x.get('local_balance', 0) / x.get('capacity', 1)), reverse=True)
                    if result.get('local_fee_rate', 0) <= args.fee_limit and result.get('remote_pubkey', '') not in ignore_remote_pubkeys and result.get('local_balance', 0) > CAPACITY_THRESHOLD
                ]
                # Extract remote_pubkey from filtered and sorted results
                remote_pubkeys = [result.get('remote_pubkey', '') for result in filtered_sorted_results]

                tags_data = {
                    "tags": [
                        {
                            "alias": "swap-candidates",
                            "id": "454d13aff835eeb91de6183684a208cd7e3d4cc19d025fab84f6838c4575cdae",
                            "nodes": remote_pubkeys
                        }
                    ]
                }

                with open(file_path_to_bos, 'w') as file:
                    json.dump(tags_data, file, indent=2)

                print(f"Tags data written to {file_path_to_bos}")
        else:
            print(f"API request failed with status code: {response.status_code}")
    except Exception as e:
        print(f"Error: {e}")

def main():
    terminal_output()

    if args.bos:
        write_bos_tags()

    if args.file_export:
        chan_ids = get_chan_ids_to_write()
        if chan_ids:
            with open(file_path, 'w') as file:
                file.write(','.join(chan_ids) + '\n')
            print(f"Channel-ID data written to {file_path}")

if __name__ == "__main__":
    main()
