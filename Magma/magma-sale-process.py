# This is a refactored code of https://github.com/jvxis/nr-tools
# Working on combining and simplifying a few code elements to allow
# for faster turnaround times and more dynamic message adjustments.
# Improving logging too.
# enter the necessary settings in config.ini file in the parent dir

import requests
import telebot
import json
from telebot import types
from typing import Tuple, List, Optional
import subprocess
import time
import os
import sys
import polling2
from datetime import datetime
import configparser
import logging  # For more structured debugging

# Get the path to the parent directory
parent_dir = os.path.dirname(os.path.abspath(__file__))

# path to the config.ini file located in the parent directory
config_file_path = os.path.join(parent_dir, '..', 'config.ini')
config = configparser.ConfigParser()
config.read(config_file_path)

# Variables
INVOICE_EXPIRY_SECONDS = 180000
MAX_FEE_PERCENTAGE = 0.90
FEE_RATE_PPM = 350 # let's pick this up from the query
RETRY_DELAY_SECONDS = 60  # Retry every minute if we can't connect to the buyer
MAX_CONNECTION_RETRIES = 30 # Retry to connect for half an hour, than abort the script
MEMPOOL_API_URL = 'https://mempool.space/api/v1/fees/recommended'

# Constants
UTXO_INPUT_SIZE = 57.5  # vBytes (approximate)
OUTPUT_SIZE = 43  # vBytes (approximate)
TRANSACTION_OVERHEAD = 10.5  # vBytes (approximate)

TOKEN = config['telegram']['magma_bot_token']
AMBOSS_TOKEN = config['credentials']['amboss_authorization']
CHAT_ID = config['telegram']['telegram_user_id']

magma_channel_list = config['paths']['charge_lnd_path']
full_path_bos = config['system']['full_path_bos']

# Amboss API details
AMBOSS_API_URL = 'https://api.amboss.space/graphql'
AMBOSS_API_HEADERS = {
    'Content-Type': 'application/json',
    'Authorization': f'Bearer {AMBOSS_TOKEN}'
}

# Main logger (for general information and debugging)
main_logger = logging.getLogger('main')
main_logger.setLevel(logging.DEBUG)  # Capture DEBUG and above
main_handler = logging.FileHandler(os.path.join(parent_dir, '..', 'logs', 'magma-auto-sale2.log'))
main_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
main_logger.addHandler(main_handler)

# Error logger (for errors and critical issues)
error_logger = logging.getLogger('error')
error_logger.setLevel(logging.ERROR)
error_handler = logging.FileHandler(os.path.join(parent_dir, '..', 'logs', 'magma-auto-sale2_error.log'))
error_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
error_logger.addHandler(error_handler)

critical_error_log_path = os.path.join(parent_dir, '..', 'logs', 'magma-critical_error.log')

# Error classes
class AmbossAPIError(Exception):
    """Represents an error when interacting with the Amboss API."""

    def __init__(self, message, status_code=None, response_data=None):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data

class LNDError(Exception):
    """Represents an error when interacting with LND."""
    pass

class InvoiceCreationError(LNDError):  # Subclass for specific LND error
    pass

def handle_critical_error(error_message):
    """Handles critical errors by logging, notifying, creating a flag, and terminating."""
    error_logger.critical(error_message)
    bot.send_message(CHAT_ID, text=f"Critical error: {error_message}. Manual intervention required. Check logs.")

    # Create the flag file
    with open(critical_error_log_path, "w") as f:
        f.write(error_message)

    logging.critical("Terminating script due to critical error.")
    sys.exit(1)  # Exit with a non-zero status to signal failure

#Code
bot = telebot.TeleBot(TOKEN)
logging.info("Amboss Channel Open Bot Started")


