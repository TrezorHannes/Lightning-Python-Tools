import os
import time
import subprocess
import json
import requests
import configparser
import re
import io
import argparse

parser = argparse.ArgumentParser(description='Lightning Swap Wallet - Swap your wallet lightning fast.')
args = parser.parse_args()

# Get the path to the parent directory
parent_dir = os.path.dirname(os.path.abspath(__file__))

# path to the config.ini file located in the parent directory
config_file_path = os.path.join(parent_dir, '..', 'config.ini')
config = configparser.ConfigParser()
config.read(config_file_path)

full_path_bos = config['system']['full_path_bos']
full_path_lncli = config['paths']['lncli_path']

# Remote pubkey to ignore. Add pubkey or reference in config.ini if you want to use it.
ignore_remote_pubkeys = config['no-swapout']['swapout_blacklist'].split(',')

cache = {}
channel  = 0
filtered_channels = []
success_counter = 1
node_aliases = {}


def get_node_alias(pub_key):
    global node_aliases

    if pub_key in node_aliases:
        return node_aliases[pub_key]

    try:
        response = requests.get(f"https://mempool.space/api/v1/lightning/nodes/{pub_key}")
        data = response.json()
        node_aliases[pub_key] = data.get('alias', '')
        return data.get('alias', '')
    except Exception as e:
        print(f"Error fetching node alias: {str(e)}")
        return pub_key


def execute_command(command):
    try:
        result = subprocess.run(command, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.stdout.decode('utf-8')
    except subprocess.CalledProcessError as e:
        print(f"Error executing command: {e.stderr.decode('utf-8')}")
        return None


def filter_channels(data):
    global cache

    if cache:
        return cache

    channels = data.get('channels', [])

    filtered_channels = []
    for channel in channels:
        local_balance = int(channel.get('local_balance', 0))
        capacity = int(channel.get('capacity', 1))
        remote_pubkey = channel.get('remote_pubkey', '')

        # Check if the remote pubkey is in the ignore list
        if remote_pubkey in ignore_remote_pubkeys:
            continue

        # Check if local_balance is equal to or more than 30% of capacity
        if local_balance >= 0.3 * capacity:
            filtered_channels.append({
                'remote_pubkey': remote_pubkey,
                'local_balance': local_balance,
                'capacity': capacity
            })

    cache = filtered_channels
    return filtered_channels

    
def execute_transaction(ln_address, amount, total_amount, interval_seconds,fee_rate, message, peer):
    global channel
    global success_counter
    remain_capacity_tx = 0
    while total_amount > 0:
        # Validate the amount as a number
        try:
            amount = int(amount)
        except ValueError:
            print("Invalid amount. Please enter a valid number.")
            break

        # Build the command with user input
        if peer is not None:
            command_to_execute = f"{full_path_bos} send {ln_address} --amount {amount} --message {message} --max-fee-rate {fee_rate} --out {peer}"
        else:
            print(f"Total peers: {len(filtered_channels)}")
            if channel < len(filtered_channels):
                print(f"Peer:{channel} - {get_node_alias(filtered_channels[channel]['remote_pubkey'])}")
                command_to_execute = f"{full_path_bos} send {ln_address} --amount {amount} --message {message} --max-fee-rate {fee_rate} --out {filtered_channels[channel]['remote_pubkey']}"
            else:
                print("Starting random peers...")
                command_to_execute = f"{full_path_bos} send {ln_address} --amount {amount} --message {message} --max-fee-rate {fee_rate}"
        print(f"Executing command: {command_to_execute}\n")
        output = subprocess.run(command_to_execute, shell=True, capture_output=True, text=True)

        # Check if the output contains a success message
        if "success" in output.stdout:
            total_amount -= amount
            print(f"Transaction successful.{output.stdout} \nRemaining amount: {total_amount}\n")
            if peer is None:
                
                if channel < len(filtered_channels):
                    success_counter += amount
                    remain_capacity_tx = (int(filtered_channels[channel]['local_balance']) - success_counter) / int(filtered_channels[channel]['capacity'])
                
                if channel < len(filtered_channels) and remain_capacity_tx >= 0.3:
                    print(f"Trying again as remain local balance is higher than 30%: {get_node_alias(filtered_channels[channel]['remote_pubkey'])}")
                else:
                    channel +=1
            
            print(f"Waiting {interval_seconds} seconds to execute next transaction\n")
            time.sleep(interval_seconds)
        else:
            print(f"Transaction failed {output.stderr}. Retrying...\n")
            channel += 1
            success_counter = 0
            print(f"Waiting in 5 seconds to try again\n")
            time.sleep(5)
        

print("-" * 80)
print(" " * 30 + f"Lightning Swap Wallet")
print("-" * 80)
        
# Get user input for LN address, amount, and total amount
while True:
    ln_address = input("Enter LN address: ")
    if not ln_address:
        print("Invalid LN address. Please enter a valid LN address.")
        continue
    elif not re.match(r'^[a-zA-Z0-9_.-]+@[a-zA-Z0-9_.-]+\.[a-zA-Z0-9_.-]+$', ln_address):
        print("Invalid LN address. Please enter a valid LN address.")
        continue
    break


while True:
    total_amount_to_transfer = input("Enter total amount to transfer: ")
    try:
        total_amount = int(total_amount_to_transfer)
    except ValueError:
        print("Invalid amount. Please enter a valid number.")
        continue
    break

while True:
    amount = input("Enter amount per transaction: ")
    try:
        amount = int(amount)
    except ValueError:
        print("Invalid amount. Please enter a valid number.")
        continue
    break

while True:
    interval = input("Enter the interval in seconds between transactions: ")
    try:
        interval_seconds = int(interval)
    except ValueError:
        print("Invalid interval. Please enter a valid number.")
        continue
    break

while True:
    fee_rate = input("Enter the max fee rate in ppm: ")
    try:
        fee_rate = int(fee_rate)
    except ValueError:
        print("Invalid fee rate. Please enter a valid number.")
        continue
    break

message = input("Payment Message: ")

while True:
    peer = input("Out Peer Alias or Pubkey: ")
    if not peer:
        peer = None
        print("\nNo peer specified, trying first with heavy outbound peers...")
        print("Getting peers with local balance >= 30%...")
        try:
            # Execute the lncli command
            command_output = execute_command(full_path_lncli)
            # Parse the JSON output
            data = json.load(io.StringIO(command_output))
        except Exception as e:
            print(f"Error executing lncli command: {str(e)}")
            exit(1)
        # Filter and print the channels
        filtered_channels = filter_channels(data)
        break
    else:
        break

execute_transaction(ln_address, amount, total_amount, interval_seconds, fee_rate, message, peer)