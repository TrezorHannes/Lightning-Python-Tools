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
# - Uses a critical error flag file (`logs/magma_sale_process-critical-error.flag` located in the
#   `logs` directory relative to the script's parent directory) to halt
#   operations if a systemic or unrecoverable error occurs. This flag must be manually
#   deleted after investigating and resolving the underlying issue to allow the bot to resume.
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
POLLING_INTERVAL_MINUTES = config.getint("magma", "polling_interval_minutes", fallback=10)

BANNED_PUBKEYS = config.get("pubkey", "banned_magma_pubkeys", fallback="").split(",")

TOKEN = config["telegram"]["magma_bot_token"]
AMBOSS_TOKEN = config["credentials"]["amboss_authorization"]
CHAT_ID = config["telegram"]["telegram_user_id"]

FULL_PATH_BOS = config["system"]["full_path_bos"]
LNCLI_PATH = config.get("paths", "lncli_path", fallback="lncli")


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

def get_node_extended_details(pubkey: str) -> dict:
    """Fetches extended details for a given node pubkey from Amboss."""
    if not pubkey:
        return {} # Return empty dict if no pubkey

    logging.info(f"Fetching extended details for pubkey: {pubkey}")
    payload = {
        "query": """
            query GetNodeExtendedInfo($pubkey: String!) {
              getNode(pubkey: $pubkey) {
                # alias # Alias is already fetched by get_node_alias, or can be added if we consolidate
                amboss {
                  is_claimed
                }
                graph_info {
                  channels {
                    num_channels
                    total_capacity
                  }
                  node {
                    addresses {
                      addr
                      ip_info {
                        country
                        ip_address
                      }
                      network
                    }
                  }
                }
                socials {
                  info {
                    email
                    nostr_username
                    telegram
                    twitter
                    message
                  }
                  lightning_labs {
                    terminal_web {
                      position
                    }
                  }
                  ln_plus {
                    rankings {
                      rank_name
                    }
                  }
                }
              }
            }
        """,
        "variables": {"pubkey": pubkey}
    }

    data = _execute_amboss_graphql_request(payload, f"GetNodeExtendedInfo-{pubkey[:10]}")

    if not data or not data.get("getNode"):
        logging.warning(f"No extended details returned for pubkey {pubkey} from Amboss.")
        return {} # Return empty dict on error or no data

    return data.get("getNode")