def monitor_sell_requests(target_statuses: list[str]) -> tuple[dict | None, dict | None]:
    """Fetches Amboss API orders with the specified statuses.

    Args:
        target_statuses (list[str]): A list of desired order statuses to filter for.

    Returns:
        tuple[dict | None, dict | None]: The first matching order and extensions, or None for each if not found or an error occurs.
    """
    query = """
    {
      getUser {
        market {
          offer_orders {
            list {
              id
              size
              status
              account
              seller_invoice_amount
              locked_fee_rate_cap
              endpoints {
                destination
              }
            }
          }
        }
      }
    }
    """

    try:
        response = requests.post(AMBOSS_API_URL, json={"query": query}, headers=AMBOSS_API_HEADERS)
        response.raise_for_status()  # Raise exception for HTTP errors

        data = response.json()
        # print(data)  # Print the entire response
        offer_orders = data.get('data', {}).get('getUser', {}).get('market', {}).get('offer_orders', {}).get('list', [])

        matching_order = next(
            (offer for offer in offer_orders if offer.get('status') in target_statuses), None
        )
        if matching_order:
            # uncomment for detailed debugging
            # main_logger.info("Found order with status '%s': %s", target_status, matching_order)
            return matching_order, data.get('extensions')  # Return both order and extensions

        return matching_order, data.get('extensions')  # Return None for order, but still include extensions

    except requests.exceptions.RequestException as e:
        error_logger.error("API request failed: %s", e)
        raise AmbossAPIError("Amboss API unavailable") from e


def adjust_poll_interval(extensions: dict, current_poll_interval: float) -> float:
    """Adjusts the polling interval based on the throttleStatus from the Amboss API response.

    Args:
        extensions (dict): The extensions data from the Amboss API response.
        current_poll_interval (float): The current polling interval in seconds.

    Returns:
        float: The adjusted polling interval in seconds.
    """

    throttle_status = extensions.get('cost', {}).get('throttleStatus', {})
    currently_available = throttle_status.get('currentlyAvailable', 0)
    restore_rate = throttle_status.get('restoreRate', 0)

    # Calculate estimated time to restore enough credits for the next request
    query_cost = extensions.get('cost', {}).get('requestedQueryCost', 0)  # Get query cost
    restore_time = query_cost / restore_rate if restore_rate > 0 else 60  # Default to 60 seconds if restore_rate is 0

    if currently_available < query_cost:
        new_poll_interval = max(restore_time, 60)  # Wait at least 60 seconds or the restore time
        main_logger.warning("API rate limit approaching, increasing poll_interval to %s seconds", new_poll_interval)
        print(f"Adjusted poll_interval: {new_poll_interval}")
        return new_poll_interval

    print(f"Using current poll_interval: {current_poll_interval}")
    return current_poll_interval  # No adjustment needed


def create_lightning_invoice(amount: int, memo: str, expiry: int) -> tuple[str | None, str | None]:
    """Creates a Lightning invoice using lncli and optionally sends it to Telegram.

    Args:
        amount (int): The invoice amount in satoshis.
        memo (str): A descriptive memo for the invoice.
        expiry (int): The invoice expiry time in seconds.
        chat_id (int, optional): The Telegram chat ID to send the invoice to (defaults to None for no message).

    Returns:
        A tuple containing:
            - The payment hash (str) if successful, or None if failed.
            - The payment request (str) if successful, or None if failed.

    Raises:
        LNDError: If there's an error creating the invoice.
    """

    command = f"lncli addinvoice --memo '{memo}' --amt {amount} --expiry {expiry}"

    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, check=True)
        output_json = json.loads(result.stdout)

        payment_hash = output_json.get("r_hash")
        payment_request = output_json.get("payment_request")

        if payment_request:
            main_logger.info("Created Lightning invoice: %s (hash: %s)", payment_request, payment_hash)
        else:
            raise LNDError(f"Failed to create invoice (amount: {amount}, memo: {memo}): {output_json}")

        return payment_hash, payment_request

    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        error_logger.error("Error creating Lightning invoice: %s. Command: %s", e, command)
        raise LNDError("Failed to create Lightning invoice") from e


