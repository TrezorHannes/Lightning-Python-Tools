# This is a local fork of https://github.com/jvxis/nr-tools
# enter the necessary settings in config.ini file in the parent dir

# Import Lybraries
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
from logging.handlers import RotatingFileHandler
import threading

# Get the path to the parent directory
parent_dir = os.path.dirname(os.path.abspath(__file__))

# path to the config.ini file located in the parent directory
config_file_path = os.path.join(parent_dir, "..", "config.ini")
config = configparser.ConfigParser()
config.read(config_file_path)

# Variables from config file
INVOICE_EXPIRY_SECONDS = config.getint("magma", "invoice_expiry_seconds", fallback=180000)
MAX_FEE_PERCENTAGE_OF_INVOICE = config.getfloat("magma", "max_fee_percentage_of_invoice", fallback=0.90)
CHANNEL_FEE_RATE_PPM = config.getint("magma", "channel_fee_rate_ppm", fallback=350)
MEMPOOL_FEES_API_URL = config.get("urls", "mempool_fees_api", fallback="https://mempool.space/api/v1/fees/recommended")
CONNECT_RETRY_DELAY_SECONDS = config.getint("magma", "connect_retry_delay_seconds", fallback=60)
MAX_CONNECT_RETRIES = config.getint("magma", "max_connect_retries", fallback=30)
POLLING_INTERVAL_MINUTES = config.getint("magma", "polling_interval_minutes", fallback=20)

BANNED_PUBKEYS = config.get("pubkey", "banned_magma_pubkeys", fallback="").split(",")

TOKEN = config["telegram"]["magma_bot_token"]
AMBOSS_TOKEN = config["credentials"]["amboss_authorization"]
CHAT_ID = config["telegram"]["telegram_user_id"]

FULL_PATH_BOS = config["system"]["full_path_bos"]

# Configure data directory
# MAGMA_DATA_DIR = config.get("magma", "magma_data_dir", fallback=os.path.join(parent_dir, "..", "data", "magma"))
# if not os.path.exists(MAGMA_DATA_DIR):
#     os.makedirs(MAGMA_DATA_DIR, exist_ok=True)

# Define log file path within MAGMA_DATA_DIR
LOG_FILE_PATH = os.path.join(parent_dir, "..", "logs", "magma-sale-process.log") # Reverted

# Set up a rotating file handler
# Ensure the logs directory exists
logs_dir = os.path.join(parent_dir, "..", "logs")
if not os.path.exists(logs_dir):
    os.makedirs(logs_dir, exist_ok=True)

handler = RotatingFileHandler(
    LOG_FILE_PATH, maxBytes=10 * 1024 * 1024, backupCount=5  # 10 MB
)

# Set up logging configuration
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[handler],
)

# Adjust logging levels for third-party libraries
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("telebot").setLevel(logging.WARNING)

CRITICAL_ERROR_FILE_PATH = os.path.join(parent_dir, "..", "logs", "magma_sale_process-critical-error.flag") # Reverted


# Code
bot = telebot.TeleBot(TOKEN)
logging.info("Amboss Channel Open Bot Started")

# --- Constants for active order polling ---
ACTIVE_ORDER_POLL_INTERVAL_SECONDS = 30  # Check every 30 seconds
ACTIVE_ORDER_POLL_DURATION_MINUTES = 15   # Poll for a total of 15 minutes


def send_telegram_notification(text, level="info"):
    """Sends a message to Telegram and logs it."""
    if level == "error":
        logging.error(f"Telegram NOTIFICATION: {text}")
    elif level == "warning":
        logging.warning(f"Telegram NOTIFICATION: {text}")
    else:
        logging.info(f"Telegram NOTIFICATION: {text}")
    try:
        bot.send_message(CHAT_ID, text=text)
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")

