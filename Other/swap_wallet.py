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


def send_payments_with_specified_peer(ln_address, amount, total_amount, interval_seconds, fee_rate, message, peer):
    success_counter = 0

    while total_amount > 0:
        command_to_execute = build_command(ln_address, amount, message, fee_rate, peer)
        output = execute_payment_command(command_to_execute)

        if "success" in output.stdout:
            total_amount -= amount
            print(f"✅ Transaction successful. Remaining amount: {total_amount}")

            # If more payments are needed, retry with the same peer
            if total_amount > 0: 
                print("Trying again with the same peer...")
                success_counter += amount  
            else:
                print("🎉 Execution finished. Have a nice day!")
                return  

        else:
            print(f"❌ Transaction failed {output.stderr}. Adjust the fee-rate or choose another outgoing peer.")
            return  # Exit on failure

        time.sleep(interval_seconds) 


def send_payments_auto_select_peer(ln_address, amount, total_amount, interval_seconds, fee_rate, message, filtered_channels):
    channel_index = 0
    success_counter = 0

    while total_amount > 0:
        if channel_index >= len(filtered_channels):
            print("⚠️ No suitable peers found with enough balance. Exiting.")
            return

        peer_alias = filtered_channels[channel_index]['peer_alias']
        remote_pubkey = filtered_channels[channel_index]['remote_pubkey']
        print(f"Total peers: {len(filtered_channels)}")
        print(f"Peer:{channel_index} - {peer_alias}")
        command_to_execute = build_command(ln_address, amount, message, fee_rate, remote_pubkey)

        output = execute_payment_command(command_to_execute)
        if "success" in output.stdout:
            total_amount -= amount
            print(f"✅ Transaction successful. Remaining amount: {total_amount}")
            
            # Check if retry is needed on the same peer
            if total_amount > 0 and should_retry_transaction(channel_index, success_counter, filtered_channels):
                print(f"Trying again as remaining local balance is higher than {args.local_balance}% with {peer_alias}")
                success_counter += amount #Increment the counter if we're retrying
            else:
                channel_index += 1
                success_counter = 0

            if total_amount == 0:
                print("🎉 Execution finished. Have a nice day!")
        else:
            print(f"❌ Transaction failed {output.stderr}. Moving to next peer...")
            channel_index += 1
            success_counter = 0  # Reset success counter on failure

        time.sleep(interval_seconds if "success" in output.stdout else 5)



def build_command(ln_address, amount, message, fee_rate, peer):
    return f"{full_path_bos} send {ln_address} --amount {amount} --message \"{message}\" --max-fee-rate {fee_rate} --out {peer}"


def execute_payment_command(command):
    print(f"Executing command: {command}\n")
    return subprocess.run(command, shell=True, capture_output=True, text=True)


def should_retry_transaction(channel_index, success_counter, peer):
    if peer:
        return True
    else:
        remain_capacity_tx = (int(filtered_channels[channel_index]['local_balance']) - success_counter) / int(filtered_channels[channel_index]['capacity'])
        return remain_capacity_tx >= (args.local_balance / 100)


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
            print(f"\n📋Getting peers with local balance >= {args.local_balance}%...")
            channels = get_channels()
            filtered_channels = filter_channels(channels)
            break
        else:
            break

except KeyboardInterrupt:
    print("\nExiting...")
    sys.exit(0)

# Send payments
channels = get_channels()
filtered_channels = filter_channels(channels)

if peer:
    send_payments_with_specified_peer(ln_address, amount, total_amount, interval_seconds, fee_rate, message, peer)
else:
    send_payments_auto_select_peer(ln_address, amount, total_amount, interval_seconds, fee_rate, message, filtered_channels)