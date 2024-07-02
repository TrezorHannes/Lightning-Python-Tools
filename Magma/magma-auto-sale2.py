# This is a local fork of https://github.com/jvxis/nr-tools
# enter the necessary settings in config.ini file in the parent dir

#Import Lybraries
import requests
import telebot
import json
from telebot import types
import subprocess
import time
import os
import schedule
from datetime import datetime
import configparser
import logging
import threading

# Get the path to the parent directory
parent_dir = os.path.dirname(os.path.abspath(__file__))

# path to the config.ini file located in the parent directory
config_file_path = os.path.join(parent_dir, '..', 'config.ini')
config = configparser.ConfigParser()
config.read(config_file_path)

# Variables
EXPIRE = 180000
limit_cost = 0.90
fee_rate_ppm = 350
API_MEMPOOL = 'https://mempool.space/api/v1/fees/recommended'
RETRY_DELAY_SECONDS = 60  # Retry every minute if we can't connect to the buyer
MAX_CONNECTION_RETRIES = 30 # Retry to connect for half an hour, than abort the script


TOKEN = config['telegram']['magma_bot_token']
AMBOSS_TOKEN = config['credentials']['amboss_authorization']
CHAT_ID = config['telegram']['telegram_user_id']

magma_channel_list = config['paths']['charge_lnd_path']
full_path_bos = config['system']['full_path_bos']

# Set up logging // Needs fixing @TrezorHannes
log_file_path = os.path.join(parent_dir, '..', 'logs', 'magma-auto-sale2.log')
logging.basicConfig(filename=log_file_path, level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
error_file_path = os.path.join(parent_dir, '..', 'logs', 'magma_channel_sale-error.log')


#Code
bot = telebot.TeleBot(TOKEN)
logging.info("Amboss Channel Open Bot Started")

def execute_lncli_addinvoice(amt, memo, expiry):
# Command to be executed
    command = (
        f"lncli addinvoice "
        f"--memo '{memo}' --amt {amt} --expiry {expiry}"
    )

    try:
        # Execute the command and capture the output
        result = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, error = result.communicate()
        output = output.decode("utf-8")
        error = error.decode("utf-8")

        # Log the command output and error
        logging.debug(f"Command Output: {output}")
        logging.error(f"Command Error: {error}")

        # Try to parse the JSON output
        try:
            output_json = json.loads(output)
            # Extract the required values
            r_hash = output_json.get("r_hash", "")
            payment_request = output_json.get("payment_request", "")
            return r_hash, payment_request

        except json.JSONDecodeError as json_error:
            # If not a valid JSON response, handle accordingly
            logging.exception(f"Error decoding JSON: {json_error}")
            return f"Error decoding JSON: {json_error}", None

    except subprocess.CalledProcessError as e:
        # Handle any errors that occur during command execution
        logging.exception(f"Error executing command: {e}")
        return f"Error executing command: {e}", None


def accept_order(order_id, payment_request):
    url = 'https://api.amboss.space/graphql'
    headers = {
        'content-type': 'application/json',
        'Authorization': f'Bearer {AMBOSS_TOKEN}',
    }
    query = '''
        mutation AcceptOrder($sellerAcceptOrderId: String!, $request: String!) {
          sellerAcceptOrder(id: $sellerAcceptOrderId, request: $request)
        }
    '''
    variables = {"sellerAcceptOrderId": order_id, "request": payment_request}

    response = requests.post(url, json={"query": query, "variables": variables}, headers=headers)
    return response.json()


def confirm_channel_point_to_amboss(order_id, transaction):
    url = 'https://api.amboss.space/graphql'
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {AMBOSS_TOKEN}'
    }

    graphql_query = f'mutation Mutation($sellerAddTransactionId: String!, $transaction: String!) {{\n  sellerAddTransaction(id: $sellerAddTransactionId, transaction: $transaction)\n}}'
    
    data = {
        'query': graphql_query,
        'variables': {
            'sellerAddTransactionId': order_id,
            'transaction': transaction
        }
    }

    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()  # Raise an HTTPError for bad responses (4xx or 5xx)

        json_response = response.json()

        if 'errors' in json_response:
            # Handle error in the JSON response and log it
            error_message = json_response['errors'][0]['message']
            log_content = f"Error in confirm_channel_point_to_amboss:\nOrder ID: {order_id}\nTransaction: {transaction}\nError Message: {error_message}\n"

            with open(error_file_path, "w") as log_file:
                log_file.write(log_content)

            return log_content
        else:
            return json_response

    except requests.exceptions.RequestException as e:
        logging.exception(f"Error making the request: {e}")
        return None
    