def accept_order(order_id: str, payment_request: str) -> dict:
    """Accepts an order on Amboss using the provided order ID and payment request.

    Args:
        order_id (str): The ID of the order to accept.
        payment_request (str): The payment request associated with the order.

    Returns:
        dict: The JSON response from the Amboss API, or None if an error occurs.

    Raises:
        AmbossAPIError: If there's an error accepting the order or sending the message.
    """

    query = """
        mutation AcceptOrder($orderId: String!, $request: String!) {
          sellerAcceptOrder(id: $orderId, request: $request)
        }
    """
    variables = {"orderId": order_id, "request": payment_request}

    try:
        response = requests.post(AMBOSS_API_URL, json={"query": query, "variables": variables}, headers=AMBOSS_API_HEADERS)
        print(response.json())  # Print the raw response
        response.raise_for_status()  # Raise exception for HTTP errors

        result = response.json()

        if payment_request is None:
            error_message = f"Cannot accept order {order_id}: invoice creation failed."
            error_logger.error(error_message)
            raise LNDError(error_message)
        
        if result.get('data', {}).get('sellerAcceptOrder'):
            main_logger.info("Order %s accepted successfully", order_id)
            bot.send_message(CHAT_ID, text=f"Order {order_id} accepted successfully!\nInvoice:\n{payment_request}")
        else:
            error_message = f"Failed to accept order {order_id}."
            if 'errors' in result:
                error_message += f" Error details: {result['errors']}"
            error_logger.error(error_message)
            bot.send_message(CHAT_ID, text=error_message)

        return result

    except requests.exceptions.RequestException as e:
        error_logger.error("Error accepting order %s: %s", order_id, e)
        raise AmbossAPIError("Failed to accept order") from e


def get_and_calculate_utxos() -> list[dict]:
    """Retrieves unspent transaction outputs (UTXOs) from lncli, sorted by amount (descending).

    Returns:
        A list of dictionaries representing UTXOs, each containing 'txid', 'vout', 'address', and 'amount_sat' fields.

    Raises:
        LNDError: If there's an error executing lncli or decoding the JSON output.
    """

    command = "lncli listunspent --min_confs=3"

    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, check=True)

        # Parse JSON output, handling potential errors
        try:
            data = json.loads(result.stdout)
            utxos = data.get("utxos", [])
        except json.JSONDecodeError as e:
            error_logger.error("Error decoding lncli output: %s", e)
            raise LNDError("Failed to decode lncli output") from e

        # Sort UTXOs by amount_sat in descending order
        utxos.sort(key=lambda x: x.get("amount_sat", 0), reverse=True)

        main_logger.info("Retrieved UTXOs: %s", utxos)
        return utxos

    except subprocess.CalledProcessError as e:
        error_logger.error("Error executing lncli: %s. Output: %s", e, e.output)
        raise LNDError("Failed to execute lncli") from e


def calculate_transaction_size(utxos_needed: int, num_outputs: int = 2) -> int:
    """Calculates the estimated size of a Bitcoin transaction in virtual bytes (vBytes).

    Args:
        utxos_needed (int): The number of UTXOs used as inputs in the transaction.
        num_outputs (int, optional): The number of transaction outputs (defaults to 2).

    Returns:
        int: The estimated transaction size in vBytes.
    """

    inputs_size = utxos_needed * UTXO_INPUT_SIZE
    outputs_size = num_outputs * OUTPUT_SIZE
    total_size = inputs_size + outputs_size + TRANSACTION_OVERHEAD
    total_size = int(total_size)  # Convert to integer (rounding down)
    return total_size


