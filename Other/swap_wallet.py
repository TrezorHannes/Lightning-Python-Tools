import os
import time
import subprocess
import json
import requests

channel  = 0
filtered_channels = []
success_counter = 1

def get_node_alias(pub_key):
    try:
        response = requests.get(f"https://mempool.space/api/v1/lightning/nodes/{pub_key}")
        data = response.json()
        return data.get('alias', '')
    except Exception as e:
        print(f"Error fetching node alias: {str(e)}")
        return pub_key

def execute_command(command):
    try:
        result = subprocess.run(command, shell=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return result.stdout.decode('utf-8')
    except subprocess.CalledProcessError as e:
        print(f"Error executing command: {e}")
        return None

def filter_channels(data):
    channels = data.get('channels', [])

    global filtered_channels
    for channel in channels:
        local_balance = int(channel.get('local_balance', 0))
        capacity = int(channel.get('capacity', 1))

        # Check if local_balance is equal to or more than 30% of capacity
        if local_balance >= 0.3 * capacity:
            filtered_channels.append({
                'remote_pubkey': channel.get('remote_pubkey', ''),
                'local_balance': local_balance,
                'capacity': capacity
            })

    return filtered_channels

    

def execute_transaction(ln_address, amount, total_amount, interval_seconds,fee_rate, message, peer):
    global channel
    global success_counter
    while total_amount > 0:
        # Validate the amount as a number
        try:
            amount = int(amount)
        except ValueError:
            print("Invalid amount. Please enter a valid number.")
            break

        # Build the command with user input
        if peer is not None:
            comando = f"bos send {ln_address} --amount {amount} --message {message} --max-fee-rate {fee_rate} --out {peer}"
        else:
            print(f"Total peers: {len(filtered_channels)}")
            if channel < len(filtered_channels):
                print(f"Peer:{channel} - {get_node_alias(filtered_channels[channel]['remote_pubkey'])}")
                comando = f"bos send {ln_address} --amount {amount} --message {message} --max-fee-rate {fee_rate} --out {filtered_channels[channel]['remote_pubkey']}"
            else:
                print("Starting random peers...")
                comando = f"bos send {ln_address} --amount {amount} --message {message} --max-fee-rate {fee_rate}"
        print(f"Executing command: {comando}\n")
        output = subprocess.run(comando, shell=True, capture_output=True, text=True)

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
        
        
# Get user input for LN address, amount, and total amount
ln_address = input("Enter LN address: ")
total_amount_to_transfer = input("Enter total amount to transfer: ")
amount = input("Enter amount per transaction: ")
interval = input("Enter the interval in seconds between transactions: ")
fee_rate = input("Enter the max fee rate in ppm: ")
message = input("Payment Message: ")
peer = input("Out Peer Alias or Pubkey: ")
if not peer:
    peer = None
    print("\nNo peer specified, trying first with heavy outbound peers...")
    print("Getting peers with local balance >= 30%...")
    # Execute the lncli command
    lncli_command = "/media/jvx/Umbrel-JV1/scripts/app compose lightning exec lnd lncli listchannels"
    command_output = execute_command(lncli_command)
    # Parse the JSON output
    data = json.loads(command_output)

    # Filter and print the channels
    filtered_channels = filter_channels(data)
    
interval_seconds = int(interval)
total_amount = int(total_amount_to_transfer)
execute_transaction(ln_address, amount, total_amount, interval_seconds, fee_rate, message, peer)