def get_channel_point(hash_to_find):
    def execute_lightning_command():
        command = [
            f"lncli",
            "pendingchannels"
        ]

        try:
            logging.info(f"Command: {command}")
            result = subprocess.run(command, capture_output=True, text=True, check=True)
            output = result.stdout

            # Parse JSON result
            result_json = json.loads(output)
            return result_json

        except subprocess.CalledProcessError as e:
            logging.exception(f"Error executing the command: {e}")
            return None
    
    result = execute_lightning_command()

    if result:
        pending_open_channels = result.get("pending_open_channels", [])

        for channel_info in pending_open_channels:
            channel_point = channel_info["channel"]["channel_point"]
            channel_hash = channel_point.split(":")[0]

            if channel_hash == hash_to_find:
                return channel_point

    return None


def execute_lnd_command(node_pub_key, fee_per_vbyte, formatted_outpoints, input_amount, fee_rate_ppm):
    # Format the command
    command = (
        f"lncli openchannel "
        f"--node_key {node_pub_key} --sat_per_vbyte={fee_per_vbyte} "
        f"{formatted_outpoints} --local_amt={input_amount} --fee_rate_ppm {fee_rate_ppm}"
    )
    logging.info(f"Executing command: {command}")
    
    try:
        # Run the command and capture both stdout and stderr
        result = subprocess.run(command, shell=True, check=False, capture_output=True, text=True)
        
        # Log both stdout and stderr regardless of the result
        logging.info(f"Command Output: {result.stdout}")
        logging.error(f"Command Error: {result.stderr}")

        if result.returncode == 0:
            try:
                output_json = json.loads(result.stdout)
                funding_txid = output_json.get("funding_txid")
                if funding_txid:
                    logging.info(f"Funding transaction ID: {funding_txid}")
                else:
                    logging.error("No funding transaction ID found in the command output.")
                return funding_txid
            except json.JSONDecodeError as json_error:
                logging.exception(f"Error decoding JSON: {json_error}")
                return None
        else:
            # Log a specific error message if the command fails
            logging.error(f"Command failed with return code {result.returncode}")
            return None

    except subprocess.CalledProcessError as e:
        # Handle command execution errors
        logging.exception(f"Error executing command: {e}")
        return None


def get_fast_fee():
    response = requests.get(API_MEMPOOL)
    data = response.json()
    if data:
        fast_fee = data['fastestFee']
        return fast_fee
    else:
        return None


def get_address_by_pubkey(peer_pubkey):
    url = 'https://api.amboss.space/graphql'
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {AMBOSS_TOKEN}'
    }

    query = f"""
    query List($pubkey: String!) {{
      getNode(pubkey: $pubkey) {{
        graph_info {{
          node {{
            addresses {{
              addr
            }}
          }}
        }}
      }}
    }}
    """

    variables = {
        "pubkey": peer_pubkey
    }

    payload = {
        "query": query,
        "variables": variables
    }

    response = requests.post(url, json=payload, headers=headers)

    if response.status_code == 200:
        data = response.json()
        addresses = data.get('data', {}).get('getNode', {}).get('graph_info', {}).get('node', {}).get('addresses', [])
        first_address = addresses[0]['addr'] if addresses else None

        if first_address:
            return f"{peer_pubkey}@{first_address}"
        else:
            return None
    else:
        logging.error(f"Error: {response.status_code}")
        return None


def connect_to_node(node_key_address, max_retries=MAX_CONNECTION_RETRIES):
    retries = 0
    while retries < max_retries:
        command = f"lncli connect {node_key_address} --timeout 120s"
        logging.info(f"Connecting to node: {command}")
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True)
            if result.returncode == 0:
                logging.info(f"Successfully connected to node {node_key_address}")
                return result.returncode  # Return the process return code
            elif "already connected to peer" in result.stderr:
                logging.info(f"Peer {node_key_address} is already connected.")
                return 0  # Return 0 to indicate success
            else:
                logging.error(f"Error connecting to node (attempt {retries + 1}): {result.stderr}")
                retries += 1
                time.sleep(RETRY_DELAY_SECONDS)  # Wait before retrying
        except subprocess.CalledProcessError as e:
            logging.error(f"Error executing lncli connect (attempt {retries + 1}): {e}")
            retries += 1
            time.sleep(RETRY_DELAY_SECONDS)  # Wait before retrying

    # If we reach this point, all retries have failed
    logging.error(f"Failed to connect to node {node_key_address} after {max_retries} retries.")
    return 1  # Return 1 or another non-zero value to indicate failure