def calculate_utxos_required_and_fees(target_amount: int, fee_per_vbyte: int) -> Tuple[int, int, Optional[List[dict]]]:
    utxos_data = sorted(get_and_calculate_utxos(), key=lambda x: x["amount_sat"], reverse=True)  # Sort by amount descending
    total_available = sum(utxo["amount_sat"] for utxo in utxos_data)

    if total_available < target_amount:
        error_message = f"Insufficient UTXOs: Need {target_amount} sats, have {total_available} sats"
        handle_critical_error(error_message)  # Call the error handler
        return -1, 0, None

    utxos_needed = 0
    accumulated_amount = 0
    selected_utxos = []
    fee_cost = 0

    for utxo in utxos_data:
        utxos_needed += 1
        accumulated_amount += utxo["amount_sat"]
        selected_utxos.append(utxo)

        tx_size = calculate_transaction_size(utxos_needed)
        fee_cost = tx_size * fee_per_vbyte

        if accumulated_amount >= target_amount + fee_cost:
            break

    profitability = (target_amount - fee_cost) / target_amount
    if profitability < MAX_FEE_PERCENTAGE:
        error_message = f"Order not profitable: Profitability {profitability:.2%}, below maximum allowed {MAX_FEE_PERCENTAGE:.2%}"
        handle_critical_error(error_message)  # Call the error handler
        return -1, 0, None

    return utxos_needed, fee_cost, selected_utxos


def check_mempool_fees_and_profitability(order: dict, max_fee_percentage: float = MAX_FEE_PERCENTAGE) -> Optional[int]:
    """Fetches mempool fee estimates and checks profitability against an order.

    Args:
        order (dict): The order details from Amboss.
        max_fee_percentage (float): Maximum allowed percentage of fee to order amount.

    Returns:
        int or None: The fastest fee from the mempool API if profitable, None otherwise.
    """

    try:
        response = requests.get(MEMPOOL_API_URL)
        response.raise_for_status()  # Raise for HTTP errors
        data = response.json()

        fast_fee = data.get('fastestFee')

        if fast_fee is None:
            error_logger.error("Fastest fee not found in mempool response: %s", data) # Log the response for debugging
            return None  # Return None on error or invalid response

    except requests.exceptions.RequestException as e:
        error_logger.error("Error fetching mempool fees: %s", e)
        return None  # Return None on error

    profitability = (order['seller_invoice_amount'] - fast_fee) / order['seller_invoice_amount']

    if profitability < max_fee_percentage:
        error_logger.warning(
            "Order not profitable: Profitability %.2f%%, below maximum allowed %.2f%%",
            profitability * 100,
            max_fee_percentage * 100,
        )
        return None

    main_logger.info(f"Fastest Fee: {fast_fee} sat/vB")
    return fast_fee

    
def get_buyer_details_and_connect(order_details: dict, max_retries=MAX_CONNECTION_RETRIES) -> bool:
    """Fetches buyer details from Amboss and attempts to connect via lncli.

    Args:
        order_details (dict): The dictionary containing order information.
        max_retries (int): Maximum number of retry attempts (default is 3).

    Returns:
        bool: True if connection is successful or the peer is already connected within retry limit, False otherwise.
    """

    peer_pubkey = order_details.get('endpoints', {}).get('destination')

    if peer_pubkey is None:
        logging.error("No 'peer_pubkey' found in order details: %s", order_details)
        raise ValueError("Missing buyer pubkey")

    url = 'https://api.amboss.space/graphql'
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {AMBOSS_TOKEN}'
    }

    query = """
    query List($pubkey: String!) {
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
    """

    variables = {"pubkey": peer_pubkey}

    try:
        response = requests.post(url, json={"query": query, "variables": variables}, headers=headers)
        response.raise_for_status()  # Raise for HTTP errors
        data = response.json()
    except requests.exceptions.RequestException as e:
        logging.error("Amboss API request failed: %s", e)
        return False  # Return False on API failure

    addresses = data.get('data', {}).get('getNode', {}).get('graph_info', {}).get('node', {}).get('addresses', [])
    first_address = addresses[0]['addr'] if addresses else None

    if first_address:
        node_key_address = f"{peer_pubkey}@{first_address}"
        retries = 0

        while retries < max_retries:
            command = f"lncli connect {node_key_address} --timeout 120s"
            logging.info(f"Connecting to node: {command}")
            try:
                result = subprocess.run(command, shell=True, capture_output=True, text=True)
                if result.returncode == 0:
                    logging.info(f"Successfully connected to node {node_key_address}")
                    return True  
                elif "already connected to peer" in result.stderr:
                    logging.info(f"Peer {node_key_address} is already connected.")
                    return True
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
        return False
    else:
        logging.error("No addresses found for pubkey: %s", peer_pubkey)
        return False