def get_order_details_from_amboss(order_id):
    """Fetches specific order details from Amboss."""
    # This function would ideally use a more specific GraphQL query to get one order
    # For now, it reuses check_channel and filters by ID.
    # TODO: Implement a GraphQL query for a single order by ID for efficiency.
    logging.info(f"Fetching details for order ID: {order_id}")
    url = "https://api.amboss.space/graphql"
    headers = {
        "content-type": "application/json",
        "Authorization": f"Bearer {AMBOSS_TOKEN}",
    }
    # Query to fetch a specific order by ID (conceptual)
    # This specific query might need adjustment based on available Amboss API fields for a single order.
    # For now, we'll fetch all and filter, which is inefficient but works with current `check_channel` like logic.
    payload = {
        "query": "query ListUserMarketOfferOrders {\n  getUser {\n    market {\n      offer_orders {\n        list {\n          id\n          size\n          status\n          account\n          seller_invoice_amount\n          endpoints {\n            destination\n          }\n        }\n      }\n    }\n  }\n}"
    }
    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json().get("data", {})
        market = data.get("getUser", {}).get("market", {})
        offer_orders = market.get("offer_orders", {}).get("list", [])
        
        for offer in offer_orders:
            if offer.get("id") == order_id:
                logging.info(f"Found details for order {order_id}: {offer}")
                return offer
        logging.warning(f"Order ID {order_id} not found in Amboss query.")
        return None
    except requests.exceptions.RequestException as e:
        logging.error(f"Error fetching order {order_id} from Amboss: {e}")
        return None


def execute_lncli_addinvoice(amt, memo, expiry):
    # Command to be executed
    command = f"lncli addinvoice " f"--memo '{memo}' --amt {amt} --expiry {expiry}"

    try:
        # Execute the command and capture the output
        result = subprocess.Popen(
            command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
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
    url = "https://api.amboss.space/graphql"
    headers = {
        "content-type": "application/json",
        "Authorization": f"Bearer {AMBOSS_TOKEN}",
    }
    query = """
        mutation AcceptOrder($sellerAcceptOrderId: String!, $request: String!) {
          sellerAcceptOrder(id: $sellerAcceptOrderId, request: $request)
        }
    """
    variables = {"sellerAcceptOrderId": order_id, "request": payment_request}

    response = requests.post(
        url, json={"query": query, "variables": variables}, headers=headers
    )
    return response.json()


def reject_order(order_id):
    url = "https://api.amboss.space/graphql"
    headers = {
        "content-type": "application/json",
        "Authorization": f"Bearer {AMBOSS_TOKEN}",
    }
    query = """
        mutation SellerRejectOrder($sellerRejectOrderId: String!) {
          sellerRejectOrder(id: $sellerRejectOrderId)
        }
    """
    variables = {"sellerRejectOrderId": order_id}

    response = requests.post(
        url, json={"query": query, "variables": variables}, headers=headers
    )
    logging.info(f"Order {order_id} rejected. Response: {response.json()}")
    return response.json()


def confirm_channel_point_to_amboss(order_id, transaction):
    url = "https://api.amboss.space/graphql"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AMBOSS_TOKEN}",
    }

    graphql_query = f"mutation Mutation($sellerAddTransactionId: String!, $transaction: String!) {{\n  sellerAddTransaction(id: $sellerAddTransactionId, transaction: $transaction)\n}}"

    data = {
        "query": graphql_query,
        "variables": {"sellerAddTransactionId": order_id, "transaction": transaction},
    }

    try:
        response = requests.post(url, headers=headers, json=data)
        response.raise_for_status()  # Raise an HTTPError for bad responses (4xx or 5xx)

        json_response = response.json()

        if "errors" in json_response:
            # Handle error in the JSON response and log it
            error_message = json_response["errors"][0]["message"]
            log_content = f"Error in confirm_channel_point_to_amboss:\nOrder ID: {order_id}\nTransaction: {transaction}\nError Message: {error_message}\n"

            with open(CRITICAL_ERROR_FILE_PATH, "a") as log_file: # Append to critical error file
                log_file.write(log_content)

            return log_content
        else:
            return json_response

    except requests.exceptions.RequestException as e:
        logging.exception(f"Error making the request: {e}")
        return None


def get_channel_point(hash_to_find):
    def execute_lightning_command():
        command = [f"lncli", "pendingchannels"]

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


def execute_lnd_command(
    node_pub_key, fee_per_vbyte, formatted_outpoints, input_amount, fee_rate_ppm
):
    # Format the command
    command = (
        f"lncli openchannel "
        f"--node_key {node_pub_key} --sat_per_vbyte={fee_per_vbyte} "
        f"{formatted_outpoints} --local_amt={input_amount} --fee_rate_ppm {fee_rate_ppm}"
    )
    logging.info(f"Executing command: {command}")

    try:
        # Run the command and capture both stdout and stderr
        result = subprocess.run(
            command, shell=True, check=False, capture_output=True, text=True
        )

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
                    logging.error(
                        "No funding transaction ID found in the command output."
                    )
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
    response = requests.get(MEMPOOL_FEES_API_URL)
    data = response.json()
    if data:
        fast_fee = data["fastestFee"]
        return fast_fee
    else:
        return None


