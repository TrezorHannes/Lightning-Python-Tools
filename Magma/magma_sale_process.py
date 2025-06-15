# Magma Channel Auto-Sale Script (Lightning Network)
#
# Purpose:
# This script automates the process of selling Lightning Network channels via Amboss Magma.
# It monitors for new channel sale offers, manages approvals (with optional manual Telegram confirmation),
# generates invoices, monitors for buyer payments, and then attempts to open the Lightning channel
# using LND's `lncli`. It provides notifications via Telegram for various stages and errors.
#
# Key Features:
# - Reads configuration from `config.ini` in the parent directory.
# - Periodically checks Amboss for new channel sale offers.
# - (Optional) Prompts for manual approval/rejection of new offers via Telegram within a time window.
# - Automatically approves offers if no manual response is received within the timeout.
# - Handles invoice generation via `lncli addinvoice`.
# - Accepts orders on Amboss and monitors for buyer invoice payment.
# - Initiates channel opening via `lncli openchannel` once an order is paid.
# - Attempts to connect to the buyer's node before opening a channel.
# - Confirms channel opening (channel point) back to Amboss.
# - Provides detailed logging to a rotating log file (`logs/magma-sale-process.log`).
# - Sends Telegram notifications for important events, successes, and errors.
# - Uses a critical error flag file (`logs/magma_sale_process-critical-error.flag`) to halt
#   operations if a systemic or unrecoverable error occurs, requiring manual intervention.
# - Supports a list of banned pubkeys to automatically reject offers from.
# - (Optional) Attempts to record income via BOS `send` command after successful channel open.
#
# How to Run:
# 1. Ensure Python 3 is installed.
# 2. Install required Python packages: `pip install pyTelegramBotAPI requests schedule`
# 3. Copy `config.ini.example` to `config.ini` in the script's parent directory and fill in
#    all necessary details (API tokens, Telegram bot info, LND/BOS paths, etc.).
# 4. Make the script executable: `chmod +x magma_sale_process.py`
# 5. Run the script directly: `python /path/to/Magma/magma_sale_process.py`
#
#    The script is designed to run as a long-running daemon/service. It uses the `schedule`
#    library internally to perform periodic checks based on `POLLING_INTERVAL_MINUTES`
#    in `config.ini`. It does not use or require an external cron job or a `--cron` argument.
#    For production, it's recommended to run it under a process manager like `systemd` to
#    ensure it restarts on failure and runs continuously.
#
# Dependencies:
# - LND (`lncli` accessible in the system's PATH or via full path in `config.ini`)
# - (Optional) BOS (`bos` accessible via full path in `config.ini` for income confirmation)
# - `requests` library
# - `pyTelegramBotAPI` library
# - `schedule` library

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


# --- GraphQL Query for Offer Orders ---
# Fetches all fields typically needed when processing or listing offer orders.
OFFER_ORDER_FIELDS_QUERY_PART = """
    list {
      id
      size
      status
      account # Expected to be the buyer's pubkey
      seller_invoice_amount
      endpoints { # Contains 'destination' which is also buyer's pubkey
        destination
      }
    }
"""

GET_USER_MARKET_OFFER_ORDERS_QUERY = f"""
    query GetUserMarketOfferOrders {{
      getUser {{
        market {{
          offer_orders {{
            {OFFER_ORDER_FIELDS_QUERY_PART}
          }}
        }}
      }}
    }}
"""
# Note: get_order_details_from_amboss uses a slightly different query name "ListUserMarketOfferOrdersForDetails"
# but the structure is the same. We can align this. For now, the payload in get_order_details_from_amboss
# will be updated to use GET_USER_MARKET_OFFER_ORDERS_QUERY.


# Code
bot = telebot.TeleBot(TOKEN)
logging.info("Amboss Channel Open Bot Started")

# --- Constants for active order polling ---
ACTIVE_ORDER_POLL_INTERVAL_SECONDS = 30  # Check every 30 seconds
ACTIVE_ORDER_POLL_DURATION_MINUTES = 15   # Poll for a total of 15 minutes
USER_CONFIRMATION_TIMEOUT_SECONDS = 300  # 5 minutes for user to respond to new offer

# --- State for pending user confirmations ---
# Structure: {order_id: {"message_id": int, "timestamp": float, "details": dict}}
pending_user_confirmations = {}
processed_banned_offer_ids = set()


def send_telegram_notification(text, level="info", **kwargs):
    """Sends a message to Telegram and logs it. Allows passing kwargs to send_message."""
    log_message = f"Telegram NOTIFICATION: {text}"
    if 'reply_markup' in kwargs:
        log_message += " (with inline keyboard)"

    if level == "error":
        logging.error(log_message)
    elif level == "warning":
        logging.warning(log_message)
    else:
        logging.info(log_message)
    try:
        # Ensure Markdown is used if not specified and message contains typical Markdown chars
        if 'parse_mode' not in kwargs and any(c in text for c in ['`', '*', '_']):
            kwargs['parse_mode'] = 'Markdown'
        return bot.send_message(CHAT_ID, text=text, **kwargs) # Return the message object
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")
        return None

def _execute_amboss_graphql_request(payload: dict, operation_name: str ="AmbossGraphQL"):
    """
    Executes a GraphQL request to the Amboss API.
    Handles common request logic, headers, timeouts, and basic error handling.
    Args:
        payload (dict): The GraphQL payload (e.g., {"query": "...", "variables": {...}}).
        operation_name (str): A descriptive name for the operation for logging.
    Returns:
        dict or None: The JSON response data part if successful, else None.
    """
    url = "https://api.amboss.space/graphql"
    headers = {
        "content-type": "application/json",
        "Authorization": f"Bearer {AMBOSS_TOKEN}",
    }
    logging.debug(f"Executing {operation_name} with payload: {json.dumps(payload, indent=2 if logging.getLogger().getEffectiveLevel() == logging.DEBUG else None)}") # Pretty print payload if debug

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=20) # Standard timeout
        response.raise_for_status()  # Raises HTTPError for bad responses (4xx or 5xx)
        
        response_json = response.json()
        
        if response_json.get("errors"):
            logging.error(f"GraphQL errors during {operation_name}: {response_json.get('errors')}")
            # Do not create CRITICAL_ERROR_FILE_PATH here for query errors, let caller decide.
            # Example: if confirm_channel_point_to_amboss gets a specific Amboss error, it might create it.
            return None # Indicate GraphQL level error
            
        return response_json.get("data") # Return only the 'data' part

    except requests.exceptions.Timeout:
        logging.error(f"Timeout during {operation_name} to Amboss.")
        return None
    except requests.exceptions.RequestException as e:
        logging.error(f"API request error during {operation_name} to Amboss: {e}")
        return None
    except json.JSONDecodeError as e:
        logging.error(f"Failed to decode JSON response during {operation_name} from Amboss: {e}")
        return None
    except Exception as e:
        logging.exception(f"Unexpected error during _execute_amboss_graphql_request for {operation_name}:")
        return None

def get_order_details_from_amboss(order_id):
    """
    Fetches specific order details from Amboss by its ID.
    This involves fetching the user's list of market offer orders and filtering by ID.
    """
    logging.info(f"Fetching details for order ID: {order_id} from Amboss...")
    payload = {"query": GET_USER_MARKET_OFFER_ORDERS_QUERY}
    
    data = _execute_amboss_graphql_request(payload, f"GetOrderDetails-{order_id}")

    if not data:
        return None # Error already logged by helper

    market_data = data.get("getUser", {}).get("market", {})
    offer_orders_list = market_data.get("offer_orders", {}).get("list", [])
    
    for offer in offer_orders_list:
        if offer.get("id") == order_id:
            logging.info(f"Successfully found details for order {order_id}: {offer}")
            return offer
    
    logging.warning(f"Order ID {order_id} not found in your Amboss market orders list.")
    return None