def execute_lncli_addinvoice(amt, memo, expiry):
    # Command to be executed as list
    command = [LNCLI_PATH, "addinvoice", "--memo", str(memo), "--amt", str(amt), "--expiry", str(expiry)]
    logging.info(f"Executing command: {' '.join(command)}")

    try:
        # Execute the command as list with shell=False
        result = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
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
        command = [LNCLI_PATH, "pendingchannels"]

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
    # Format the command as list
    command = [
        LNCLI_PATH, "openchannel",
        "--node_key", node_pub_key,
        "--sat_per_vbyte", str(fee_per_vbyte),
        "--local_amt", str(input_amount),
        "--fee_rate_ppm", str(fee_rate_ppm)
    ]
    
    # Add outpoints if present
    if formatted_outpoints:
        # Assuming formatted_outpoints is a string like "--utxo hash:index --utxo hash:index"
        # We need to split it for the list format
        parts = formatted_outpoints.split()
        command.extend(parts)

    logging.info(f"Executing command: {' '.join(command)}")
    std_err_output = "N/A" # Initialize stderr output

    try:
        # Run the command as list with shell=False
        result = subprocess.run(
            command, check=False, capture_output=True, text=True
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


def get_node_connection_details(peer_pubkey: str) -> list[dict]:
    """
    Fetches all node addresses (IP/Tor) and related info for a given peer pubkey from Amboss.
    Returns a list of dictionaries with 'addr', 'network', and 'country' (if available).
    Prioritizes non-Tor addresses.
    """
    logging.info(f"Fetching all connection details for pubkey: {peer_pubkey} from Amboss...")
    
    node_details = get_node_extended_details(peer_pubkey)

    if not node_details:
        logging.error(f"Failed to get extended details for {peer_pubkey} for connection.")
        return []

    addresses_raw = node_details.get("graph_info", {}).get("node", {}).get("addresses", [])
    
    if not addresses_raw:
        logging.warning(f"No addresses found for {peer_pubkey} on Amboss.")
        return []

    connection_details = []
    for address_entry in addresses_raw:
        addr = address_entry.get("addr")
        network = address_entry.get("network")
        country = address_entry.get("ip_info", {}).get("country") if address_entry.get("ip_info") else None

        if addr and network:
            detail = {
                "addr": addr,
                "network": network,
                "country": country
            }
            connection_details.append(detail)
            logging.debug(f"Found address: {addr}, Network: {network}, Country: {country}")

    # Prioritize non-Tor addresses
    # Sort: put non-Tor addresses first (check for '.onion' in addr for Tor)
    clearnet_addresses = [d for d in connection_details if ".onion" not in d["addr"]]
    tor_addresses = [d for d in connection_details if ".onion" in d["addr"]]

    # Return clearnet addresses first, then Tor addresses
    final_ordered_addresses = clearnet_addresses + tor_addresses
    
    logging.info(f"Returning {len(final_ordered_addresses)} connection details for {peer_pubkey}.")
    return final_ordered_addresses


def connect_to_node(peer_pubkey: str, connection_details_list: list[dict], max_retries=None) -> tuple[int, str | None, str | None]:
    """
    Attempts to connect to a node using multiple addresses provided in a prioritized list.
    Retries each address based on MAX_CONNECT_RETRIES.
    
    Args:
        peer_pubkey: The public key of the peer.
        connection_details_list: A list of dicts, each containing 'addr', 'network', 'country'.
        max_retries: Maximum connection attempts per address.
        
    Returns:
        tuple: (0 for success, 1 for failure, connected_addr_uri if successful, error_message if failed)
    """
    if max_retries is None:
        max_retries = MAX_CONNECT_RETRIES
    overall_last_stderr = "No connection attempts made."
    
    if not connection_details_list:
        error_message = f"No connection addresses provided for peer {peer_pubkey}."
        logging.error(error_message)
        return 1, None, error_message # No addresses to try
        
    for detail in connection_details_list:
        address_to_try = detail["addr"]
        network = detail["network"]
        country_info = f" ({detail['country']})" if detail['country'] else ""
        node_key_address = f"{peer_pubkey}@{address_to_try}"
        
        logging.info(f"Attempting to connect to peer {peer_pubkey} via {network} address {address_to_try}{country_info}...")

        retries = 0
        while retries < max_retries:
            command = [LNCLI_PATH, "connect", node_key_address, "--timeout", "120s"]
            logging.info(f"Executing connect command (attempt {retries + 1}/{max_retries} for current address): {' '.join(command)}")
            try:
                result = subprocess.run(command, capture_output=True, text=True, check=False)
                current_stderr = result.stderr.strip() if result.stderr else "N/A"
                overall_last_stderr = current_stderr # Keep track of the very last stderr encountered

                if result.returncode == 0:
                    logging.info(f"Successfully connected to node {node_key_address}")
                    return 0, node_key_address, None  # Success
                elif "already connected to peer" in current_stderr.lower():
                    logging.info(f"Peer {node_key_address} is already connected.")
                    return 0, node_key_address, None  # Already connected is also a success
                else:
                    logging.error(
                        f"Error connecting to node {node_key_address} (attempt {retries + 1}): {current_stderr}"
                    )
            except subprocess.CalledProcessError as e:
                current_stderr = e.stderr.strip() if e.stderr else "N/A"
                overall_last_stderr = current_stderr
                logging.error(f"CalledProcessError executing lncli connect (attempt {retries + 1}): {e}. stderr: {current_stderr}")
            except Exception as e:
                current_stderr = f"Unexpected Exception: {str(e)}"
                overall_last_stderr = current_stderr
                logging.error(f"Unexpected error executing lncli connect (attempt {retries + 1}): {current_stderr}")
            
            retries += 1
            if retries < max_retries:
                logging.info(f"Waiting {CONNECT_RETRY_DELAY_SECONDS}s before retrying connection to {node_key_address}")
                time.sleep(CONNECT_RETRY_DELAY_SECONDS)
        
        logging.warning(f"Failed to connect to {node_key_address} after {max_retries} retries. Trying next address if available.")

    # If we reach this point, all addresses and retries have failed
    error_message = f"Failed to connect to peer {peer_pubkey} after trying all available addresses and retries. Last error: {overall_last_stderr}"
    logging.error(error_message)
    return 1, None, error_message # Failure


def _handle_critical_litloop_error(detail: str, context: str):
    """Handles critical errors related to litloop, logs, notifies, and halts the script.
    
    Args:
        detail: The detailed error message
        context: Context describing what operation failed
    """
    failure_message = f"🔥 CRITICAL: {context}. Details: `{detail}`. Script aborted."
    logging.critical(failure_message)
    send_telegram_notification(failure_message, level="error", parse_mode="Markdown")
    
    logging.warning(f"Creating critical error flag due to litloop failure: {detail}")
    with open(CRITICAL_ERROR_FILE_PATH, "a") as log_file:
        log_file.write(f"{datetime.now()}: litloop command failed. Error: {detail}\n")
    
    raise RuntimeError(f"litloop failure: {detail}")


def get_lncli_utxos():
    # First get all UTXOs from LND
    command = [LNCLI_PATH, "listunspent", "--min_confs=3"]
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
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
            # Construct the litloop command as list
            litloop_cmd = [
                loop_path,
                "--rpcserver=localhost:8443",
                "--tlscertpath=~/.lit/tls.cert",
                "static",
                "listunspent"
            ]
            process = subprocess.Popen(
                litloop_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            output, error = process.communicate()
            output = output.decode("utf-8")
            error = error.decode("utf-8") if error else ""
            return_code = process.returncode

            # Check if litloop command failed (non-zero return code, empty output, or JSON decode error)
            # Since loop_path is configured, we must be able to query litloop to avoid using reserved UTXOs
            litloop_error_occurred = False
            error_message_detail = "Unknown litloop error"

            if return_code != 0:
                litloop_error_occurred = True
                error_message_detail = f"litloop command failed with return code {return_code}. stderr: {error.strip() if error else 'No stderr output'}"
                logging.error(f"litloop command failed: {error_message_detail}")
            elif not output or not output.strip():
                litloop_error_occurred = True
                error_message_detail = f"litloop command returned empty output. stderr: {error.strip() if error else 'No stderr output'}"
                logging.error(f"litloop command returned empty output: {error_message_detail}")
            else:
                try:
                    loop_data = json.loads(output)
                    loop_utxos = loop_data.get("utxos", [])
                    logging.info(f"Found {len(loop_utxos)} static loop UTXOs")
                except json.JSONDecodeError as e:
                    litloop_error_occurred = True
                    error_message_detail = f"Failed to decode litloop JSON output: {e}. Output: {output[:200] if output else 'Empty'}. stderr: {error.strip() if error else 'No stderr output'}"
                    logging.error(f"Error decoding litloop output: {error_message_detail}")

            # If litloop is configured but failed, this is critical - we cannot safely proceed
            # as we may try to use reserved static loop UTXOs, causing channel open failures
            if litloop_error_occurred:
                _handle_critical_litloop_error(
                    error_message_detail,
                    "litloop command failed when attempting to list static loop UTXOs. The litloop service may not be running"
                )
    except RuntimeError:
        # Re-raise RuntimeError (our critical error) to propagate up and abort execution
        raise
    except Exception as e:
        # For other unexpected errors, if loop_path is set, treat as critical
        if loop_path and os.path.exists(loop_path):
            error_message_detail = f"Unexpected error executing litloop: {str(e)}"
            _handle_critical_litloop_error(
                error_message_detail,
                "Unexpected error when attempting to query litloop"
            )
        else:
            # If loop_path is not configured, just log and continue (non-critical)
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
    inputs_size = utxos_needed * 57.5  # Each UTXO is 57.5 vBytes
    outputs_size = 2 * 43  # Two outputs of 43 vBytes each
    overhead_size = 10.5  # Transaction overhead of 10.5 vBytes
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
    # It's better to pass command as a list when shell=False
    # This avoids shell interpretation issues with quotes in the message.
    command = [
        FULL_PATH_BOS,
        "send",
        config['info']['NODE'],
        "--amount", str(amount),
        "--avoid-high-fee-routes",
        "--message", f"HODLmeTight Amboss Channel Sale with {peer_pubkey}"
    ]
    logging.info(f"Executing BOS command: {' '.join(command)}")

    try:
        # Changed shell=True to shell=False, and added timeout for robustness
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=120 # Added a 2-minute timeout
        )
        logging.info(f"BOS Command Output: stdout='{result.stdout.strip()}', stderr='{result.stderr.strip()}'")
        return result.stdout
    except subprocess.CalledProcessError as e:
        logging.error(f"Error executing BOS command: {e}")
        logging.error(f"BOS command failed. stdout: '{e.stdout.strip()}', stderr: '{e.stderr.strip()}'")
        return None
    except subprocess.TimeoutExpired as e:
        logging.error(f"BOS command timed out after {e.timeout} seconds.")
        logging.error(f"BOS command timed out. stdout: '{e.stdout.strip()}', stderr: '{e.stderr.strip()}'")
        return None
    except Exception as e:
        logging.exception(f"Unexpected error in bos_confirm_income: {e}")
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

        # Immediately acknowledge the callback to stop the client-side loading animation
        decision_text_verb = "Approved" if action == "approve" else "Rejected"
        bot.answer_callback_query(call.id, text=f"Order {order_id} {decision_text_verb}. Processing...")

        order_original_details = confirmation_details_entry["details"]
        buyer_alias = order_original_details.get("buyer_alias", "N/A")
        buyer_pubkey = order_original_details.get('account') or order_original_details.get("endpoints", {}).get("destination", "Unknown")
        amount = order_original_details['seller_invoice_amount']

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
                # Run long-running task in a separate thread to avoid blocking Telegram polling
                threading.Thread(target=_complete_offer_approval_process, args=(order_id, order_original_details), name=f"Approve-{order_id}").start()
            else:
                msg = f"⚠️ Order `{order_id}` status changed to `{order_original_details.get('status')}` before user approval ({action}) could be fully processed. No action taken."
                logging.warning(msg)
                send_telegram_notification(msg, level="warning", parse_mode="Markdown")

        elif action == "reject":
            send_telegram_notification(f"🗑️ Rejecting order `{order_id}` ({buyer_alias}) on Amboss.", parse_mode="Markdown")
            # Thread rejection as well to be safe and responsive
            threading.Thread(target=reject_order, args=(order_id,), name=f"Reject-{order_id}").start()

    except Exception as e:
        logging.exception(f"Error in order_decision_callback for call data {call.data}:")
        try:
            bot.answer_callback_query(call.id, text="Error processing your decision.")
        except:
            pass
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


def check_pending_confirmations_timeouts():
    """Checks for timed-out offers in pending_user_confirmations."""
    global pending_user_confirmations
    current_time = time.time()
    timed_out_orders_ids = []
    
    # Use list() to create a copy of keys/items for thread-safe iteration
    for order_id, info in list(pending_user_confirmations.items()):
        if current_time - info["timestamp"] > USER_CONFIRMATION_TIMEOUT_SECONDS:
            timed_out_orders_ids.append(order_id)
            
    for order_id in timed_out_orders_ids:
        # Pop safely; another thread (callback) might have handled it
        confirmation_info = pending_user_confirmations.pop(order_id, None)
        if confirmation_info:
            _handle_timeout_for_offer(order_id, confirmation_info)

def process_new_offers():
    """
    Checks for new offers (WAITING_FOR_SELLER_APPROVAL).
    If a new offer is found, it asks the user for confirmation via Telegram, including the buyer's alias.
    It also handles timeouts for offers previously presented to the user.
    """
    global pending_user_confirmations

    # Timeout logic moved to check_pending_confirmations_timeouts() to run more frequently

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

# Fetch extended buyer details
    extended_details_parts = []
    if destination_pubkey:
        node_details = get_node_extended_details(destination_pubkey)
        if node_details:
            # Amboss specific
            amboss_info = node_details.get("amboss")
            if amboss_info and amboss_info.get("is_claimed") is not None:
                extended_details_parts.append(f"Claimed: {'Yes' if amboss_info['is_claimed'] else 'No'}")

            # Graph Info
            graph_info = node_details.get("graph_info")
            if graph_info:
                channels_info = graph_info.get("channels")
                if channels_info:
                    if channels_info.get("num_channels") is not None:
                        extended_details_parts.append(f"Channels: {channels_info['num_channels']}")
                    if channels_info.get("total_capacity"):
                        try:
                            capacity_sats = int(channels_info['total_capacity'])
                            extended_details_parts.append(f"Capacity: {capacity_sats:,} sats")
                        except ValueError:
                            logging.warning(f"Could not parse total_capacity: {channels_info['total_capacity']}")


            # Socials
            socials = node_details.get("socials")
            if socials:
                social_info = socials.get("info")
                if social_info:
                    for key, label in [
                        ("email", "Email"), ("nostr_username", "Nostr"),
                        ("telegram", "Telegram"), ("twitter", "Twitter"),
                        ("message", "Msg") # Keep "Msg" short
                    ]:
                        value = social_info.get(key)
                        if value and str(value).strip(): # Ensure it's not None or empty string
                            # Truncate long messages
                            if key == "message" and len(str(value)) > 70:
                                value_display = str(value)[:67] + "..."
                            else:
                                value_display = str(value)
                            extended_details_parts.append(f"{label}: `{value_display}`")
                
                ll_info = socials.get("lightning_labs", {}).get("terminal_web")
                if ll_info and ll_info.get("position") is not None:
                    extended_details_parts.append(f"TermRank: {ll_info['position']}")
                
                lnplus_info = socials.get("ln_plus", {}).get("rankings")
                if lnplus_info and lnplus_info.get("rank_name"):
                    extended_details_parts.append(f"LN+Rank: {lnplus_info['rank_name']}")
    
    # Construct the prompt message
    prompt_lines = [
        f"🔔 New Magma Offer:",
        f"ID: `{order_id}`",
        f"💰 Amount: {seller_invoice_amount} sats",
        f"👤 Buyer: `{buyer_alias}` ({destination_pubkey[:10]}...)",
    ]

    if extended_details_parts:
        prompt_lines.append("Buyer Details:")
        for part in extended_details_parts:
            prompt_lines.append(f"  • {part}")
    
    prompt_lines.append(f"⏳ Please Approve/Reject within 5 min.")
    prompt_message = "\n".join(prompt_lines)

    # Define the inline keyboard markup
    markup = types.InlineKeyboardMarkup()
    approve_button = types.InlineKeyboardButton("✅ Approve Offer", callback_data=f"decide_order:approve:{order_id}")
    reject_button = types.InlineKeyboardButton("❌ Reject Offer", callback_data=f"decide_order:reject:{order_id}")
    markup.add(approve_button, reject_button)
    
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
                send_telegram_notification(
                    f"💰 Buyer paid for order `{order_id}`! Status: `{current_status}`.\nProceeding to open channel.", 
                    parse_mode="Markdown"
                )
                logging.info(f"Order {order_id} is now WAITING_FOR_CHANNEL_OPEN. Triggering channel open process.")
                process_paid_order(order_details)
                return 
            elif current_status in ["CANCELLED", "EXPIRED", "SELLER_REJECTED", "ERROR", "COMPLETED"]: 
                send_telegram_notification(f"ℹ️ Order `{order_id}` is now `{current_status}`. Stopped active payment monitoring.", parse_mode="Markdown")
                logging.info(f"Order {order_id} reached terminal state {current_status}. Stopping active poll.")
                return
        else:
            logging.warning(f"Could not fetch details for order {order_id} during active poll.")

        time.sleep(ACTIVE_ORDER_POLL_INTERVAL_SECONDS)

    send_telegram_notification(f"⏳ Order `{order_id}`: Buyer did not pay within {ACTIVE_ORDER_POLL_DURATION_MINUTES} min. Stopped active monitoring.", parse_mode="Markdown")
    logging.info(f"Active polling for order {order_id} finished. Buyer did not pay or status not WAITING_FOR_CHANNEL_OPEN in time.")


def process_paid_order(order_details):
    """Processes a single order that is WAITING_FOR_CHANNEL_OPEN."""
    order_id = "UNKNOWN_ORDER_ID" # Default for logging in case order_details is not as expected
    try:
        if not isinstance(order_details, dict):
            logging.error(f"process_paid_order called with invalid order_details type: {type(order_details)}. Details: {order_details}")
            send_telegram_notification("🔥 Critical internal error: Invalid data for processing paid order. Check logs.", level="error")
            return

        order_id = order_details.get('id', 'MISSING_ID')
        customer_pubkey = order_details.get('account')
        channel_size_str = order_details.get('size')
        seller_invoice_amount_str = order_details.get('seller_invoice_amount')

        if not all([order_id != 'MISSING_ID', customer_pubkey, channel_size_str, seller_invoice_amount_str]):
            logging.error(f"Missing critical fields in order_details for order {order_id}: Pubkey={customer_pubkey}, Size={channel_size_str}, InvoiceAmount={seller_invoice_amount_str}")
            send_telegram_notification(f"🔥 Critical internal error: Incomplete data for processing paid order `{order_id}`. Check logs.", level="error", parse_mode="Markdown")
            return
        
        # Ensure numeric types are correctly handled, Amboss provides strings.
        try:
            channel_size = int(channel_size_str)
            seller_invoice_amount = int(seller_invoice_amount_str)
        except ValueError as ve:
            logging.error(f"Could not convert size or seller_invoice_amount to int for order {order_id}. Size='{channel_size_str}', Amount='{seller_invoice_amount_str}'. Error: {ve}")
            send_telegram_notification(f"🔥 Error: Invalid numeric data (size/amount) for order `{order_id}`.", level="error", parse_mode="Markdown")
            return

        buyer_alias = get_node_alias(customer_pubkey)

        send_telegram_notification(
            f"⚡️ Processing paid order `{order_id}`:\n"
            f"👤 Buyer: `{buyer_alias}` ({customer_pubkey[:10]}...)\n"
            f"📦 Size: {channel_size:,} sats\n"
            f"🧾 Invoice: {seller_invoice_amount:,} sats",
            parse_mode="Markdown"
        )

        # 1. Connect to Peer
        send_telegram_notification(f"🔗 Attempting to connect to peer `{buyer_alias}` ({customer_pubkey[:10]}...) for order `{order_id}`.", parse_mode="Markdown")
        
        # Use the new function to get a list of prioritized connection details
        connection_details = get_node_connection_details(customer_pubkey)

        if not connection_details:
            error_msg = f"🔥 Could not get any address details for peer `{buyer_alias}` ({customer_pubkey[:10]}...) (Order `{order_id}`). Connection might fail."
            logging.error(error_msg)
            send_telegram_notification(error_msg, level="error", parse_mode="Markdown")
            # We continue anyway, as maybe we are already connected or LND knows a path.
        
        # Pass the list to the updated connect_to_node function
        # It returns: (status_code, connected_address_uri, error_msg)
        node_connection_status, connected_addr, conn_error_msg = connect_to_node(customer_pubkey, connection_details)
        
        if node_connection_status == 0:
            # connected_addr will contain the specific address we succeeded with
            success_msg = f"✅ Successfully connected to `{buyer_alias}`."
            if connected_addr:
                 success_msg += f" ({connected_addr})"
            send_telegram_notification(success_msg, parse_mode="Markdown")
        else:
            tg_error_msg = (
                f"⚠️ Could not connect to `{buyer_alias}` for order `{order_id}` after trying all addresses.\n"
                f"Last error: `{conn_error_msg}`\n"
                f"Will attempt channel open anyway (LND might handle it)."
            )
            send_telegram_notification(tg_error_msg, level="warning", parse_mode="Markdown")

        # 2. Open Channel
        send_telegram_notification(f"🛠️ Attempting to open {channel_size:,} sats channel with `{buyer_alias}` ({customer_pubkey[:10]}...) for order `{order_id}`.", parse_mode="Markdown")
        funding_tx, msg_open_or_error = open_channel(customer_pubkey, channel_size, seller_invoice_amount)

        if funding_tx is None:
            error_msg = f"🔥 Failed to open channel for order `{order_id}`.\n`lncli` error: `{msg_open_or_error}`"
            logging.error(error_msg) 
            send_telegram_notification(error_msg, level="error", parse_mode="Markdown")
            return
        
        send_telegram_notification(
            f"✅ Channel opening initiated for order `{order_id}`.\nFunding TX: `{funding_tx}`\nDetails: `{msg_open_or_error}`", 
            parse_mode="Markdown"
        )

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
            return
        send_telegram_notification(f"✅ Channel point for order `{order_id}`: `{channel_point}`", parse_mode="Markdown")

        # 4. Confirm Channel Point to Amboss (with a small delay)
        logging.info(f"Waiting 10 seconds before confirming channel point {channel_point} to Amboss for order {order_id}.")
        time.sleep(10)
        send_telegram_notification(f"📡 Confirming channel point to Amboss for order `{order_id}`...", parse_mode="Markdown")
        
        channel_confirmed_result = confirm_channel_point_to_amboss(order_id, channel_point)
        has_errors = False
        if channel_confirmed_result is None:
            has_errors = True
            if not (isinstance(channel_confirmed_result, dict) and "errors" in channel_confirmed_result):
                 channel_confirmed_result = {"errors": [{"message": "Network/request level error during Amboss confirmation or helper returned None."}]}
        elif isinstance(channel_confirmed_result, dict) and "errors" in channel_confirmed_result:
            has_errors = True
            
        if has_errors:
            err_detail = channel_confirmed_result.get("errors", [{"message": "Unknown confirmation failure"}])[0].get("message", "details unavailable")
            error_msg = f"🔥 Failed to confirm channel point `{channel_point}` to Amboss for order `{order_id}`. Result: `{err_detail}`. Confirm manually."
            logging.error(f"{error_msg} Full Amboss response: {channel_confirmed_result}")
            send_telegram_notification(error_msg, level="error", parse_mode="Markdown")
            return

        success_msg = f"✅ Channel for order `{order_id}` confirmed to Amboss!"
        logging.info(f"{success_msg} Result: {channel_confirmed_result}")
        send_telegram_notification(success_msg, parse_mode="Markdown")

        # 5. BOS Confirm Income (Optional accounting step)
        if FULL_PATH_BOS and config.has_option('info', 'NODE') and config.get('info', 'NODE'): # Check if BOS configured
            logging.info(f"Attempting BOS income confirmation for order {order_id}, peer pubkey: {customer_pubkey}")
            bos_result = bos_confirm_income(seller_invoice_amount, peer_pubkey=customer_pubkey) 
            if bos_result:
                send_telegram_notification(f"✅ BOS income confirmation for order `{order_id}` initiated. Check logs for details.", level="info", parse_mode="Markdown")
                logging.info(f"BOS income confirmation for order {order_id} processed. Full output in logs.")
            else:
                send_telegram_notification(f"⚠️ BOS income confirmation for order `{order_id}` failed. Check logs.", level="warning", parse_mode="Markdown")
                logging.error(f"BOS income confirmation failed for order {order_id}.")
        else: 
            logging.info(f"Skipping BOS income confirmation for order {order_id} as BOS path or node info is not configured.")

        logging.info(f"Successfully processed paid order {order_id}.")

    except Exception as e:
        # If order_id was not determinable early, use the default
        logging.exception(f"Unhandled exception in process_paid_order for order_id '{order_id}':")
        # Ensure order_id in message is the best effort
        safe_order_id_msg = order_id if order_id not in ['UNKNOWN_ORDER_ID', 'MISSING_ID'] else 'an order'
        send_telegram_notification(
            f"🔥 Critical internal error processing paid order `{safe_order_id_msg}`. Check logs. Error: `{str(e)}`", 
            level="error", 
            parse_mode="Markdown"
        )
        # Do not create CRITICAL_ERROR_FILE_PATH here, let the main loop's catch-all handle systemic issues.
        # This function should return to allow the bot to continue processing other tasks if possible.
        return


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
        
        # Check for timeouts every 1 minute to ensure responsive auto-approval (independent of heavy polling)
        schedule.every(1).minutes.do(check_pending_confirmations_timeouts)
        
        schedule.every(POLLING_INTERVAL_MINUTES).minutes.do(execute_bot_behavior)
        
        # Define robust polling wrapper with automatic restart on failure
        def run_telegram_polling():
            """Runs Telegram polling with automatic restart on failure.
            
            Uses infinity_polling() instead of polling() for better error recovery.
            If polling crashes (network issues, timeouts, etc.), it will automatically
            restart after a short delay. This prevents the silent thread death that
            causes callback buttons to stop working.
            """
            restart_count = 0
            while True:
                try:
                    if restart_count > 0:
                        logging.warning(f"Telegram polling restart #{restart_count}")
                        send_telegram_notification(
                            f"⚠️ Telegram poller restarted (attempt #{restart_count}). "
                            "If you see this frequently, check network connectivity.",
                            level="warning"
                        )
                    logging.info("Starting Telegram bot infinity_polling...")
                    # infinity_polling handles most transient errors internally
                    # timeout: connection timeout for requests
                    # long_polling_timeout: how long Telegram waits before returning empty response
                    bot.infinity_polling(timeout=60, long_polling_timeout=30)
                except Exception as e:
                    restart_count += 1
                    logging.error(f"Telegram polling crashed with error: {e}. Restarting in 10 seconds... (restart #{restart_count})")
                    time.sleep(10)
        
        # Start the Telegram bot polling in a separate daemon thread
        logging.info("Starting Telegram bot poller thread with infinity_polling.")
        telegram_thread = threading.Thread(target=run_telegram_polling, name="TelegramPoller", daemon=True)
        telegram_thread.start()

        # Run scheduled tasks in the main thread
        logging.info("Entering main scheduling loop.")
        while True:
            schedule.run_pending()
            time.sleep(1)