def get_lncli_utxos():
    command = f"lncli listunspent --min_confs=3"
    process = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output, error = process.communicate()
    output = output.decode("utf-8")

    utxos = []

    try:
        data = json.loads(output)
        utxos = data.get("utxos", [])
    except json.JSONDecodeError as e:
        logging.exception(f"Error decoding lncli output: {e}")
    
    # Sort utxos based on amount_sat in reverse order
    utxos = sorted(utxos, key=lambda x: x.get("amount_sat", 0), reverse=True)
    
    logging.info(f"Utxos:{utxos}")
    return utxos


def calculate_transaction_size(utxos_needed):
    inputs_size = utxos_needed * 57.5  # Cada UTXO é de 57.5 vBytes
    outputs_size = 2 * 43  # Dois outputs de 43 vBytes cada
    overhead_size = 10.5  # Overhead de 10.5 vBytes
    total_size = inputs_size + outputs_size + overhead_size
    return total_size


def calculate_utxos_required_and_fees(amount_input, fee_per_vbyte):
    utxos_data = get_lncli_utxos()
    channel_size = float(amount_input)
    total = sum(utxo["amount_sat"] for utxo in utxos_data)
    utxos_needed = 0
    fee_cost = 0
    amount_with_fees = channel_size
    related_outpoints = []

    if total < channel_size:
        logging.error(f"There are not enough UTXOs to open a channel {channel_size} SATS. Total UTXOS: {total} SATS")
        return -1, 0, None

    #for utxo_amount, utxo_outpoint in zip(utxos_data['amounts'], utxos_data['outpoints']):
    for utxo in utxos_data:
        utxos_needed += 1
        transaction_size = calculate_transaction_size(utxos_needed)
        fee_cost = transaction_size * fee_per_vbyte
        amount_with_fees = channel_size + fee_cost

        related_outpoints.append(utxo['outpoint'])

        if utxo['amount_sat'] >= amount_with_fees:
            break
        channel_size -= utxo['amount_sat']

    return utxos_needed, fee_cost, related_outpoints if related_outpoints else None


def check_channel():
    logging.info("check_channel function called")
    url = 'https://api.amboss.space/graphql'
    headers = {
        'content-type': 'application/json',
        'Authorization': f'Bearer {AMBOSS_TOKEN}',
    }
    payload = {
        "query": "query List {\n  getUser {\n    market {\n      offer_orders {\n        list {\n          id\n          size\n          status\n        account\n        seller_invoice_amount\n        }\n      }\n    }\n  }\n}"
    }


    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()  # Raise an exception for 4xx and 5xx status codes

        data = response.json().get('data', {})
        market = data.get('getUser', {}).get('market', {})
        offer_orders = market.get('offer_orders', {}).get('list', [])

        # Log the entire offer list for debugging
        # logging.info(f"All Offers: {offer_orders}")

        # Find the first offer with status "WAITING_FOR_CHANNEL_OPEN"
        valid_channel_to_open = next((offer for offer in offer_orders if offer.get('status') == "WAITING_FOR_CHANNEL_OPEN"), None)

        # Log the found offer for debugging
        logging.info(f"Found Offer: {valid_channel_to_open}")

        if not valid_channel_to_open:
            logging.info("No orders with status 'WAITING_FOR_CHANNEL_OPEN' waiting for execution.")
            return None

        return valid_channel_to_open

    except requests.exceptions.RequestException as e:
        logging.exception(f"An error occurred while processing the check-channel request: {str(e)}")
        return None