def get_node_alias(pubkey: str) -> str:
    """Fetches the alias for a given node pubkey using the getNodeAlias query."""
    if not pubkey:
        return "N/A"
    
    logging.info(f"Fetching alias for pubkey: {pubkey}")
    payload = {
        "query": """
            query GetNodeAlias($pubkey: String!) {
              getNodeAlias(pubkey: $pubkey)
            }
        """,
        "variables": {"pubkey": pubkey}
    }
    
    data = _execute_amboss_graphql_request(payload, f"GetNodeAlias-{pubkey[:10]}")

    if not data:
        return "ErrorFetchingAlias" # Error already logged by helper

    alias = data.get("getNodeAlias") # Direct access to alias from the specific query
    if alias is not None:
        if alias == "": # Handle empty alias string from Amboss as "N/A"
            logging.info(f"Alias for {pubkey} is empty, returning 'N/A'.")
            return "N/A"
        logging.info(f"Alias for {pubkey}: {alias}")
        return alias
    else:
        # This case implies 'getNodeAlias' field was null or missing, though data itself was present.
        logging.warning(f"No alias returned in getNodeAlias data for {pubkey}.")
        return "AliasNotFound"


def execute_lncli_addinvoice(amt, memo, expiry):
    # Command to be executed
    command = f"lncli addinvoice " f"--memo '{memo}' --amt {amt} --expiry {expiry}"
    logging.info(f"Executing command: {command}")

    try:
        # Execute the command and capture the output
        result = subprocess.Popen(
            command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        output, error = result.communicate()
        output_decoded = output.decode("utf-8").strip()
        error_decoded = error.decode("utf-8").strip()

        # Log the command output and error
        logging.debug(f"lncli addinvoice stdout: {output_decoded}")
        if error_decoded:
            logging.error(f"lncli addinvoice stderr: {error_decoded}")

        # Try to parse the JSON output
        try:
            output_json = json.loads(output_decoded)
            r_hash = output_json.get("r_hash", "")
            payment_request = output_json.get("payment_request", "")
            if not r_hash or not payment_request: # Check if essential fields are missing
                err_msg = f"Missing r_hash or payment_request in lncli addinvoice output. stdout: {output_decoded}"
                if error_decoded:
                     err_msg += f" stderr: {error_decoded}"
                logging.error(err_msg)
                return f"Error: {err_msg}", None
            return r_hash, payment_request

        except json.JSONDecodeError as json_error:
            # If not a valid JSON response, handle accordingly
            log_msg = f"Error decoding JSON from lncli addinvoice: {json_error}. stdout: {output_decoded}"
            if error_decoded:
                log_msg += f" stderr: {error_decoded}"
            logging.exception(log_msg)
            return f"Error decoding JSON: {json_error}. stderr: {error_decoded if error_decoded else 'N/A'}", None

    except subprocess.CalledProcessError as e:
        # This exception is less likely with Popen but good to keep
        logging.exception(f"Error executing lncli addinvoice command: {e}")
        return f"Error executing command: {e}. stderr: {e.stderr.decode('utf-8').strip() if e.stderr else 'N/A'}", None
    except Exception as e: # Catch other potential errors
        logging.exception(f"Unexpected error in execute_lncli_addinvoice: {e}")
        return f"Unexpected error: {e}", None


def accept_order(order_id, payment_request):
    """Accepts an order on Amboss."""
    logging.info(f"Accepting order {order_id} on Amboss with payment request: {payment_request[:30]}...")
    payload = {
        "query": """
            mutation AcceptOrder($sellerAcceptOrderId: String!, $request: String!) {
              sellerAcceptOrder(id: $sellerAcceptOrderId, request: $request)
            }
        """,
        "variables": {"sellerAcceptOrderId": order_id, "request": payment_request}
    }
    # This is a mutation, so we expect the full response, not just 'data' part directly from helper
    # because the success/failure is often indicated by the presence of 'sellerAcceptOrder' field itself
    # or specific errors.
    url = "https://api.amboss.space/graphql"
    headers = {
        "content-type": "application/json",
        "Authorization": f"Bearer {AMBOSS_TOKEN}",
    }
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=60) # Increased timeout
        response.raise_for_status()
        response_json = response.json()
        logging.info(f"Amboss sellerAcceptOrder response for {order_id}: {response_json}")
        return response_json # Return the full JSON response for the caller to interpret
    except requests.exceptions.Timeout:
        logging.error(f"Timeout while accepting Amboss order {order_id}.")
        return {"errors": [{"message": "Timeout during Amboss API call"}]}
    except requests.exceptions.RequestException as e:
        logging.error(f"API request error accepting Amboss order {order_id}: {e}")
        return {"errors": [{"message": f"RequestException: {e}"}]}
    except json.JSONDecodeError as e:
        logging.error(f"Failed to decode JSON response when accepting Amboss order {order_id}: {e}")
        return {"errors": [{"message": f"JSONDecodeError: {e}"}]}
    except Exception as e:
        logging.exception(f"Unexpected error in accept_order for {order_id}:")
        return {"errors": [{"message": f"Unexpected error: {e}"}]}


def reject_order(order_id):
    """Rejects an order on Amboss."""
    logging.info(f"Rejecting order {order_id} on Amboss...")
    payload = {
        "query": """
            mutation SellerRejectOrder($sellerRejectOrderId: String!) {
              sellerRejectOrder(id: $sellerRejectOrderId)
            }
        """,
        "variables": {"sellerRejectOrderId": order_id}
    }
    # Similar to accept_order, mutations might need specific handling of the full response
    url = "https://api.amboss.space/graphql"
    headers = {
        "content-type": "application/json",
        "Authorization": f"Bearer {AMBOSS_TOKEN}",
    }
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=60) # Increased timeout
        response.raise_for_status()
        response_json = response.json()
        logging.info(f"Amboss sellerRejectOrder response for {order_id}: {response_json}")
        return response_json
    except requests.exceptions.Timeout:
        logging.error(f"Timeout while rejecting Amboss order {order_id}.")
        return {"errors": [{"message": "Timeout during Amboss API call"}]}
    except requests.exceptions.RequestException as e:
        logging.error(f"API request error rejecting Amboss order {order_id}: {e}")
        return {"errors": [{"message": f"RequestException: {e}"}]}
    except json.JSONDecodeError as e:
        logging.error(f"Failed to decode JSON response when rejecting Amboss order {order_id}: {e}")
        return {"errors": [{"message": f"JSONDecodeError: {e}"}]}
    except Exception as e:
        logging.exception(f"Unexpected error in reject_order for {order_id}:")
        return {"errors": [{"message": f"Unexpected error: {e}"}]}