def open_channel(pubkey: str, channel_size: int, fee_rate: int, outpoints: Optional[List[dict]] = None) -> Optional[str]:
    """Opens a channel with the specified peer and parameters.

    Args:
        pubkey (str): The public key of the peer node.
        channel_size (int): The desired size of the channel in satoshis.
        fee_rate (int): The fee rate in satoshis per vbyte.
        outpoints (Optional[List[dict]]): List of UTXO dictionaries (if using specific UTXOs, defaults to None).

    Returns:
        str or None: The funding transaction ID if successful, None otherwise.
    """

    main_logger.info("Opening Channel: %s", pubkey)

    try:
        # Calculate UTXOs and fees
        utxos_needed, fee_cost, selected_utxos = calculate_utxos_required_and_fees(
            channel_size, fee_rate
        )

        if utxos_needed == -1:  # Not enough UTXOs
            raise LNDError(f"Insufficient confirmed balance to open a {channel_size} sats channel.")
        elif fee_cost >= channel_size * MAX_FEE_PERCENTAGE:  # Fee too high
            raise LNDError(
                f"Fee ({fee_cost} sats) is too high relative to the channel size ({channel_size} sats)."
            )

        # Format outpoints for lncli command (if using specific UTXOs)
        formatted_outpoints = ""
        if outpoints:
            formatted_outpoints = " ".join([f"--utxo {utxo['txid']}:{utxo['vout']}" for utxo in outpoints])

        # Construct and execute lncli command 
        # NOTE: we are using channel_size here
        command = (
            f"lncli openchannel "
            f"--node_key {pubkey} --sat_per_vbyte {fee_rate} "
            f"{formatted_outpoints} --local_amt {channel_size} --fee_rate_ppm {FEE_RATE_PPM}"
        )

        main_logger.info(f"Executing lncli command: {command}")
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        if result.returncode != 0:  # Check for errors
            raise LNDError(f"Error opening channel: {result.stderr}")
        
        # Parse the output to get funding_txid (consider using a JSON parser here)
        funding_txid = result.stdout.strip().split('\n')[-1] 
        
        if funding_txid.startswith('funding_txid:'):
            funding_txid = funding_txid.split(':')[1].strip()
            main_logger.info(f"Channel opened with funding transaction: {funding_txid}")
            return funding_txid
        else:
            raise LNDError("Unexpected output from lncli. Could not extract funding_txid.")
    
    except LNDError as e:
        # Handle critical LND error
        handle_critical_error(e)
        return None

    