#Check Buy offers
def check_offers():
    url = 'https://api.amboss.space/graphql'
    headers = {
        'content-type': 'application/json',
        'Authorization': f'Bearer {AMBOSS_TOKEN}',
    }
    payload = {
        "query": "query List {\n  getUser {\n    market {\n      offer_orders {\n        list {\n          id\n          seller_invoice_amount\n          status\n        }\n      }\n    }\n  }\n}"
    }

    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()  # Raise an exception for 4xx and 5xx status codes

        data = response.json()

        if data is None:  # Check if data is None
            logging.error("No data received from the API")
            return None
        
        response_data = response.json()
        # logging.info(f"Raw API Response: {response_data}")

        market = data.get('getUser', {}).get('market', {})
        # offer_orders = market.get('offer_orders', {}).get('list', [])
        offer_orders = response_data.get('data', {}).get('getUser', {}).get('market', {}).get('offer_orders', {}).get('list', [])

        for offer in offer_orders:
            logging.info(f"Offer ID: {offer.get('id')}, Status: {offer.get('status')}")
        # Find the first offer with status "VALID_CHANNEL_OPENING"
        valid_channel_opening_offer = next((offer for offer in offer_orders if offer.get('status') == "WAITING_FOR_SELLER_APPROVAL"), None)

        # Log the found offer for debugging
        logging.info(f"Found Offer: {valid_channel_opening_offer}")

        if not valid_channel_opening_offer:
            logging.info("No orders with status 'WAITING_FOR_SELLER_APPROVAL' waiting for approval.")
            return None

        return valid_channel_opening_offer

    except requests.exceptions.RequestException as e:
        logging.exception(f"An error occurred while processing the check-offers request: {str(e)}")
        return None


# Function Channel Open
def open_channel(pubkey, size, invoice):
    # get fastest fee
    logging.info("Getting fastest fee...")
    fee_rate = get_fast_fee()
    formatted_outpoints = None
    if fee_rate:
        logging.info(f"Fastest Fee:{fee_rate} sat/vB")
       # Check UTXOS and Fee Cost
        logging.info("Getting UTXOs, Fee Cost and Outpoints to open the channel")
        utxos_needed, fee_cost, related_outpoints = calculate_utxos_required_and_fees(size,fee_rate)
       # Check if enough UTXOS
        if utxos_needed == -1:
            msg_open = f"There isn't enough confirmed Balance to open a {size} SATS channel"
            logging.info(msg_open)
            return -1, msg_open 
        # Check if Fee Cost is less than the Invoice
        if (fee_cost) >= float(invoice):
            msg_open = f"Can't open this channel now, the fee {fee_cost} is bigger or equal to {limit_cost*100}% of the Invoice paid by customer"
            logging.info(msg_open)
            return -2, msg_open
        # Good to open channel
        if related_outpoints is not None:
            formatted_outpoints = ' '.join([f'--utxo {outpoint}' for outpoint in related_outpoints])
            logging.info(f"Opening Channel: {pubkey}")
            # Run function to open channel
        else:
        # Handle the case when related_outpoints is None
            logging.info("No related outpoints found.")
        logging.info(f"Opening Channel: {pubkey}")
        # Run function to open channel
        funding_tx = execute_lnd_command(pubkey, fee_rate, formatted_outpoints, size, fee_rate_ppm)
        if funding_tx is None:
            msg_open = f"Problem to execute the LNCLI command to open the channel. Please check the Log Files"
            logging.info(msg_open)
            return -3, msg_open
        msg_open = f"Channel opened with funding transaction: {funding_tx}"
        logging.info(msg_open)
        return funding_tx, msg_open       

    else:
        return None


def bos_confirm_income(amount, peer_pubkey):
    command = (
        f"{config['system']['full_path_bos']} send {config['info']['NODE']} "
        f"--amount {amount} --avoid-high-fee-routes --message 'HODLmeTight Amboss Channel Sale with {peer_pubkey}'"
    )
    logging.info(f"Executing BOS command: {command}")

    try:
        result = subprocess.run(command, shell=True, check=True, capture_output=True, text=True)
        logging.info(f"BOS Command Output: {result.stdout}")
        bot.send_message(CHAT_ID, text=f"BOS Command Output: {result.stdout}")
        return result.stdout
    except subprocess.CalledProcessError as e:
        logging.error(f"Error executing BOS command: {e}")
        bot.send_message(CHAT_ID, text=f"Error executing BOS command: {e}")
        return None
    

