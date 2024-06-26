# Script: ps_peers.py to show available PeerSwap List in Command-Line
# Author: TheWall
# Finetuned: Hakuna@HODLmeTight

# === Required Imports ===
import json
import subprocess
from prettytable import PrettyTable

# Get your l-btc balance
def get_lbtc_balance():
    result = subprocess.run(['pscli', 'lbtc-getbalance'], stdout=subprocess.PIPE)
    balance_data = json.loads(result.stdout)
    sat_amount = balance_data['sat_amount']
    formatted_balance = "{:,}".format(int(sat_amount))
    return formatted_balance

# Function to execute the command and return JSON output
def get_pscli_listpeers_output():
    result = subprocess.run(['pscli', 'listpeers'], stdout=subprocess.PIPE)
    return json.loads(result.stdout)

# Alt function to get the output of 'lncli listchannels'
def get_lncli_listchannels_output():
    result = subprocess.run(['lncli', 'listchannels'], stdout=subprocess.PIPE)
    return json.loads(result.stdout)

# Function to get our own node's public key
def get_local_node_pubkey():
    result = subprocess.run(['lncli', 'getinfo'], stdout=subprocess.PIPE)
    info = json.loads(result.stdout)
    return info['identity_pubkey']

# Function to get channel fee information - tricky, since sometimes we're node1, sometimes node2 in getchainfo
def get_channel_fee_info(chan_id, local_pubkey):
    result = subprocess.run(['lncli', 'getchaninfo', '--chan_id', str(chan_id)], stdout=subprocess.PIPE)
    chan_info = json.loads(result.stdout)
    node1_pub = chan_info['node1_pub']
    node2_pub = chan_info['node2_pub']
    own_fee_rate = chan_info['node1_policy']['fee_rate_milli_msat'] if local_pubkey == node1_pub else chan_info['node2_policy']['fee_rate_milli_msat']
    peer_fee_rate = chan_info['node2_policy']['fee_rate_milli_msat'] if local_pubkey == node1_pub else chan_info['node1_policy']['fee_rate_milli_msat']
    return own_fee_rate, peer_fee_rate


# Alt function to match alias using node_id
def find_alias_by_node_id(node_id):
    channels_data = get_lncli_listchannels_output()
    for channel in channels_data['channels']:
        if channel['remote_pubkey'] == node_id:
            return channel['peer_alias']
    return None

# Main script
def main():
    local_pubkey = get_local_node_pubkey()  # Get the local node's public key
    peers_data = get_pscli_listpeers_output()

    lbtc_balance = get_lbtc_balance()  # Store the balance 

    # Creating a PrettyTable
    table = PrettyTable()
    table.field_names = ["Alias", "Trusted", "Assets", "Channel ID", "Local Balance", "Remote Balance", "Own Fee-Rate", "Peer Fee-Rate", "Active"]

    # Sort peers by 'Trusted' field
    sorted_peers = sorted(peers_data['peers'], key=lambda x: not x['swaps_allowed'])

    for peer in sorted_peers:
        if peer['channels']:  # Check if there are channels
            alias = find_alias_by_node_id(peer['node_id'])  
            # alias = find_alias_by_node_id(graph_data, peer['node_id'])
            trusted = "Yes" if peer['swaps_allowed'] else "No"
            assets = ', '.join(peer['supported_assets'])

            # Add rows for each channel
            for channel in peer['channels']:
                # Retrieve fee rates for the current channel
                own_fee_rate, peer_fee_rate = get_channel_fee_info(channel['channel_id'], local_pubkey)
                # Determine the active status
                active_status = "✅" if channel['active'] else "🚫"
                table.add_row([
                    alias,
                    trusted,
                    assets,
                    channel['channel_id'],
                    "{:,}".format(int(channel['local_balance'])),
                    "{:,}".format(int(channel['remote_balance'])),
                    own_fee_rate,
                    peer_fee_rate,
                    active_status
                ])

    table.title = f"PeerSwap List - LBTC Balance: {lbtc_balance}"

    print(table)

get_lbtc_balance()
if __name__ == "__main__":
    main()