def get_channel_point(funding_txid: str, max_retries: int = 10, timeout_seconds: int = 300) -> Optional[str]:
    """Retrieves the channel point of a pending channel by its funding transaction ID.

    Args:
        funding_txid (str): The funding transaction ID of the channel.
        max_retries (int): Maximum number of retry attempts (default is 10).
        timeout_seconds (int): Timeout duration in seconds (default is 300 - 5 minutes).

    Returns:
        str or None: The channel point if found within the timeout, None otherwise.
    """

    def fetch_pending_channels():
        """Fetches pending channel information from lncli."""
        try:
            result = subprocess.run(["lncli", "pendingchannels"], capture_output=True, text=True, check=True)
            return json.loads(result.stdout)
        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            error_logger.error("Error fetching pending channels: %s", e)
            return None  # Return None on error to trigger retry

    def check_channel_point(pending_channels_data):
        """Checks if the desired channel point is in the pending channels data."""
        for channel_info in pending_channels_data.get("pending_open_channels", []):
            channel_point = channel_info["channel"]["channel_point"]
            if channel_point.startswith(funding_txid):  # Use startswith for partial match
                return channel_point
        return None  # Return None if not found

    main_logger.info(f"Retrieving channel point for funding tx: {funding_txid}")

    # Polling with retries and timeout
    try:
        channel_point = polling2.poll(
            target=fetch_pending_channels,
            check_success=check_channel_point,
            step=10,  # Poll every 10 seconds
            timeout=timeout_seconds,
            poll_forever=False  
        )
        if channel_point:
            main_logger.info(f"Channel point found: {channel_point}")
            return channel_point
    except polling2.TimeoutException:
        error_logger.error(
            f"Timeout: Channel point not found for funding tx {funding_txid} after {timeout_seconds} seconds."
        )

    handle_critical_error(f"Timeout: Channel point not found for funding tx {funding_txid} after {timeout_seconds} seconds.")
    return None


def confirm_channel_point_to_amboss(order_id: str, funding_txid: str) -> bool:
    """Confirms a channel opening to Amboss by retrieving the channel point and sending it.

    Args:
        order_id (str): The ID of the order to confirm.
        funding_txid (str): The funding transaction ID of the opened channel.

    Returns:
        bool: True if confirmation was successful, False otherwise.
    """

    try:
        channel_point = get_channel_point(funding_txid)  # Get channel point within this function
        if not channel_point:
            raise LNDError(f"Could not find channel point for funding transaction: {funding_txid}")

        query = """
            mutation Mutation($sellerAddTransactionId: String!, $transaction: String!) {
              sellerAddTransaction(id: $sellerAddTransactionId, transaction: $transaction)
            }
        """
        variables = {
            "sellerAddTransactionId": order_id,
            "transaction": channel_point,
        }

        response = requests.post(AMBOSS_API_URL, json={"query": query, "variables": variables}, headers=AMBOSS_API_HEADERS)
        response.raise_for_status()

        result = response.json()
        if result.get('data', {}).get('sellerAddTransaction'):
            main_logger.info(f"Channel point confirmed for order {order_id}: {channel_point}")
            bot.send_message(CHAT_ID, text=f"Channel point confirmed for order {order_id}: {channel_point}")
            return True
        else:
            error_message = f"Failed to confirm channel point for order {order_id}."
            if 'errors' in result:
                error_message += f" Error details: {result['errors']}"
            error_logger.error(error_message)
            bot.send_message(CHAT_ID, text=error_message)
            return False

    except (requests.exceptions.RequestException, LNDError) as e:
        error_logger.error("Error confirming channel point: %s", e)
        return False