def confirm_channel_point_to_amboss(order_id, transaction):
    """Confirms the channel point (funding transaction) to Amboss."""
    logging.info(f"Confirming channel point {transaction} to Amboss for order {order_id}...")
    payload = {
        "query": """
            mutation ConfirmChannelPoint($sellerAddTransactionId: String!, $transaction: String!) {
              sellerAddTransaction(id: $sellerAddTransactionId, transaction: $transaction)
            }
        """,
        "variables": {"sellerAddTransactionId": order_id, "transaction": transaction},
    }
    # This is a critical mutation. The full response is needed.
    url = "https://api.amboss.space/graphql"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {AMBOSS_TOKEN}",
    }
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=60) # Increased timeout
        response.raise_for_status()
        response_json = response.json()
        logging.info(f"Amboss sellerAddTransaction response for {order_id}: {response_json}")

        if "errors" in response_json:
            error_message = response_json["errors"][0].get("message", "Unknown Amboss API error")
            log_content = (
                f"Amboss API error in confirm_channel_point_to_amboss for order ID {order_id}, TX: {transaction}.\n"
                f"Error: {error_message}\n"
                "This is a critical failure from Amboss. Halting bot to prevent further issues."
            )
            logging.critical(log_content)
            # This is a case where Amboss itself failed a critical step.
            # We will create the critical error flag here as it might indicate a broader Amboss issue
            # or a problem with our API key / permissions for mutations.
            with open(CRITICAL_ERROR_FILE_PATH, "a") as err_file:
                err_file.write(f"{datetime.now()}: {log_content}\n")
            send_telegram_notification(f"🔥 CRITICAL: Failed to confirm channel to Amboss for order `{order_id}` due to API error: `{error_message}`. Bot halted. Manual check required.", level="error", parse_mode="Markdown")
            # Return the error structure for the caller
            return response_json 
        else:
            return response_json # Success
            
    except requests.exceptions.Timeout:
        logging.error(f"Timeout while confirming channel point to Amboss for order {order_id}.")
        # Do not create critical flag for a simple timeout on one order confirmation.
        return {"errors": [{"message": "Timeout during Amboss API call"}]}
    except requests.exceptions.RequestException as e:
        logging.error(f"API request error confirming channel point to Amboss for order {order_id}: {e}")
        return {"errors": [{"message": f"RequestException: {e}"}]}
    except json.JSONDecodeError as e:
        logging.error(f"Failed to decode JSON response confirming channel point to Amboss for order {order_id}: {e}")
        return {"errors": [{"message": f"JSONDecodeError: {e}"}]}
    except Exception as e:
        logging.exception(f"Unexpected error in confirm_channel_point_to_amboss for {order_id}:")
        return {"errors": [{"message": f"Unexpected error: {e}"}]}


def get_channel_point(hash_to_find):
    def execute_lightning_command():
        command = [f"lncli", "pendingchannels"]

        try:
            logging.info(f"Command: {command}")
            # Ensure subprocess.run captures stderr for logging
            result = subprocess.run(command, capture_output=True, text=True, check=False) # check=False to inspect result
            
            if result.stderr:
                logging.warning(f"lncli pendingchannels stderr: {result.stderr.strip()}")
            if result.returncode != 0:
                logging.error(f"lncli pendingchannels failed with return code {result.returncode}")
                return None
            
            output = result.stdout
            result_json = json.loads(output)
            return result_json

        except subprocess.CalledProcessError as e: # Less likely with check=False
            logging.exception(f"Error executing lncli pendingchannels: {e}")
            return None
        except json.JSONDecodeError as e:
            logging.exception(f"Error decoding JSON from lncli pendingchannels: {e}")
            return None
        except Exception as e:
            logging.exception(f"Unexpected error in execute_lightning_command for pendingchannels: {e}")
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
        f"{formatted_outpoints if formatted_outpoints else ''} --local_amt={input_amount} --fee_rate_ppm {fee_rate_ppm}" # Ensure formatted_outpoints is not None
    )
    logging.info(f"Executing command: {command}")
    std_err_output = "N/A" # Initialize stderr output

    try:
        # Run the command and capture both stdout and stderr
        result = subprocess.run(
            command, shell=True, check=False, capture_output=True, text=True
        )
        std_err_output = result.stderr.strip() if result.stderr else "N/A"

        # Log both stdout and stderr regardless of the result
        logging.info(f"Command Output: {result.stdout.strip() if result.stdout else 'N/A'}")
        if std_err_output != "N/A" and std_err_output: # Log if not empty
            logging.error(f"Command Error: {std_err_output}")

        if result.returncode == 0:
            try:
                output_json = json.loads(result.stdout)
                funding_txid = output_json.get("funding_txid")
                if funding_txid:
                    logging.info(f"Funding transaction ID: {funding_txid}")
                    return funding_txid, None # Return None for error message if successful
                else:
                    err_msg = "No funding transaction ID found in the command output."
                    logging.error(err_msg)
                    return None, err_msg # Return error message
            except json.JSONDecodeError as json_error:
                err_msg = f"Error decoding JSON: {json_error}. Output: {result.stdout.strip()}"
                logging.exception(err_msg)
                return None, err_msg # Return error message
        else:
            # Log a specific error message if the command fails
            err_msg = f"Command failed with return code {result.returncode}. stderr: {std_err_output}"
            logging.error(err_msg)
            return None, err_msg # Return error message

    except subprocess.CalledProcessError as e: # Should be caught by check=False, but for safety
        std_err_output = e.stderr.strip() if e.stderr else "N/A"
        err_msg = f"Error executing command: {e}. stderr: {std_err_output}"
        logging.exception(err_msg)
        return None, err_msg # Return error message
    except Exception as e: # Catch any other unexpected error
        err_msg = f"Unexpected error executing LND command: {e}. Last known stderr: {std_err_output}"
        logging.exception(err_msg)
        return None, err_msg


def get_fast_fee():
    response = requests.get(MEMPOOL_FEES_API_URL)
    data = response.json()
    if data:
        fast_fee = data["fastestFee"]
        return fast_fee
    else:
        return None


def get_address_by_pubkey(peer_pubkey):
    """Fetches the node address (IP/Tor) for a given peer pubkey from Amboss."""
    logging.info(f"Fetching address for pubkey: {peer_pubkey} from Amboss...")
    payload = {
        "query": """
            query GetNodeAddress($pubkey: String!) {
              getNode(pubkey: $pubkey) {
                graph_info {
                  node {
                    addresses {
                      addr
                    }
                  }
                }
              }
            }
        """,
        "variables": {"pubkey": peer_pubkey}
    }

    data = _execute_amboss_graphql_request(payload, f"GetNodeAddress-{peer_pubkey[:10]}")

    if not data:
        logging.error(f"Failed to get address for {peer_pubkey} from Amboss.") # Error logged by helper
        return None

    addresses = (
        data.get("getNode", {})
        .get("graph_info", {})
        .get("node", {})
        .get("addresses", [])
    )
    first_address = addresses[0]["addr"] if addresses else None

    if first_address:
        logging.info(f"Found address for {peer_pubkey}: {first_address}")
        return f"{peer_pubkey}@{first_address}"
    else:
        logging.warning(f"No address found for {peer_pubkey} on Amboss.")
        return None