@bot.message_handler(commands=['channelopen'])
def send_telegram_message(message):
    logging.info("send_telegram_message function called")
    if message is None:
        # If message is not provided, create a dummy message for default behavior
        class DummyMessage:
            def __init__(self):
                self.chat = DummyChat()

        class DummyChat:
            def __init__(self):
                self.id = CHAT_ID  # Provide a default chat ID

        message = DummyMessage()
    # Get the current date and time
    current_datetime = datetime.now()

    # Format and Log the current date and time
    formatted_datetime = current_datetime.strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"Date and Time: {formatted_datetime}")
    # bot.send_message(message.chat.id, text="Checking new Orders...")
    logging.info("Checking new Orders...")
    valid_channel_opening_offer = check_offers()

    if not valid_channel_opening_offer:
        # bot.send_message(message.chat.id, text="No Magma orders waiting for your approval.")
        logging.info("No Magma orders waiting for your approval.")
    else:

        # Display the details of the valid channel opening offer
        bot.send_message(message.chat.id, text="Found Order:")
        formatted_offer = f"ID: {valid_channel_opening_offer['id']}\n"
        formatted_offer += f"Amount: {valid_channel_opening_offer['seller_invoice_amount']}\n"
        formatted_offer += f"Status: {valid_channel_opening_offer['status']}\n"

        bot.send_message(message.chat.id, text=formatted_offer)

        # Call the invoice function
        bot.send_message(message.chat.id, text=f"Generating Invoice of {valid_channel_opening_offer['seller_invoice_amount']} sats...")
        invoice_hash, invoice_request = execute_lncli_addinvoice(valid_channel_opening_offer['seller_invoice_amount'],f"Magma-Channel-Sale-Order-ID:{valid_channel_opening_offer['id']}", str(EXPIRE))
        if "Error" in invoice_hash:
            logging.info(invoice_hash)
            bot.send_message(message.chat.id, text=invoice_hash)
            return

        # Log the invoice result for debugging
        logging.debug("Invoice Result:", invoice_request)
        # Send the payment_request content to Telegram
        if invoice_request is not None:
            bot.send_message(message.chat.id, str(invoice_request))
        
        # Accept the order
        bot.send_message(message.chat.id, f"Accepting Order: {valid_channel_opening_offer['id']}")
        accept_result = accept_order(valid_channel_opening_offer['id'], invoice_request)
        logging.info(f"Order Acceptance Result: {accept_result}")
        bot.send_message(message.chat.id, text=f"Order Acceptance Result: {accept_result}")
    
        # Check if the order acceptance was successful
        if 'data' in accept_result and 'sellerAcceptOrder' in accept_result['data']:
            if accept_result['data']['sellerAcceptOrder']:
                success_message = "Invoice Successfully Sent to Amboss. Now you need to wait for Buyer payment to open the channel."
                bot.send_message(message.chat.id, text=success_message)
                logging.info(success_message)
            else:
                failure_message = "Failed to accept the order. Check the accept_result for details."
                bot.send_message(message.chat.id, text=failure_message)
                logging.error(failure_message)
                return
        
        else:
            error_message = "Unexpected format in the order acceptance result. Check the accept_result for details."
            bot.send_message(message.chat.id, text=error_message)
            logging.error(error_message)
            logging.error(f"Unexpected Order Acceptance Result Format: {accept_result}")
            return
    
    # Wait seven minutes to check if the buyer pre-paid the offer
    time.sleep(420)
    # Check if there is no error on a previous attempt to open a channel or confirm channel point to amboss
    
    if not os.path.exists(error_file_path):
        # bot.send_message(message.chat.id, text="Checking Channels to Open...")
        logging.info("Checking Channels to Open...")
        valid_channel_to_open = check_channel()

        if not valid_channel_to_open:
            # bot.send_message(message.chat.id, text="No Channels pending to open.")
            logging.info("No Channels pending to open.")
            return

        # Display the details of the valid channel opening offer
        bot.send_message(message.chat.id, text="Order:")
        formatted_offer = f"ID: {valid_channel_to_open['id']}\n"
        formatted_offer += f"Customer: {valid_channel_to_open['account']}\n"
        formatted_offer += f"Size: {valid_channel_to_open['size']} SATS\n"
        formatted_offer += f"Invoice: {valid_channel_to_open['seller_invoice_amount']} SATS\n"
        formatted_offer += f"Status: {valid_channel_to_open['status']}\n"

        bot.send_message(message.chat.id, text=formatted_offer)

        #Connecting to Peer
        bot.send_message(message.chat.id, text=f"Connecting to peer: {valid_channel_to_open['account']}")
        customer_addr = get_address_by_pubkey(valid_channel_to_open['account'])
        #Connect
        node_connection = connect_to_node(customer_addr)
        if node_connection == 0:
            logging.info(f"Successfully connected to node {customer_addr}")
            bot.send_message(message.chat.id, text=f"Successfully connected to node {customer_addr}")
        
        else:
            logging.error(f"Error connecting to node {customer_addr}:")
            bot.send_message(message.chat.id, text=f"Can't connect to node {customer_addr}. Maybe it is already connected trying to open channel anyway")

        #Open Channel
        
        bot.send_message(message.chat.id, text=f"Open a {valid_channel_to_open['size']} SATS channel")    
        funding_tx, msg_open = open_channel(valid_channel_to_open['account'], valid_channel_to_open['size'], valid_channel_to_open['seller_invoice_amount']) # type: ignore
        # Deal with  errors and show on Telegram
        if funding_tx == -1 or funding_tx == -2 or funding_tx == -3:
            bot.send_message(message.chat.id, text=msg_open)
            return
        # Send funding tx to Telegram
        bot.send_message(message.chat.id, text=msg_open)
        logging.info("Waiting 10 seconds to get channel point...")
        bot.send_message(message.chat.id, text="Waiting 10 seconds to get channel point...")
        # Wait 10 seconds to get channel point
        time.sleep(10)

        # Get Channel Point
        channel_point = get_channel_point(funding_tx)
        if channel_point is None:
            #log_file_path = "amboss_channel_point.log"
            msg_cp = f"Can't get channel point, please check the log file {log_file_path} and try to get it manually from LNDG for the funding txid: {funding_tx}"
            logging.error(msg_cp)
            bot.send_message(message.chat.id,text=msg_cp)
            # Create the log file and write the channel_point value
            with open(log_file_path, "w") as log_file:
                log_file.write(funding_tx)
            return
        logging.info(f"Channel Point: {channel_point}")
        bot.send_message(message.chat.id, text=f"Channel Point: {channel_point}")

        logging.info("Waiting 10 seconds to Confirm Channel Point to Magma...")
        bot.send_message(message.chat.id, text="Waiting 10 seconds to Confirm Channel Point to Magma...")
        # Wait 10 seconds to get channel point
        time.sleep(10)
        # Send Channel Point to Amboss
        logging.info("Confirming Channel to Amboss...")
        bot.send_message(message.chat.id, text= "Confirming Channel to Amboss...")
        channel_confirmed = confirm_channel_point_to_amboss(valid_channel_to_open['id'],channel_point)
        if channel_confirmed is None or "Error" in channel_confirmed:
            #log_file_path = "amboss_channel_point.log"
            if isinstance(channel_confirmed, str) and "Error" in channel_confirmed:
                msg_confirmed = channel_confirmed
            else:
                msg_confirmed = f"Can't confirm channel point {channel_point} to Amboss, check the log file {log_file_path} and try to do it manually"
            logging.info(msg_confirmed)
            bot.send_message(message.chat.id, text=msg_confirmed)
            # Create the log file and write the channel_point value
            logging.error(channel_point)
            return
        msg_confirmed = "Opened Channel confirmed to Amboss"
        logging.info(msg_confirmed)
        logging.info(f"Result: {channel_confirmed}")
        bot.send_message(message.chat.id, text=msg_confirmed)
        bot.send_message(message.chat.id, text=f"Result: {channel_confirmed}")

        # We'll send the same invoice amount to ourselves to allow for LNDg to pick this up as a net-positive income for accounting.
        # Check your keysends table to a manual mark to add it to your PNL
        bos_result = bos_confirm_income(valid_channel_to_open['seller_invoice_amount'], peer_pubkey=valid_channel_to_open['peer_pubkey'])
        if bos_result:
            logging.info("BOS command executed successfully.")
        else:
            logging.error("BOS command execution failed.")
    elif os.path.exists(error_file_path):
        bot.send_message(message.chat.id, text=f"The log file {error_file_path} already exists. This means you need to check if there is a pending channel to confirm to Amboss. Check the {log_file_path} content")


@bot.message_handler(commands=['runnow'])
def handle_command(message):
    logging.info("Executing bot behavior triggered by Telegram command.")
    execute_bot_behavior()


def execute_bot_behavior():
    # This function contains the logic you want to execute
    logging.info("Executing bot behavior...")
    send_telegram_message(None)  # Pass None as a placeholder for the message parameter


if __name__ == "__main__":
    # Check if the error log file exists
    if not os.path.exists(error_file_path):
        # Schedule the bot behavior to run every 20 minutes
        schedule.every(20).minutes.do(execute_bot_behavior)

        # Start the bot in non-blocking mode
        threading.Thread(target=lambda: bot.polling(none_stop=True)).start()

        # Run scheduled tasks in the main thread
        while True:
            schedule.run_pending()
            time.sleep(1)  # Ensure there's a small delay to prevent high CPU usage
    else:
        logging.info(f"The log file {error_file_path} already exists. This means you need to check if there is a pending channel to confirm to Amboss. Check the {log_file_path} content")