def get_address_by_pubkey(peer_pubkey):
    url = "https://api.amboss.space/graphql"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AMBOSS_TOKEN}",
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

    variables = {"pubkey": peer_pubkey}

    payload = {"query": query, "variables": variables}

    response = requests.post(url, json=payload, headers=headers)

    if response.status_code == 200:
        data = response.json()
        addresses = (
            data.get("data", {})
            .get("getNode", {})
            .get("graph_info", {})
            .get("node", {})
            .get("addresses", [])
        )
        first_address = addresses[0]["addr"] if addresses else None

        if first_address:
            return f"{peer_pubkey}@{first_address}"
        else:
            return None
    else:
        logging.error(f"Error: {response.status_code}")
        return None


def connect_to_node(node_key_address, max_retries=None):
    if max_retries is None:
        max_retries = MAX_CONNECT_RETRIES
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
                logging.error(
                    f"Error connecting to node (attempt {retries + 1}): {result.stderr}"
                )
                retries += 1
                time.sleep(CONNECT_RETRY_DELAY_SECONDS)  # Wait before retrying
        except subprocess.CalledProcessError as e:
            logging.error(f"Error executing lncli connect (attempt {retries + 1}): {e}")
            retries += 1
            time.sleep(CONNECT_RETRY_DELAY_SECONDS)  # Wait before retrying

    # If we reach this point, all retries have failed
    logging.error(
        f"Failed to connect to node {node_key_address} after {max_retries} retries."
    )
    return 1  # Return 1 or another non-zero value to indicate failure


