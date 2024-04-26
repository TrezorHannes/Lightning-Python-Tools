'''
Overall Goal of the script: to send a specified amount of Lightning funds to a specified LN address. 
The user can specify the total amount to transfer, the amount per transaction, the interval between transactions, 
the maximum fee rate, and a message to include with the payments. The user can also specify a peer to send the 
payments as first hop. If no peer is specified, the script will try to find peers with a high local balance.

'''

import os
import time
import subprocess
from subprocess import run
import json
import configparser
import re
import argparse
import sys

# Parse the command line arguments
parser = argparse.ArgumentParser(description='Lightning Swap Wallet')
parser.add_argument('-lb', '--local-balance', type=float, default=60,
                    help='Minimum local balance percentage to consider for transactions (default: 60)')
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


def get_channels():
    try:
        command = [full_path_lncli, 'listchannels']
        # Execute the command and capture the output
        command_output = execute_command(command)
        if command_output is None:
            print("Command execution failed, no output to parse.")
            return []
        else:
            # Attempt to parse the JSON output
            try:
                data = json.loads(command_output)
                return data.get('channels', [])
            except json.JSONDecodeError as json_err:
                print(f"JSON parsing error: {json_err}")
                print("Raw command output that caused JSON parsing error:", command_output)
                return []
    except Exception as e:
        print(f"Error executing lncli command: {str(e)}")
        exit(1)

def execute_command(command):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.stderr:
        print("Error:", result.stderr.decode())
    return result.stdout.decode('utf-8')


def filter_channels(channels):
    filtered_channels = []
    for channel in channels:
        local_balance = int(channel.get('local_balance', 0))
        capacity = int(channel.get('capacity', 1))
        remote_pubkey = channel.get('remote_pubkey', '')
        peer_alias = channel.get('peer_alias', 'Unknown')

        # Check if the remote pubkey is in the ignore list
        if remote_pubkey in ignore_remote_pubkeys:
            continue

        if local_balance >= (args.local_balance / 100) * capacity:
            filtered_channels.append({
                'remote_pubkey': remote_pubkey,
                'peer_alias': peer_alias,
                'local_balance': local_balance,
                'capacity': capacity
            })
    # Sort channels by local_balance in descending order
    filtered_channels.sort(key=lambda x: x['local_balance'], reverse=True)
    return filtered_channels


channel = 0

def send_payments(ln_address, amount, total_amount, interval_seconds, fee_rate, message, peer):
    global channel
    global success_counter
    remain_capacity_tx = 0
    while total_amount > 0:

        # Build the command with user input
        if peer is not None:
            command_to_execute = f"{full_path_bos} send {ln_address} --amount {amount} --message \"{message}\" --max-fee-rate {fee_rate} --out {peer}"
        else:
            print(f"Total peers: {len(filtered_channels)}")
            if channel < len(filtered_channels):
                peer_alias = filtered_channels[channel]['peer_alias']
                print(f"Peer:{channel} - {peer_alias}")
                command_to_execute = f"{full_path_bos} send {ln_address} --amount {amount} --message \"{message}\" --max-fee-rate {fee_rate} --out {filtered_channels[channel]['remote_pubkey']}"
            else:
                print("Starting random peers...")
                command_to_execute = f"{full_path_bos} send {ln_address} --amount {amount} --message \"{message}\" --max-fee-rate {fee_rate}"
        print(f"Executing command: {command_to_execute}\n")
        output = subprocess.run(command_to_execute, shell=True, capture_output=True, text=True)

        # Check if the output contains a success message
        if "success" in output.stdout:
            total_amount -= amount
            print(f"✅ Transaction successful.{output.stdout} \nRemaining amount: {total_amount}\n")
            if peer is None:
                
                if channel < len(filtered_channels):
                    success_counter += amount
                    remain_capacity_tx = (int(filtered_channels[channel]['local_balance']) - success_counter) / int(filtered_channels[channel]['capacity'])
                
                if channel < len(filtered_channels) and remain_capacity_tx >= (args.local_balance / 100):
                    peer_alias = filtered_channels[channel]['peer_alias']
                    print(f"Trying again as remain local balance is higher than {args.local_balance}%: {peer_alias}")
                else:
                    channel += 1
            
            print(f"Waiting {interval_seconds} seconds to execute next transaction\n")
            time.sleep(interval_seconds)
        else:
            print(f"❌ Transaction failed {output.stderr}. Retrying...\n")
            channel += 1
            success_counter = 0
            print(f"⌛ Waiting in 5 seconds to try again\n")
            time.sleep(5)

print("-" * 80)
print(" " * 30 + f"Lightning Swap Wallet")
print("-" * 80)
        
try:
    # Get user input for LN address, amount, and total amount
    while True:
        ln_address = input("📧 Enter LN address: ")
        if not ln_address:
            print("🛑 Invalid LN address. Please enter a valid LN address.")
            continue
        elif not re.match(r'^[a-zA-Z0-9_.-]+@[a-zA-Z0-9_.-]+\.[a-zA-Z0-9_.-]+$', ln_address):
            print("🛑 Invalid LN address. Please enter a valid LN address.")
            continue
        break


    while True:
        total_amount_to_transfer = input("💰 Enter total amount to transfer: ")
        try:
            total_amount = int(total_amount_to_transfer)
        except ValueError:
            print("🛑 Invalid amount. Please enter a valid number.")
            continue
        break

    while True:
        amount = input("💸 Enter amount per transaction: ")
        try:
            amount = int(amount)
        except ValueError:
            print("🛑 Invalid amount. Please enter a valid number.")
            continue
        break

    while True:
        interval = input("⌛ Enter the interval in seconds between transactions: ")
        try:
            interval_seconds = int(interval)
        except ValueError:
            print("🛑 Invalid interval. Please enter a valid number.")
            continue
        break

    while True:
        fee_rate = input("🫰 Enter the max fee rate in ppm: ")
        try:
            fee_rate = int(fee_rate)
        except ValueError:
            print("🛑 Invalid fee rate. Please enter a valid number.")
            continue
        break

    message = input("🗯️ Payment Message: ")

    while True:
        peer = input("🫗 Out Peer Alias or Pubkey: ")
        if not peer:
            peer = None
            print("\n📢 No peer specified, trying first with heavy outbound peers...")
            print("\n📋Getting peers with local balance >= {args.local_balance}%...")
            channels = get_channels()
            filtered_channels = filter_channels(channels)
            break
        else:
            break

except KeyboardInterrupt:
    print("\nExiting...")
    sys.exit(0)

# Send payments

send_payments(ln_address, amount, total_amount, interval_seconds, fee_rate, message, peer)