def connect_to_node(node_key_address, max_retries=None):
    if max_retries is None:
        max_retries = MAX_CONNECT_RETRIES
    retries = 0
    last_stderr = "N/A"
    while retries < max_retries:
        command = f"lncli connect {node_key_address} --timeout 120s"
        logging.info(f"Connecting to node (attempt {retries + 1}/{max_retries}): {command}")
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, check=False) # check=False to inspect result
            last_stderr = result.stderr.strip() if result.stderr else "N/A"

            if result.returncode == 0:
                logging.info(f"Successfully connected to node {node_key_address}")
                return 0, None  # Return 0 for success, None for error message
            elif "already connected to peer" in last_stderr.lower(): # Check lowercased stderr
                logging.info(f"Peer {node_key_address} is already connected.")
                return 0, None  # Return 0 for success
            else:
                logging.error(
                    f"Error connecting to node {node_key_address} (attempt {retries + 1}): {last_stderr}"
                )
        except subprocess.CalledProcessError as e: # Should not happen with check=False
            last_stderr = e.stderr.strip() if e.stderr else "N/A"
            logging.error(f"CalledProcessError executing lncli connect (attempt {retries + 1}): {e}. stderr: {last_stderr}")
        except Exception as e:
            logging.error(f"Unexpected error executing lncli connect (attempt {retries + 1}): {e}")
            last_stderr = f"Unexpected Exception: {str(e)}"
        
        retries += 1
        if retries < max_retries:
            logging.info(f"Waiting {CONNECT_RETRY_DELAY_SECONDS}s before retrying connection to {node_key_address}")
            time.sleep(CONNECT_RETRY_DELAY_SECONDS)

    # If we reach this point, all retries have failed
    error_message = f"Failed to connect to node {node_key_address} after {max_retries} retries. Last error: {last_stderr}"
    logging.error(error_message)
    return 1, error_message  # Return 1 for failure, and the detailed error message


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
    
    # Ensure utxos_data is a list and contains dictionaries with 'amount_sat'
    if not isinstance(utxos_data, list) or not all(isinstance(utxo, dict) and 'amount_sat' in utxo for utxo in utxos_data):
        logging.error(f"Invalid UTXO data format received: {utxos_data}")
        send_telegram_notification("🔥 Error: Invalid UTXO data format. Cannot calculate fees.", level="error")
        return -1, 0, None

    total_available_sats = sum(utxo.get("amount_sat", 0) for utxo in utxos_data) # Use .get for safety
    utxos_needed = 0
    fee_cost = 0
    # amount_with_fees = channel_size # This was incorrect, fee_cost needs to be added iteratively
    selected_utxos_total_amount = 0 # Sum of selected UTXOs
    related_outpoints = []

    if total_available_sats < channel_size: # Initial check without considering fees yet
        logging.error(
            f"There are not enough UTXOs ({total_available_sats} sats) to cover the desired channel size {channel_size} SATS, even before fees."
        )
        return -1, 0, None

    # Iterate through sorted UTXOs (largest first, already sorted by get_lncli_utxos)
    for utxo in utxos_data:
        utxos_needed += 1
        selected_utxos_total_amount += utxo.get("amount_sat", 0)
        related_outpoints.append(utxo["outpoint"])
        
        transaction_size = calculate_transaction_size(utxos_needed)
        current_fee_cost = transaction_size * fee_per_vbyte
        
        # Check if the sum of selected UTXOs can cover the channel size plus current fees
        if selected_utxos_total_amount >= (channel_size + current_fee_cost):
            fee_cost = current_fee_cost # Final fee_cost
            break 
        # If not enough, continue to add more UTXOs
    else: 
        # This else block executes if the loop completed without breaking
        # (i.e., even with all UTXOs, couldn't cover channel size + fees)
        logging.error(
            f"Not enough UTXOs to cover channel size {channel_size} + fees {fee_cost}. "
            f"Selected UTXOs total: {selected_utxos_total_amount}. Needed: {channel_size + fee_cost}"
        )
        return -1, 0, None # Not enough UTXOs

    return utxos_needed, fee_cost, related_outpoints


def get_orders_awaiting_channel_open(): # Renamed from check_channel
    logging.info("Checking for Magma orders awaiting channel open (WAITING_FOR_CHANNEL_OPEN)...")
    payload = {"query": GET_USER_MARKET_OFFER_ORDERS_QUERY}
    
    data = _execute_amboss_graphql_request(payload, "GetOrdersAwaitingChannelOpen")

    if not data:
        return None # Error already logged by helper

    market = data.get("getUser", {}).get("market", {})
    offer_orders = market.get("offer_orders", {}).get("list", [])
    
    orders_to_open = [
        offer for offer in offer_orders if offer.get("status") == "WAITING_FOR_CHANNEL_OPEN"
    ]

    if not orders_to_open:
        logging.info("No orders found with status 'WAITING_FOR_CHANNEL_OPEN'.")
        return None 
    
    if len(orders_to_open) > 1:
        logging.warning(f"Found {len(orders_to_open)} orders WAITING_FOR_CHANNEL_OPEN. Processing one: {orders_to_open[0]['id']}")
    
    found_offer = orders_to_open[0]
    logging.info(f"Found order WAITING_FOR_CHANNEL_OPEN: {found_offer['id']}")
    return found_offer


def get_offers_awaiting_seller_approval(): # Renamed from check_offers
    global processed_banned_offer_ids
    logging.info("Checking for Magma offers awaiting seller approval (WAITING_FOR_SELLER_APPROVAL)...")
    payload = {"query": GET_USER_MARKET_OFFER_ORDERS_QUERY}

    data = _execute_amboss_graphql_request(payload, "GetOffersAwaitingSellerApproval")

    if not data:
        return None

    market_data = data.get("getUser", {}).get("market", {})
    offer_orders_list = market_data.get("offer_orders", {}).get("list", [])

    for offer in offer_orders_list:
        offer_id = offer.get("id")
        current_status = offer.get("status")
        destination_pubkey = offer.get('account') or offer.get("endpoints", {}).get("destination")

        logging.debug(f"Offer ID: {offer_id}, Status: {current_status}, Buyer Pubkey: {destination_pubkey}")

        # Primary filter: Only consider offers genuinely awaiting seller approval
        if current_status != "WAITING_FOR_SELLER_APPROVAL":
            logging.debug(f"Offer {offer_id} has status '{current_status}', not 'WAITING_FOR_SELLER_APPROVAL'. Skipping.")
            # If it was a previously auto-rejected banned offer, we might want to ensure it's removed from our temporary set
            # if Amboss now confirms its non-pending status. This prevents the set from growing indefinitely if Amboss updates.
            if offer_id in processed_banned_offer_ids and current_status in ["SELLER_REJECTED", "CANCELLED", "EXPIRED", "ERROR", "COMPLETED"]:
                logging.info(f"Offer {offer_id} (previously auto-rejected) now has terminal status '{current_status}'. Removing from processed_banned_offer_ids.")
                processed_banned_offer_ids.discard(offer_id)
            continue

        # At this point, offer.status is "WAITING_FOR_SELLER_APPROVAL"

        if destination_pubkey in BANNED_PUBKEYS:
            if offer_id not in processed_banned_offer_ids:
                logging.info(
                    f"Offer {offer_id} (status: {current_status}) from banned pubkey {destination_pubkey}. Attempting auto-rejection."
                )
                reject_response = reject_order(offer_id)

                if reject_response and not reject_response.get("errors") and reject_response.get("data", {}).get("sellerRejectOrder"):
                    send_telegram_notification(
                        f"🗑️ Auto-rejected offer `{offer_id}` (was WAITING_FOR_SELLER_APPROVAL) from banned pubkey: `{destination_pubkey}`.",
                        level="warning", parse_mode="Markdown"
                    )
                    processed_banned_offer_ids.add(offer_id)
                    logging.info(f"Added {offer_id} to processed_banned_offer_ids after successful auto-rejection.")
                elif reject_response and reject_response.get("errors"):
                    logging.error(
                        f"Failed to auto-reject offer {offer_id} from banned pubkey {destination_pubkey}. "
                        f"Amboss errors: {reject_response.get('errors')}"
                    )
                else:
                    logging.warning(
                        f"Auto-rejection of offer {offer_id} from banned pubkey {destination_pubkey} "
                        f"did not confirm success or failed. Response: {reject_response}. Will retry processing next cycle."
                    )
            else:
                logging.debug(f"Offer {offer_id} from banned pubkey {destination_pubkey} was already processed for auto-rejection in this session (still WAITING_FOR_SELLER_APPROVAL). Skipping.")
            continue # This offer (from banned pubkey, now handled or previously handled) should not be returned for manual approval

        # If we reach here, the offer is "WAITING_FOR_SELLER_APPROVAL" and NOT from a banned pubkey.
        # This is a candidate for manual user approval.
        logging.info(f"Found valid, unbanned offer awaiting approval: {offer_id}")
        return offer # Return the first such offer

    logging.info("No unbanned offers found currently in 'WAITING_FOR_SELLER_APPROVAL' status.")
    return None