if __name__ == "__main__":
    # target_statuses = ["WAITING_FOR_SELLER_APPROVAL", "WAITING_FOR_CHANNEL_OPEN"]
    target_statuses = ["SELLER_REJECTED"]
    poll_interval = 5.0  # Initial polling interval

    if os.path.exists(critical_error_log_path):
        error_logger.error("Critical error flag file exists. Script will not run.")
        sys.exit(1) 

    # Use polling2 library's poll function for better control and error handling
    poll_fn = lambda: monitor_sell_requests(target_statuses) 
    while True:
        try:
            order, extensions = polling2.poll(
                poll_fn,
                check_success=lambda x: x[0] is not None,
                step=poll_interval,
                poll_forever=True
            )
            main_logger.info(f"Order found: {order}")
            main_logger.info(f"Fee rate cap: {order.get('locked_fee_rate_cap')}")

            if extensions is not None:
                main_logger.info(f"Extensions: {extensions}")
                main_logger.info(f"Currently available credits: {extensions.get('cost', {}).get('throttleStatus', {}).get('currentlyAvailable')}")

            if order is not None:
                try:
                    match order['status']:
                        case "WAITING_FOR_SELLER_APPROVAL":
                            main_logger.info("Found an order waiting for seller approval.")
                            # Add logic to decide whether to approve or reject the order
                            # ... (approval/rejection logic)

                        case "WAITING_FOR_BUYER_PAYMENT":
                            main_logger.info("Found an order waiting for buyer payment.")
                            payment_hash, invoice_request = create_lightning_invoice(
                                order['seller_invoice_amount'],
                                f"Magma-Channel-Sale-Order-ID:{order['id']}",
                                INVOICE_EXPIRY_SECONDS,
                            )
                            if invoice_request:
                                accept_result = accept_order(order['id'], invoice_request)
                                if accept_result:
                                    bot.send_message(CHAT_ID, text=f"Order {order['id']} accepted. Invoice:\n{invoice_request}\nWaiting for buyer to pay...")
                                else:
                                    bot.send_message(CHAT_ID, text=f"Failed to accept order {order['id']}. Check logs for details.")
                            else:
                                bot.send_message(CHAT_ID, text=f"Failed to create invoice for order {order['id']}. Check logs for details.")

                        case "WAITING_FOR_CHANNEL_OPEN":
                            main_logger.info("Found a pending channel opening request.")

                            for retry_count in range(MAX_CONNECTION_RETRIES):
                                connection_success = get_buyer_details_and_connect(order)
                                if connection_success:
                                    fee_rate = check_mempool_fees_and_profitability(order)
                                    if fee_rate:
                                        utxos_needed, fee_cost, selected_utxos = calculate_utxos_required_and_fees(order["size"], fee_rate)
                                        if utxos_needed != -1:
                                            try:
                                                funding_tx = open_channel(
                                                    order["endpoints"]["destination"],
                                                    order["size"],
                                                    fee_rate,
                                                    selected_utxos,
                                                )
                                                if funding_tx:
                                                    if confirm_channel_point_to_amboss(order['id'], funding_tx):
                                                        bot.send_message(CHAT_ID, text=f"Channel opened and confirmed for order {order['id']}. Funding tx: {funding_tx}")
                                                    else:
                                                        # Retry confirmation until timeout?
                                                        pass 
                                                else:
                                                    bot.send_message(CHAT_ID, text=f"Failed to open channel for order {order['id']}. Check logs for details.")
                                            except LNDError as e:
                                                handle_critical_error(e)
                                            break  
                                    else:
                                        bot.send_message(CHAT_ID, text=f"Insufficient funds for order {order['id']}.")
                                        break
                                    
                                else:
                                    if retry_count < MAX_CONNECTION_RETRIES - 1:
                                        time.sleep(RETRY_DELAY_SECONDS)
                                    else:
                                        bot.send_message(CHAT_ID, text=f"Failed to connect to buyer for order {order['id']} after {MAX_CONNECTION_RETRIES} attempts.")
                                        break
                        
                        # ... (other cases for error states)
                except (AmbossAPIError, LNDError, ValueError) as e:
                    # Log and send Telegram message for any exceptions
                    error_message = f"Error processing order {order.get('id', 'Unknown')}: {e}"
                    error_logger.error(error_message)
                    bot.send_message(CHAT_ID, text=error_message)

            # Adjust poll interval based on throttle status
            if extensions is not None:
                poll_interval = adjust_poll_interval(extensions, poll_interval)

        except polling2.TimeoutException as e:
            error_logger.warning("Polling timeout: %s", e)

        except AmbossAPIError as e:  # Catch Amboss API errors
            error_logger.error("Amboss API error: %s", e)
            
            # Check if the Amboss API error is critical
            if e.status_code == 500:  # Example critical status code
                handle_critical_error("Amboss API is down")
            
            poll_interval = 60  # Increase polling interval on error
            
        except LNDError as e:  # Catch LND errors
            handle_critical_error(e)
            
        except Exception as e:  # Catch all other unexpected exceptions
            handle_critical_error(e)