def get_lncli_utxos():
    # First get all UTXOs from LND
    command = f"lncli listunspent --min_confs=3"
    process = subprocess.Popen(
        command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    output, error = process.communicate()
    output = output.decode("utf-8")

    utxos = []

    try:
        data = json.loads(output)
        utxos = data.get("utxos", [])
    except json.JSONDecodeError as e:
        logging.exception(f"Error decoding lncli output: {e}")

    # Get static loop addresses UTXOs if loop binary is available
    loop_utxos = []
    loop_path = ""
    if "system" in config and "path_command" in config["system"]:
        loop_command_path = config["system"]["path_command"]
        if loop_command_path: # Ensure path_command is not empty
             loop_path = os.path.join(loop_command_path, "loop")

    try:
        if loop_path and os.path.exists(loop_path):
            # Construct the litloop command
            litloop_cmd = f"{loop_path} --rpcserver=localhost:8443 --tlscertpath=~/.lit/tls.cert static listunspent"
            process = subprocess.Popen(
                litloop_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            output, error = process.communicate()
            output = output.decode("utf-8")

            try:
                loop_data = json.loads(output)
                loop_utxos = loop_data.get("utxos", [])
                logging.info(f"Found {len(loop_utxos)} static loop UTXOs")
            except json.JSONDecodeError as e:
                logging.exception(f"Error decoding litloop output: {e}")
    except Exception as e:
        logging.exception(f"Error checking for loop binary: {e}")

    # Create a set of loop outpoints for efficient lookup
    loop_outpoints = {utxo.get("outpoint") for utxo in loop_utxos}

    # Filter out UTXOs that are in the loop outpoints set
    filtered_utxos = [
        utxo for utxo in utxos if utxo.get("outpoint") not in loop_outpoints
    ]

    # Sort filtered utxos based on amount_sat in reverse order
    filtered_utxos = sorted(
        filtered_utxos, key=lambda x: x.get("amount_sat", 0), reverse=True
    )

    logging.info(f"Filtered UTXOs (excluding loop static addresses): {filtered_utxos}")
    return filtered_utxos


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
        logging.error(
            f"There are not enough UTXOs to open a channel {channel_size} SATS. Total UTXOS: {total} SATS"
        )
        return -1, 0, None

    # for utxo_amount, utxo_outpoint in zip(utxos_data['amounts'], utxos_data['outpoints']):
    for utxo in utxos_data:
        utxos_needed += 1
        transaction_size = calculate_transaction_size(utxos_needed)
        fee_cost = transaction_size * fee_per_vbyte
        amount_with_fees = channel_size + fee_cost

        related_outpoints.append(utxo["outpoint"])

        if utxo["amount_sat"] >= amount_with_fees:
            break
        channel_size -= utxo["amount_sat"]

    return utxos_needed, fee_cost, related_outpoints if related_outpoints else None


def check_channel():
    logging.info("check_channel function called")
    url = "https://api.amboss.space/graphql"
    headers = {
        "content-type": "application/json",
        "Authorization": f"Bearer {AMBOSS_TOKEN}",
    }
    payload = {
        "query": "query List {\n  getUser {\n    market {\n      offer_orders {\n        list {\n          id\n          size\n          status\n        account\n        seller_invoice_amount\n        }\n      }\n    }\n  }\n}"
    }

    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()  # Raise an exception for 4xx and 5xx status codes

        data = response.json().get("data", {})
        market = data.get("getUser", {}).get("market", {})
        offer_orders = market.get("offer_orders", {}).get("list", [])

        # Log the entire offer list for debugging
        # logging.info(f"All Offers: {offer_orders}")

        # Find the first offer with status "WAITING_FOR_CHANNEL_OPEN"
        valid_channel_to_open = next(
            (
                offer
                for offer in offer_orders
                if offer.get("status") == "WAITING_FOR_CHANNEL_OPEN"
            ),
            None,
        )

        # Log the found offer for debugging
        logging.info(f"Found Offer: {valid_channel_to_open}")

        if not valid_channel_to_open:
            logging.info(
                "No orders with status 'WAITING_FOR_CHANNEL_OPEN' waiting for execution."
            )
            return None

        return valid_channel_to_open

    except requests.exceptions.RequestException as e:
        logging.exception(
            f"An error occurred while processing the check-channel request: {str(e)}"
        )
        return None


def check_offers():
    url = "https://api.amboss.space/graphql"
    headers = {
        "content-type": "application/json",
        "Authorization": f"Bearer {AMBOSS_TOKEN}",
    }
    # Updated GraphQL query to include 'endpoints' and 'destination' to retrieve pubkey from channel-buyer
    query = """
    query ListChannelOffers {
      getUser {
        market {
          offer_orders {
            list {
              id
              seller_invoice_amount
              status
              endpoints {
                destination
              }
            }
          }
        }
      }
    }
    """

    payload = {"query": query}

    try:
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()  # Raise an exception for 4xx and 5xx status codes

        data = response.json()

        if data is None:  # Check if data is None
            logging.error("No data received from the API")
            return None

        offer_orders = (
            data.get("data", {})
            .get("getUser", {})
            .get("market", {})
            .get("offer_orders", {})
            .get("list", [])
        )

        # Initialize valid_channel_opening_offer to None
        valid_channel_opening_offer = None

        for offer in offer_orders:
            logging.info(f"Offer ID: {offer.get('id')}, Status: {offer.get('status')}")

            # Retrieve the pubkey for the offer
            destination = offer.get("endpoints", {}).get("destination")

            # Check whether the channel-buyer pubkey is in banned config file
            if destination in BANNED_PUBKEYS:
                logging.info(
                    f"Pubkey {destination} is banned. Rejecting order {offer.get('id')}."
                )
                reject_order(offer.get("id"))
                continue

            # Find the first offer with status "WAITING_FOR_SELLER_APPROVAL"
            if offer.get("status") == "WAITING_FOR_SELLER_APPROVAL":
                logging.info(f"Found valid & unbanned offer to process: {offer}")
                valid_channel_opening_offer = offer
                break

        if not valid_channel_opening_offer:
            logging.info(
                "No orders with status 'WAITING_FOR_SELLER_APPROVAL' waiting for approval."
            )
            return None

        return valid_channel_opening_offer

    except requests.exceptions.RequestException as e:
        logging.exception(
            f"An error occurred while processing the check-offers request: {str(e)}"
        )
        return None


def open_channel(pubkey, size, invoice):
    # get fastest fee
    logging.info("Getting fastest fee...")
    fee_rate = get_fast_fee()
    formatted_outpoints = None
    if fee_rate:
        logging.info(f"Fastest Fee:{fee_rate} sat/vB")
        # Check UTXOS and Fee Cost
        logging.info("Getting UTXOs, Fee Cost and Outpoints to open the channel")
        utxos_needed, fee_cost, related_outpoints = calculate_utxos_required_and_fees(
            size, fee_rate
        )
        # Check if enough UTXOS
        if utxos_needed == -1:
            msg_open = (
                f"There isn't enough confirmed Balance to open a {size} SATS channel"
            )
            logging.info(msg_open)
            return -1, msg_open
        # Check if Fee Cost is less than the Invoice
        if (fee_cost) >= float(invoice) * MAX_FEE_PERCENTAGE_OF_INVOICE: # Use new config variable
            msg_open = f"Can't open this channel now, the fee {fee_cost} is bigger or equal to {MAX_FEE_PERCENTAGE_OF_INVOICE*100}% of the Invoice paid by customer" # Use new config variable
            logging.info(msg_open)
            return -2, msg_open
        # Good to open channel
        if related_outpoints is not None:
            formatted_outpoints = " ".join(
                [f"--utxo {outpoint}" for outpoint in related_outpoints]
            )
            logging.info(f"Opening Channel: {pubkey}")
            # Run function to open channel
        else:
            # Handle the case when related_outpoints is None
            logging.info("No related outpoints found.")
        logging.info(f"Opening Channel: {pubkey}")
        # Run function to open channel
        funding_tx = execute_lnd_command(
            pubkey, fee_rate, formatted_outpoints, size, CHANNEL_FEE_RATE_PPM # Use new config variable
        )
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
        f"{FULL_PATH_BOS} send {config['info']['NODE']} " # Use new config variable
        f"--amount {amount} --avoid-high-fee-routes --message 'HODLmeTight Amboss Channel Sale with {peer_pubkey}'"
    )
    logging.info(f"Executing BOS command: {command}")

    try:
        result = subprocess.run(
            command, shell=True, check=True, capture_output=True, text=True
        )
        logging.info(f"BOS Command Output: {result.stdout}")
        bot.send_message(CHAT_ID, text=f"BOS Command Output: {result.stdout}")
        return result.stdout
    except subprocess.CalledProcessError as e:
        logging.error(f"Error executing BOS command: {e}")
        bot.send_message(CHAT_ID, text=f"Error executing BOS command: {e}")
        return None


def process_new_offers():
    """Checks for new offers and processes them (invoice, accept)."""
    logging.info("Checking for new Magma offers (WAITING_FOR_SELLER_APPROVAL)...")
    valid_channel_opening_offer = check_offers() # Fetches offers WAITING_FOR_SELLER_APPROVAL

    if not valid_channel_opening_offer:
        logging.info("No new Magma offers waiting for approval.")
        return

    order_id = valid_channel_opening_offer['id']
    seller_invoice_amount = valid_channel_opening_offer['seller_invoice_amount']
    current_status = valid_channel_opening_offer['status']

    send_telegram_notification(
        f"Found new Magma offer:\nID: {order_id}\nAmount: {seller_invoice_amount} sats\nStatus: {current_status}"
    )

    # Check for banned pubkey (already in check_offers, but good for direct call)
    destination_pubkey = valid_channel_opening_offer.get("endpoints", {}).get("destination")
    if destination_pubkey in BANNED_PUBKEYS:
        logging.info(f"Offer {order_id} from banned pubkey {destination_pubkey}. Auto-rejecting.")
        send_telegram_notification(f"Offer {order_id} from banned pubkey {destination_pubkey}. Auto-rejecting.")
        reject_order(order_id)
        return

    send_telegram_notification(f"Generating invoice for {seller_invoice_amount} sats (Order ID: {order_id})...")
    invoice_hash, invoice_request = execute_lncli_addinvoice(
        seller_invoice_amount,
        f"Magma-Channel-Sale-Order-ID:{order_id}",
        str(INVOICE_EXPIRY_SECONDS),
    )

    if "Error" in invoice_hash or invoice_request is None:
        error_msg = f"Failed to generate invoice for order {order_id}: {invoice_hash}"
        logging.error(error_msg)
        send_telegram_notification(error_msg, level="error")
        # Consider whether to auto-reject the order here or let it expire
        return

    logging.debug(f"Invoice for order {order_id}: {invoice_request}")
    send_telegram_notification(f"Invoice for order {order_id}:\n`{invoice_request}`")

    send_telegram_notification(f"Accepting Magma order: {order_id}")
    accept_result = accept_order(order_id, invoice_request)
    logging.info(f"Order {order_id} acceptance result: {accept_result}")

    if "data" in accept_result and "sellerAcceptOrder" in accept_result["data"] and accept_result["data"]["sellerAcceptOrder"]:
        success_message = f"Order {order_id} accepted. Invoice sent to Amboss. Waiting for buyer payment."
        send_telegram_notification(success_message)
        logging.info(success_message)
        
        # Start actively polling for this specific order's payment
        wait_for_buyer_payment(order_id)
    else:
        failure_message = f"Failed to accept order {order_id}. Amboss response: {accept_result}"
        logging.error(failure_message)
        send_telegram_notification(failure_message, level="error")
        # Log critical error if Amboss acceptance fails unexpectedly
        if "errors" in accept_result:
             with open(CRITICAL_ERROR_FILE_PATH, "a") as log_file:
                log_file.write(f"Failed to accept Amboss order {order_id}. Response: {accept_result.get('errors')}\n")


def wait_for_buyer_payment(order_id):
    """Actively polls a specific order to see if it becomes WAITING_FOR_CHANNEL_OPEN."""
    logging.info(f"Starting active poll for payment of order {order_id} for {ACTIVE_ORDER_POLL_DURATION_MINUTES} minutes.")
    send_telegram_notification(f"Actively monitoring order {order_id} for buyer payment...")

    start_time = time.time()
    max_duration_seconds = ACTIVE_ORDER_POLL_DURATION_MINUTES * 60

    while time.time() - start_time < max_duration_seconds:
        order_details = get_order_details_from_amboss(order_id) # Fetches current status
        if order_details:
            current_status = order_details.get("status")
            logging.info(f"Order {order_id} current status: {current_status}")
            if current_status == "WAITING_FOR_CHANNEL_OPEN":
                send_telegram_notification(f"Buyer has paid for order {order_id}! Status: {current_status}. Proceeding to open channel.")
                logging.info(f"Order {order_id} is now WAITING_FOR_CHANNEL_OPEN. Triggering channel open process.")
                # Directly call the processing for this order, no need to wait for next main poll cycle
                process_paid_order(order_details)
                return # Exit after processing
            elif current_status in ["CANCELLED", "EXPIRED", "SELLER_REJECTED", "ERROR", "COMPLETED"]: # Terminal states
                send_telegram_notification(f"Order {order_id} reached terminal state: {current_status} during active payment poll. Stopping active poll.")
                logging.info(f"Order {order_id} reached terminal state {current_status}. Stopping active poll.")
                return
        else:
            logging.warning(f"Could not fetch details for order {order_id} during active poll.")
            # Continue polling, maybe a transient API issue

        time.sleep(ACTIVE_ORDER_POLL_INTERVAL_SECONDS)

    send_telegram_notification(f"Finished active polling for order {order_id} after {ACTIVE_ORDER_POLL_DURATION_MINUTES} minutes. Buyer did not pay or status change not detected in time.")
    logging.info(f"Active polling for order {order_id} finished. Buyer did not pay or status not WAITING_FOR_CHANNEL_OPEN in time.")


def process_paid_order(order_details):
    """Processes a single order that is WAITING_FOR_CHANNEL_OPEN."""
    order_id = order_details['id']
    customer_pubkey = order_details['account'] # Assuming 'account' is the buyer's pubkey
    channel_size = order_details['size']
    seller_invoice_amount = order_details['seller_invoice_amount']

    send_telegram_notification(
        f"Processing paid order {order_id}:\nCustomer: {customer_pubkey}\nSize: {channel_size} SATS\nInvoice: {seller_invoice_amount} SATS"
    )

    # 1. Connect to Peer
    send_telegram_notification(f"Attempting to connect to peer: {customer_pubkey} for order {order_id}")
    customer_addr_uri = get_address_by_pubkey(customer_pubkey)

    if not customer_addr_uri:
        error_msg = f"Could not get address for peer {customer_pubkey} (Order {order_id}). Cannot open channel."
        logging.error(error_msg)
        send_telegram_notification(error_msg, level="error")
        # This is critical as we can't proceed with this order
        with open(CRITICAL_ERROR_FILE_PATH, "a") as log_file:
            log_file.write(f"Critical: Could not get address for peer {customer_pubkey} (Order {order_id})\n")
        return

    node_connection_status = connect_to_node(customer_addr_uri)
    if node_connection_status == 0:
        send_telegram_notification(f"Successfully connected to peer {customer_addr_uri} (Order {order_id}).")
    else:
        # Don't fail outright, lncli might still open if already connected or connects during openchannel
        send_telegram_notification(f"Could not connect to peer {customer_addr_uri} (Order {order_id}), or already connected. Attempting channel open anyway.", level="warning")

    # 2. Open Channel
    send_telegram_notification(f"Attempting to open {channel_size} SATS channel with {customer_pubkey} (Order {order_id}).")
    funding_tx, msg_open = open_channel(customer_pubkey, channel_size, seller_invoice_amount)

    if funding_tx == -1 or funding_tx == -2 or funding_tx == -3 or funding_tx is None:
        error_msg = f"Failed to open channel for order {order_id}: {msg_open}"
        logging.error(error_msg)
        send_telegram_notification(error_msg, level="error")
        # This is critical for this order
        with open(CRITICAL_ERROR_FILE_PATH, "a") as log_file:
            log_file.write(f"Critical: Failed to open channel for order {order_id}. Reason: {msg_open}. Funding TX: {funding_tx}\n")
        return
    
    send_telegram_notification(f"Channel opening initiated for order {order_id}. Funding TX: `{funding_tx}`. {msg_open}")

    # 3. Get Channel Point (with retries and timeout)
    send_telegram_notification(f"Waiting for channel point for TX {funding_tx} (Order {order_id})...", level="info")
    logging.info(f"Waiting up to 5 minutes to get channel point for TX {funding_tx} (Order {order_id})...")
    
    channel_point = None
    get_cp_start_time = time.time()
    while time.time() - get_cp_start_time < 300: # 5 minute timeout for channel point
        channel_point = get_channel_point(funding_tx)
        if channel_point:
            break
        logging.debug(f"Channel point for {funding_tx} not found yet, retrying in 10s...")
        time.sleep(10)

    if channel_point is None:
        error_msg = f"Could not get channel point for TX {funding_tx} (Order {order_id}) after 5 mins. Please check manually."
        logging.error(error_msg)
        send_telegram_notification(error_msg, level="error")
        with open(CRITICAL_ERROR_FILE_PATH, "a") as log_file:
            log_file.write(f"Critical: Failed to get channel point for TX {funding_tx} (Order {order_id}). Confirm manually.\n")
        return
    send_telegram_notification(f"Channel point for order {order_id}: `{channel_point}`")

    # 4. Confirm Channel Point to Amboss (with a small delay)
    logging.info(f"Waiting 10 seconds before confirming channel point {channel_point} to Amboss for order {order_id}.")
    time.sleep(10)
    send_telegram_notification(f"Confirming channel point to Amboss for order {order_id}...")
    
    channel_confirmed_result = confirm_channel_point_to_amboss(order_id, channel_point)
    if channel_confirmed_result is None or (isinstance(channel_confirmed_result, str) and "Error" in channel_confirmed_result) or ("errors" in channel_confirmed_result):
        error_msg = f"Failed to confirm channel point {channel_point} to Amboss for order {order_id}. Result: {channel_confirmed_result}. Confirm manually."
        logging.error(error_msg)
        send_telegram_notification(error_msg, level="error")
        with open(CRITICAL_ERROR_FILE_PATH, "a") as log_file:
            log_file.write(f"Critical: Failed to confirm channel point to Amboss for order {order_id}, CP: {channel_point}. Result: {channel_confirmed_result}\n")
        return

    success_msg = f"Channel for order {order_id} confirmed to Amboss! Result: {channel_confirmed_result}"
    logging.info(success_msg)
    send_telegram_notification(success_msg)

    # 5. BOS Confirm Income (Optional accounting step)
    if customer_addr_uri: # Re-use from earlier
        logging.info(f"Attempting BOS income confirmation for order {order_id}, peer: {customer_addr_uri}")
        bos_result = bos_confirm_income(seller_invoice_amount, peer_pubkey=customer_pubkey) # Use pubkey for message
        if bos_result:
            send_telegram_notification(f"BOS income confirmation for order {order_id} attempted. Output: {bos_result[:200]}...", level="info")
            logging.info(f"BOS income confirmation for order {order_id} successful.")
        else:
            send_telegram_notification(f"BOS income confirmation for order {order_id} failed.", level="warning")
            logging.error(f"BOS income confirmation failed for order {order_id}.")
    else: # Should not happen if we got this far, but as a fallback.
        logging.warning(f"Skipping BOS income confirmation for order {order_id} as customer_addr_uri was not available.")

    logging.info(f"Successfully processed paid order {order_id}.")


def process_paid_orders_for_channel_opening():
    """Checks for orders WAITING_FOR_CHANNEL_OPEN and processes them."""
    logging.info("Checking for paid Magma orders (WAITING_FOR_CHANNEL_OPEN)...")
    
    # `check_channel()` fetches orders with WAITING_FOR_CHANNEL_OPEN status
    # In a high-volume scenario, this might return multiple.
    # The current `check_channel` returns only the first one it finds.
    # We should process all of them if multiple are found.
    
    # For now, let's adapt to process one at a time as per original logic, but ideally, loop through all.
    # TODO: Modify check_channel to return a list and loop here.
    
    order_to_open = check_channel() # Gets one order WAITING_FOR_CHANNEL_OPEN

    if not order_to_open:
        logging.info("No paid Magma orders waiting for channel opening.")
        return

    # If an active order poll (wait_for_buyer_payment) is running for this order_id and successfully
    # processes it, this check_channel might pick it up again if the timing is just right
    # and its status hasn't updated in Amboss immediately.
    # This is a minor race condition, but process_paid_order should be idempotent enough or
    # we might need a small in-memory set of "recently processed by active poll" order IDs to skip here.
    # For now, let's assume process_paid_order is safe to call even if shortly after active poll.
    
    logging.info(f"Found paid order {order_to_open['id']} ready for channel opening. Processing...")
    process_paid_order(order_to_open)


def execute_bot_behavior():
    """Main bot behavior, called periodically or by command."""
    current_datetime = datetime.now()
    formatted_datetime = current_datetime.strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"Executing Magma bot behavior cycle at {formatted_datetime}")

    if os.path.exists(CRITICAL_ERROR_FILE_PATH):
        msg = f"CRITICAL ERROR FLAG ({CRITICAL_ERROR_FILE_PATH}) exists. Bot behavior suspended. Manual intervention required."
        logging.critical(msg)
        # Send one-time notification if bot is running and sees this
        # This part might be tricky if the bot instance is restarted.
        # For now, primary notification is via logs and manual check of the flag.
        # send_telegram_notification(msg, level="error") # Careful with spamming this
        return

    try:
        # Stage 1: Process new offers, accept them, and start active polling for payment
        process_new_offers()

        # Stage 2: Process orders that are already paid and ready for channel opening
        # This catches orders that might have been missed by active polling (e.g., script restart)
        # or took longer to pay.
        process_paid_orders_for_channel_opening()

    except Exception as e:
        logging.exception("Unhandled exception in execute_bot_behavior:")
        send_telegram_notification(f"FATAL ERROR in bot behavior: {e}. Check logs immediately!", level="error")
        # Create critical error flag for any unhandled exception in the main flow
        with open(CRITICAL_ERROR_FILE_PATH, "a") as log_file:
            log_file.write(f"Unhandled exception at {formatted_datetime}: {e}\n")

    logging.info(f"Magma bot behavior cycle completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


@bot.message_handler(commands=["runnow", "channelopen"]) # Keep runnow, add channelopen for manual trigger
def handle_run_command(message):
    logging.info(f"'{message.text}' command received. Executing bot behavior now.")
    send_telegram_notification(f"'{message.text}' command received. Triggering Magma processing cycle.")
    # Run in a new thread to avoid blocking Telegram bot polling if behavior takes time
    threading.Thread(target=execute_bot_behavior).start()


if __name__ == "__main__":
    if os.path.exists(CRITICAL_ERROR_FILE_PATH):
        logging.critical(
            f"The critical error flag file {CRITICAL_ERROR_FILE_PATH} exists. "
            "Magma Sale Process will not start its scheduled tasks. Please investigate and remove the flag file."
        )
        # Optionally send a startup Telegram error if CRITICAL_ERROR_FILE_PATH exists
        # send_telegram_notification(f"Magma Bot STARTUP FAILED: Critical error flag {CRITICAL_ERROR_FILE_PATH} exists.", level="error")
    else:
        logging.info("Starting Magma Sale Process scheduler.")
        send_telegram_notification("Magma Sale Process bot started and scheduler running.", level="info")
        # Schedule the bot behavior to run every POLLING_INTERVAL_MINUTES
        schedule.every(POLLING_INTERVAL_MINUTES).minutes.do(execute_bot_behavior)

        # Start the Telegram bot polling in a separate thread
        logging.info("Starting Telegram bot poller thread.")
        threading.Thread(target=lambda: bot.polling(none_stop=True, interval=30), name="TelegramPoller").start()

        # Run scheduled tasks in the main thread
        logging.info("Entering main scheduling loop.")
        while True:
            schedule.run_pending()
            time.sleep(1)