def open_channel(pubkey, size, invoice):
    # get fastest fee
    logging.info("Getting fastest fee...")
    fee_rate = get_fast_fee()
    formatted_outpoints = None # Initialize to ensure it's defined
    msg_open_or_error = "Fee rate could not be determined." # Default error message

    if fee_rate:
        logging.info(f"Fastest Fee:{fee_rate} sat/vB")
        logging.info("Getting UTXOs, Fee Cost and Outpoints to open the channel")
        utxos_needed, fee_cost, related_outpoints = calculate_utxos_required_and_fees(
            size, fee_rate
        )
        if utxos_needed == -1:
            msg_open_or_error = (
                f"Not enough confirmed balance for a {size} SATS channel."
            )
            logging.info(msg_open_or_error)
            return None, msg_open_or_error # Return None for TX, and error message
        
        if (fee_cost) >= float(invoice) * MAX_FEE_PERCENTAGE_OF_INVOICE:
            msg_open_or_error = (
                f"Fee ({fee_cost} sats) is >= {MAX_FEE_PERCENTAGE_OF_INVOICE*100}% of invoice ({float(invoice)} sats)."
            )
            logging.info(msg_open_or_error)
            return None, msg_open_or_error # Return None for TX, and error message
        
        if related_outpoints:
            formatted_outpoints = " ".join(
                [f"--utxo {outpoint}" for outpoint in related_outpoints]
            )
        else:
            # This case means we might be using a single, large enough UTXO or LND handles selection.
            # No specific error, but formatted_outpoints remains None if not constructed.
            # execute_lnd_command handles formatted_outpoints being None by passing an empty string.
            logging.info("No specific outpoints passed to lncli; LND will select UTXOs or using available balance.")


        logging.info(f"Preparing to open channel to: {pubkey} with outpoints: {formatted_outpoints}")
        # execute_lnd_command now returns (funding_txid_or_None, error_message_or_None)
        funding_tx, lncli_error_msg = execute_lnd_command(
            pubkey, fee_rate, formatted_outpoints, size, CHANNEL_FEE_RATE_PPM
        )

        if funding_tx is None:
            # lncli_error_msg already contains the detailed error from execute_lnd_command
            msg_open_or_error = f"lncli openchannel command failed. Details: {lncli_error_msg}"
            logging.info(msg_open_or_error) # Log the detailed error
            return None, msg_open_or_error # Propagate None for TX and the detailed error
        
        msg_open_or_error = f"Channel opened with funding transaction: {funding_tx}"
        logging.info(msg_open_or_error)
        return funding_tx, msg_open_or_error # Return TX and success message

    else: # fee_rate was None
        logging.error(msg_open_or_error) # Log the "Fee rate could not be determined" error
        return None, msg_open_or_error # Return None for TX and this specific error


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
        # bot.send_message(CHAT_ID, text=f"BOS Command Output: {result.stdout}") # Removed
        return result.stdout
    except subprocess.CalledProcessError as e:
        logging.error(f"Error executing BOS command: {e}")
        # bot.send_message(CHAT_ID, text=f"Error executing BOS command: {e}") # Removed
        return None


def _complete_offer_approval_process(order_id, order_details):
    """Generates invoice, accepts on Amboss, and starts payment polling."""
    seller_invoice_amount = order_details['seller_invoice_amount']
    buyer_alias = order_details.get("buyer_alias", "N/A") 
    
    send_telegram_notification(f"✅ Order `{order_id}` approved by you ({buyer_alias}).\nGenerating invoice for {seller_invoice_amount} sats...", parse_mode="Markdown")
    invoice_hash_or_error, invoice_request = execute_lncli_addinvoice( # Modified to return error message
        seller_invoice_amount,
        f"Magma-Channel-Sale-Order-ID:{order_id}",
        str(INVOICE_EXPIRY_SECONDS),
    )

    if invoice_request is None or "Error" in invoice_hash_or_error: # Check if invoice_request is None or error in first part
        # invoice_hash_or_error now contains the error message from execute_lncli_addinvoice
        error_msg = f"🔥 Failed to generate invoice for approved order `{order_id}`.\n`lncli` error: `{invoice_hash_or_error}`"
        logging.error(error_msg)
        send_telegram_notification(error_msg, level="error", parse_mode="Markdown")
        return 

    invoice_hash = invoice_hash_or_error # If successful, this is the actual hash
    logging.debug(f"Invoice for approved order {order_id} (hash: {invoice_hash}): {invoice_request}")
    send_telegram_notification(f"🧾 Invoice for `{order_id}`:\n`{invoice_request}`", parse_mode="Markdown")

    send_telegram_notification(f"📡 Accepting Magma order `{order_id}` on Amboss...", parse_mode="Markdown")
    accept_result = accept_order(order_id, invoice_request)
    logging.info(f"Order {order_id} Amboss acceptance result: {accept_result}")

    if "data" in accept_result and "sellerAcceptOrder" in accept_result["data"] and accept_result["data"]["sellerAcceptOrder"]:
        success_message = f"⏳ Order `{order_id}` accepted on Amboss. Invoice sent. Monitoring for buyer payment."
        send_telegram_notification(success_message, parse_mode="Markdown")
        logging.info(success_message)
        wait_for_buyer_payment(order_id) # Start active polling
    else:
        errors_list = accept_result.get('errors', [])
        error_message_detail = "Unknown error"
        is_timeout_error = False

        if errors_list and isinstance(errors_list, list) and len(errors_list) > 0:
            first_error = errors_list[0]
            if isinstance(first_error, dict):
                error_message_detail = first_error.get('message', 'Unknown error structure in error list')
                if "Timeout during Amboss API call" in error_message_detail:
                    is_timeout_error = True
        
        failure_message = f"🔥 Failed to accept approved order `{order_id}` on Amboss. Details: `{error_message_detail}`"
        logging.error(failure_message)
        send_telegram_notification(failure_message, level="error", parse_mode="Markdown")

        if not is_timeout_error and errors_list: # Create critical flag only if it's not a timeout but some other Amboss error
            logging.warning(f"Creating critical error flag for order {order_id} due to non-timeout Amboss error during accept: {errors_list}")
            with open(CRITICAL_ERROR_FILE_PATH, "a") as log_file:
                log_file.write(f"{datetime.now()}: Failed to accept Amboss order {order_id} after approval. Response: {errors_list}\n")
        elif is_timeout_error:
            logging.info(f"Order {order_id} acceptance timed out. Not creating critical error flag. Amboss may need to be checked manually for this order or it might be re-processed if applicable.")


@bot.callback_query_handler(func=lambda call: call.data.startswith('decide_order:'))
def handle_order_decision_callback(call):
    """Handles callbacks from Approve/Reject offer buttons."""
    try:
        _, action, order_id = call.data.split(':')
        logging.info(f"Received user decision: {action} for order {order_id} via Telegram callback.")

        confirmation_details_entry = pending_user_confirmations.pop(order_id, None)

        if not confirmation_details_entry:
            logging.warning(f"Received callback for order {order_id}, but it was not in pending_user_confirmations (likely timed out or already processed).")
            bot.answer_callback_query(call.id, text=f"Offer {order_id} already processed or timed out.")
            try:
                bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)
            except Exception as e:
                logging.debug(f"Could not edit message for already processed callback {order_id}: {e}")
            return

        order_original_details = confirmation_details_entry["details"]
        buyer_alias = order_original_details.get("buyer_alias", "N/A")
        buyer_pubkey = order_original_details.get('account') or order_original_details.get("endpoints", {}).get("destination", "Unknown")
        amount = order_original_details['seller_invoice_amount']

        decision_text_verb = "Approved" if action == "approve" else "Rejected"
        decision_emoji = "✅" if action == "approve" else "❌"
        
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=confirmation_details_entry["message_id"],
                text=(
                    f"{decision_emoji} Offer {decision_text_verb} by You:\n"
                    f"ID: `{order_id}`\n"
                    f"💰 Amount: {amount} sats\n"
                    f"👤 Buyer: `{buyer_alias}` ({buyer_pubkey[:10]}...)"
                ),
                reply_markup=None,
                parse_mode="Markdown"
            )
        except Exception as e:
            logging.error(f"Error editing Telegram message for order {order_id} after decision: {e}")

        if action == "approve":
            send_telegram_notification(f"▶️ Proceeding with approved order `{order_id}` ({buyer_alias}).", parse_mode="Markdown")
            if order_original_details.get("status") == "WAITING_FOR_SELLER_APPROVAL":
                _complete_offer_approval_process(order_id, order_original_details)
            else:
                msg = f"⚠️ Order `{order_id}` status changed to `{order_original_details.get('status')}` before user approval ({action}) could be fully processed. No action taken."
                logging.warning(msg)
                send_telegram_notification(msg, level="warning", parse_mode="Markdown")

        elif action == "reject":
            send_telegram_notification(f"🗑️ Rejecting order `{order_id}` ({buyer_alias}) on Amboss.", parse_mode="Markdown")
            reject_order(order_id)

        bot.answer_callback_query(call.id, text=f"Order {order_id} {decision_text_verb}.")

    except Exception as e:
        logging.exception(f"Error in order_decision_callback for call data {call.data}:")
        bot.answer_callback_query(call.id, text="Error processing your decision.")
        send_telegram_notification("🔥 Error processing user decision from Telegram button. Check logs.", level="error", parse_mode="Markdown")


def _handle_timeout_for_offer(order_id, confirmation_info):
    """Handles the logic when an offer confirmation times out."""
    logging.info(f"Order {order_id} timed out waiting for user confirmation. Defaulting to approve.")
    
    order_details = confirmation_info['details']
    buyer_alias = order_details.get("buyer_alias", "N/A")
    buyer_pubkey = order_details.get('account') or order_details.get("endpoints", {}).get("destination", "Unknown")
    amount = order_details['seller_invoice_amount']

    send_telegram_notification(
        f"⏳ Offer Timeout & Auto-Approved:\n"
        f"ID: `{order_id}`\n"
        f"💰 Amount: {amount} sats\n"
        f"👤 Buyer: `{buyer_alias}` ({buyer_pubkey[:10]}...)\n"
        f"No response in 5 min.",
        level="warning",
        parse_mode="Markdown"
    )
    try:
        bot.edit_message_text(
            chat_id=CHAT_ID,
            message_id=confirmation_info["message_id"],
            text=(
                f"✅ Auto-Approved (Timeout):\n"
                f"ID: `{order_id}`\n"
                f"💰 Amount: {amount} sats\n"
                f"👤 Buyer: `{buyer_alias}` ({buyer_pubkey[:10]}...)"
            ),
            reply_markup=None,
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Error editing Telegram message for timed-out order {order_id}: {e}")
    
    order_details_fresh = get_order_details_from_amboss(order_id) 
    if order_details_fresh:
        # Add buyer_alias to fresh details if needed for _complete_offer_approval_process
        order_details_fresh['buyer_alias'] = buyer_alias # Carry over known alias
        if order_details_fresh.get("status") == "WAITING_FOR_SELLER_APPROVAL":
             _complete_offer_approval_process(order_id, order_details_fresh)
        else:
            msg = f"⚠️ Order `{order_id}` (timed out) status changed to `{order_details_fresh.get('status')}` before auto-approval. No action taken."
            logging.warning(msg)
            send_telegram_notification(msg, level="warning", parse_mode="Markdown")
    else:
        msg = f"🔥 Could not fetch details for timed-out order `{order_id}` for auto-approval. Manual check required."
        logging.error(msg)
        send_telegram_notification(msg, level="error", parse_mode="Markdown")


def process_new_offers():
    """
    Checks for new offers (WAITING_FOR_SELLER_APPROVAL).
    If a new offer is found, it asks the user for confirmation via Telegram, including the buyer's alias.
    It also handles timeouts for offers previously presented to the user.
    """
    global pending_user_confirmations

    # 1. Handle Timeouts for pending user actions
    current_time = time.time()
    timed_out_orders_ids = []
    for order_id, info in list(pending_user_confirmations.items()):
        if current_time - info["timestamp"] > USER_CONFIRMATION_TIMEOUT_SECONDS:
            timed_out_orders_ids.append(order_id)
            
    for order_id in timed_out_orders_ids:
        confirmation_info = pending_user_confirmations.pop(order_id, None)
        if confirmation_info:
            _handle_timeout_for_offer(order_id, confirmation_info)

    # 2. Fetch new offers from Amboss
    logging.info("Checking for new Magma offers (WAITING_FOR_SELLER_APPROVAL)...")
    new_offer_from_amboss = get_offers_awaiting_seller_approval() # Use renamed function

    if not new_offer_from_amboss:
        logging.info("No new Magma offers found requiring seller approval at this time.")
        return

    order_id = new_offer_from_amboss['id']
    
    # If it's already pending user confirmation, we've already asked. Let timeout or callback handle it.
    if order_id in pending_user_confirmations:
        logging.info(f"Offer {order_id} is already awaiting user confirmation. Skipping new prompt.")
        return

    # This is a genuinely new offer we haven't prompted for yet.
    seller_invoice_amount = new_offer_from_amboss['seller_invoice_amount']
    current_status = new_offer_from_amboss['status']
    # 'account' is often the buyer's pubkey in Amboss market data,
    # 'destination' under endpoints is also usually the buyer's pubkey. Prefer 'account' if available.
    destination_pubkey = new_offer_from_amboss.get('account') or \
                         new_offer_from_amboss.get("endpoints", {}).get("destination")


    # Pre-check for banned pubkey before even asking user
    if destination_pubkey in BANNED_PUBKEYS:
        logging.info(f"New offer {order_id} from banned pubkey {destination_pubkey}. Auto-rejecting without user prompt.")
        send_telegram_notification(f"Auto-rejecting new offer {order_id} from banned pubkey: {destination_pubkey}.", level="warning")
        reject_order(order_id)
        return

    # Fetch buyer's alias
    buyer_alias = "N/A"
    if destination_pubkey:
        buyer_alias = get_node_alias(destination_pubkey)
    else:
        logging.warning(f"Could not determine destination pubkey for order {order_id} to fetch alias.")


    # Ask user for confirmation
    markup = types.InlineKeyboardMarkup()
    approve_button = types.InlineKeyboardButton("✅ Approve Offer", callback_data=f"decide_order:approve:{order_id}")
    reject_button = types.InlineKeyboardButton("❌ Reject Offer", callback_data=f"decide_order:reject:{order_id}")
    markup.add(approve_button, reject_button)

    prompt_message = (
        f"🔔 New Magma Offer:\n"
        f"ID: `{order_id}`\n"
        f"💰 Amount: {seller_invoice_amount} sats\n"
        f"👤 Buyer: `{buyer_alias}` ({destination_pubkey[:10]}...)\n"
        f"⏳ Please Approve/Reject within 5 min."
    )
    sent_message = send_telegram_notification(prompt_message, reply_markup=markup, parse_mode="Markdown")

    if sent_message:
        # Store comprehensive details in pending_user_confirmations for rich messages on timeout/callback
        pending_confirmation_data = new_offer_from_amboss.copy()
        pending_confirmation_data['buyer_alias'] = buyer_alias # Add the fetched alias

        pending_user_confirmations[order_id] = {
            "message_id": sent_message.message_id,
            "timestamp": time.time(),
            "details": pending_confirmation_data 
        }
        logging.info(f"Offer {order_id} (Buyer: {buyer_alias}) presented to user for confirmation. Awaiting response or timeout.")
    else:
        logging.error(f"Failed to send confirmation request to Telegram for order {order_id}.")


def wait_for_buyer_payment(order_id):
    """Actively polls a specific order to see if it becomes WAITING_FOR_CHANNEL_OPEN."""
    logging.info(f"Starting active poll for payment of order {order_id} for {ACTIVE_ORDER_POLL_DURATION_MINUTES} minutes.")
    # Notification for starting active poll is now part of _complete_offer_approval_process

    start_time = time.time()
    max_duration_seconds = ACTIVE_ORDER_POLL_DURATION_MINUTES * 60

    while time.time() - start_time < max_duration_seconds:
        order_details = get_order_details_from_amboss(order_id) 
        if order_details:
            current_status = order_details.get("status")
            logging.info(f"Order {order_id} current status: {current_status}")
            if current_status == "WAITING_FOR_CHANNEL_OPEN":
                send_telegram_notification(f"💰 Buyer paid for order `{order_id}`! Status: {current_status}.\nProceeding to open channel.", parse_mode="Markdown")
                logging.info(f"Order {order_id} is now WAITING_FOR_CHANNEL_OPEN. Triggering channel open process.")
                process_paid_order(order_details)
                return 
            elif current_status in ["CANCELLED", "EXPIRED", "SELLER_REJECTED", "ERROR", "COMPLETED"]: 
                send_telegram_notification(f"ℹ️ Order `{order_id}` is now {current_status}. Stopped active payment monitoring.", parse_mode="Markdown")
                logging.info(f"Order {order_id} reached terminal state {current_status}. Stopping active poll.")
                return
        else:
            logging.warning(f"Could not fetch details for order {order_id} during active poll.")

        time.sleep(ACTIVE_ORDER_POLL_INTERVAL_SECONDS)

    send_telegram_notification(f"⏳ Order `{order_id}`: Buyer did not pay within {ACTIVE_ORDER_POLL_DURATION_MINUTES} min. Stopped active monitoring.", parse_mode="Markdown")
    logging.info(f"Active polling for order {order_id} finished. Buyer did not pay or status not WAITING_FOR_CHANNEL_OPEN in time.")


def process_paid_order(order_details):
    """Processes a single order that is WAITING_FOR_CHANNEL_OPEN."""
    order_id = order_details['id']
    customer_pubkey = order_details['account'] 
    channel_size = order_details['size']
    seller_invoice_amount = order_details['seller_invoice_amount']
    
    buyer_alias = get_node_alias(customer_pubkey)

    send_telegram_notification(
        f"⚡️ Processing paid order `{order_id}`:\n"
        f"👤 Buyer: `{buyer_alias}` ({customer_pubkey[:10]}...)\n"
        f"📦 Size: {channel_size} sats\n"
        f"🧾 Invoice: {seller_invoice_amount} sats",
        parse_mode="Markdown"
    )

    # 1. Connect to Peer
    send_telegram_notification(f"🔗 Attempting to connect to peer `{buyer_alias}` ({customer_pubkey[:10]}...) for order `{order_id}`.", parse_mode="Markdown")
    customer_addr_uri = get_address_by_pubkey(customer_pubkey)

    if not customer_addr_uri:
        error_msg = f"🔥 Could not get address for peer `{buyer_alias}` ({customer_pubkey[:10]}...) (Order `{order_id}`). Cannot open channel."
        logging.error(error_msg)
        send_telegram_notification(error_msg, level="error", parse_mode="Markdown")
        with open(CRITICAL_ERROR_FILE_PATH, "a") as log_file:
            log_file.write(f"Critical: Could not get address for peer {customer_pubkey} (Order {order_id})\n")
        return

    node_connection_status, conn_error_msg = connect_to_node(customer_addr_uri) # Modified to return error message
    if node_connection_status == 0:
        send_telegram_notification(f"✅ Successfully connected to `{buyer_alias}` ({customer_addr_uri}).", parse_mode="Markdown")
    else:
        # conn_error_msg contains the detailed error from connect_to_node
        tg_error_msg = (
            f"⚠️ Could not connect to `{buyer_alias}` ({customer_addr_uri}) for order `{order_id}`.\n"
            f"`lncli` error: `{conn_error_msg}`\n"
            f"Will attempt channel open anyway."
        )
        send_telegram_notification(tg_error_msg, level="warning", parse_mode="Markdown")


    # 2. Open Channel
    send_telegram_notification(f"🛠️ Attempting to open {channel_size} sats channel with `{buyer_alias}` ({customer_pubkey[:10]}...) for order `{order_id}`.", parse_mode="Markdown")
    # open_channel now returns (funding_tx_or_None, error_message_or_None)
    # The second element (msg_open_or_error) will contain detailed lncli error if funding_tx is None
    funding_tx, msg_open_or_error = open_channel(customer_pubkey, channel_size, seller_invoice_amount)


    if funding_tx is None: # Indicates an error occurred
        # msg_open_or_error now contains the detailed error from execute_lnd_command (via open_channel)
        error_msg = f"🔥 Failed to open channel for order `{order_id}`.\n`lncli` error: `{msg_open_or_error}`"
        logging.error(error_msg) 
        send_telegram_notification(error_msg, level="error", parse_mode="Markdown")
        # Do NOT create CRITICAL_ERROR_FILE_PATH here, this is an order-specific failure.
        # Log to a separate file for critical ORDER issues if desired, or just rely on main log and Telegram.
        # with open(CRITICAL_ORDER_FAILURES_LOG , "a") as log_file:
        # log_file.write(f"Critical: Failed to open channel for order {order_id}. Reason: {msg_open_or_error}.\n")
        return
    
    # If successful, msg_open_or_error is the success message
    send_telegram_notification(f"✅ Channel opening initiated for order `{order_id}`.\nFunding TX: `{funding_tx}`\nDetails: {msg_open_or_error}", parse_mode="Markdown")

    # 3. Get Channel Point (with retries and timeout)
    send_telegram_notification(f"⏳ Waiting for channel point for TX `{funding_tx}` (Order `{order_id}`)...", level="info", parse_mode="Markdown")
    logging.info(f"Waiting up to 5 minutes to get channel point for TX {funding_tx} (Order {order_id})...")
    
    channel_point = None
    get_cp_start_time = time.time()
    while time.time() - get_cp_start_time < 300: 
        channel_point = get_channel_point(funding_tx)
        if channel_point:
            break  
        logging.debug(f"Channel point for {funding_tx} not found yet, retrying in 10s...")
        time.sleep(10)

    if channel_point is None:
        error_msg = f"🔥 Could not get channel point for TX `{funding_tx}` (Order `{order_id}`) after 5 mins. Please check manually."
        logging.error(error_msg)
        send_telegram_notification(error_msg, level="error", parse_mode="Markdown")
        # Do NOT create CRITICAL_ERROR_FILE_PATH here. Manual check for this order.
        return
    send_telegram_notification(f"✅ Channel point for order `{order_id}`: `{channel_point}`", parse_mode="Markdown")

    # 4. Confirm Channel Point to Amboss (with a small delay)
    logging.info(f"Waiting 10 seconds before confirming channel point {channel_point} to Amboss for order {order_id}.")
    time.sleep(10)
    send_telegram_notification(f"📡 Confirming channel point to Amboss for order `{order_id}`...", parse_mode="Markdown")
    
    channel_confirmed_result = confirm_channel_point_to_amboss(order_id, channel_point)
    # Check for various error indications in channel_confirmed_result
    has_errors = False
    if channel_confirmed_result is None: # This means _execute_amboss_graphql_request had a fundamental issue
        has_errors = True
        # Error message would have been logged by the helper or confirm_channel_point_to_amboss for specific Amboss GQL errors
        # If confirm_channel_point_to_amboss returned None without specific errors, it means network/request level issue
        if not (isinstance(channel_confirmed_result, dict) and "errors" in channel_confirmed_result) : # if not already an error dict
             channel_confirmed_result = {"errors": [{"message": "Network/request level error during Amboss confirmation or helper returned None."}]}

    elif isinstance(channel_confirmed_result, dict) and "errors" in channel_confirmed_result:
        # This means Amboss returned a GraphQL error. confirm_channel_point_to_amboss handles CRITICAL_ERROR_FILE_PATH for these.
        has_errors = True
        
    if has_errors:
        # The CRITICAL_ERROR_FILE_PATH is created by confirm_channel_point_to_amboss if Amboss returns a GQL error.
        # For other errors (like timeout from the wrapper), we just notify and log.
        err_detail = channel_confirmed_result.get("errors", [{"message": "Unknown confirmation failure"}])[0].get("message", "details unavailable")
        error_msg = f"🔥 Failed to confirm channel point `{channel_point}` to Amboss for order `{order_id}`. Result: `{err_detail}`. Confirm manually."
        logging.error(error_msg) # Full error already logged by confirm_channel_point_to_amboss or helper
        send_telegram_notification(error_msg, level="error", parse_mode="Markdown")
        # No CRITICAL_ERROR_FILE_PATH creation here unless confirm_channel_point_to_amboss did it for a direct Amboss API error.
        return

    success_msg = f"✅ Channel for order `{order_id}` confirmed to Amboss!"
    logging.info(f"{success_msg} Result: {channel_confirmed_result}") # Log full result
    send_telegram_notification(success_msg, parse_mode="Markdown")


    # 5. BOS Confirm Income (Optional accounting step)
    if customer_addr_uri: 
        logging.info(f"Attempting BOS income confirmation for order {order_id}, peer pubkey: {customer_pubkey}")
        bos_result = bos_confirm_income(seller_invoice_amount, peer_pubkey=customer_pubkey) 
        if bos_result:
            # The full output is in the logs. Send a concise notification.
            send_telegram_notification(f"✅ BOS income confirmation for order `{order_id}` initiated. Check logs for details.", level="info", parse_mode="Markdown")
            logging.info(f"BOS income confirmation for order {order_id} processed. Full output in logs.")
        else:
            send_telegram_notification(f"⚠️ BOS income confirmation for order `{order_id}` failed. Check logs.", level="warning", parse_mode="Markdown")
            logging.error(f"BOS income confirmation failed for order {order_id}.")
    else: 
        logging.warning(f"Skipping BOS income confirmation for order {order_id} as customer_addr_uri was not available.")

    logging.info(f"Successfully processed paid order {order_id}.")


def process_paid_orders_for_channel_opening():
    """Checks for orders WAITING_FOR_CHANNEL_OPEN and processes them."""
    logging.info("Checking for paid Magma orders (WAITING_FOR_CHANNEL_OPEN)...")
    
    order_to_open = get_orders_awaiting_channel_open() # Use renamed function

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
        send_telegram_notification(f"🔥🔥 FATAL ERROR in bot behavior: {e}. Check logs immediately!", level="error", parse_mode="Markdown")
        # Create critical error flag for any unhandled exception in the main flow
        with open(CRITICAL_ERROR_FILE_PATH, "a") as log_file:
            log_file.write(f"{formatted_datetime}: {e}\n")

    logging.info(f"Magma bot behavior cycle completed at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


@bot.message_handler(commands=["processmagmaorders"])
def handle_run_command(message):
    logging.info(f"'{message.text}' command received. Executing bot behavior now.")
    send_telegram_notification(f"🚀 Manual trigger '{message.text}' received. Starting Magma processing cycle...", parse_mode="Markdown")
    threading.Thread(target=execute_bot_behavior, name=f"ManualRun-{message.text[1:]}").start()


if __name__ == "__main__":
    # Ensure logs directory exists before setting up handler
    logs_dir_for_main = os.path.join(parent_dir, "..", "logs")
    if not os.path.exists(logs_dir_for_main):
        try:
            os.makedirs(logs_dir_for_main, exist_ok=True)
        except OSError as e:
            # Cannot create log directory, a truly critical startup failure
            print(f"CRITICAL: Cannot create log directory {logs_dir_for_main}. Error: {e}. Exiting.")
            # Optionally, try to write to a flag file in a known location if possible, or just exit.
            with open(os.path.join(parent_dir, "..", "magma_bot_STARTUP_FAILED_LOGDIR.flag"), "w") as f:
                f.write(f"Could not create log directory: {logs_dir_for_main}. Error: {e}")
            exit(1) # Exit immediately

    # Initialize logging (moved handler setup after logs_dir check)
    log_file_path_for_main = os.path.join(logs_dir_for_main, "magma-sale-process.log")
    main_handler = RotatingFileHandler(
        log_file_path_for_main, maxBytes=10 * 1024 * 1024, backupCount=5  # 10 MB
    )
    logging.basicConfig(
        level=logging.INFO, # Default to INFO, can be overridden by config later if needed
        format="%(asctime)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s", # Added funcName and lineno
        handlers=[main_handler],
    )
    # Adjust logging levels for third-party libraries
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("telebot").setLevel(logging.WARNING)
    
    # Load config after basic logging is up, so config loading errors can be logged.
    config = configparser.ConfigParser()
    try:
        if not config.read(config_file_path):
            logging.critical(f"CRITICAL: Configuration file {config_file_path} not found or empty. Bot cannot start.")
            with open(CRITICAL_ERROR_FILE_PATH, "a") as f:
                f.write(f"{datetime.now()}: Config file {config_file_path} not found or empty.\n")
            # Attempt to send Telegram if basic token/chat_id might be hardcoded or environment vars
            if TOKEN and CHAT_ID: # TOKEN and CHAT_ID are global, might be defined before full config load
                 send_telegram_notification(f"☠️ Magma Bot STARTUP FAILED: Config file `{config_file_path}` not found/empty.", level="error")
            exit(1)
        # Re-apply config based logging level if specified
        log_level_str = config.get("system", "log_level", fallback="INFO").upper()
        numeric_level = getattr(logging, log_level_str, logging.INFO)
        logging.getLogger().setLevel(numeric_level) # Set root logger level
        main_handler.setLevel(numeric_level) # Ensure handler also respects this level
        logging.info(f"Log level set to {log_level_str} from config.")

    except configparser.Error as e:
        logging.critical(f"CRITICAL: Error parsing configuration file {config_file_path}: {e}. Bot cannot start.")
        with open(CRITICAL_ERROR_FILE_PATH, "a") as f:
            f.write(f"{datetime.now()}: Error parsing config file {config_file_path}: {e}\n")
        if TOKEN and CHAT_ID:
             send_telegram_notification(f"☠️ Magma Bot STARTUP FAILED: Error parsing config `{config_file_path}`: {e}", level="error")
        exit(1)


    if os.path.exists(CRITICAL_ERROR_FILE_PATH):
        logging.critical(
            f"The critical error flag file {CRITICAL_ERROR_FILE_PATH} exists. "
            "Magma Sale Process will not start its scheduled tasks. Please investigate and remove the flag file."
        )
        # Check if TOKEN and CHAT_ID are available before trying to send a message
        if TOKEN and CHAT_ID:
            try:
                # Need a temporary bot instance or ensure global 'bot' is initialized enough
                # For simplicity, let's assume if we are here, basic config is loaded.
                 send_telegram_notification(f"☠️ Magma Bot STARTUP FAILED: Critical error flag found at `{CRITICAL_ERROR_FILE_PATH}`. Manual intervention required.", level="error")
            except Exception as e:
                logging.error(f"Could not send startup critical error Telegram message: {e}")
    else:
        logging.info("Starting Magma Sale Process scheduler.")
        send_telegram_notification("🤖 Magma Sale Process Bot Started\nScheduler running. Listening for orders.", level="info", parse_mode="Markdown")
        schedule.every(POLLING_INTERVAL_MINUTES).minutes.do(execute_bot_behavior)
        # Start the Telegram bot polling in a separate thread
        logging.info("Starting Telegram bot poller thread.")
        threading.Thread(target=lambda: bot.polling(none_stop=True, interval=30), name="TelegramPoller").start()

        # Run scheduled tasks in the main thread
        logging.info("Entering main scheduling loop.")
        while True:
            schedule.run_pending()
            time.sleep(1)
