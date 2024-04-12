# This is a refactored code of https://github.com/jvxis/nr-tools
# Working on combining and simplifying a few code elements to allow
# for faster turnaround times and more dynamic message adjustments.
# Improving logging too.
# enter the necessary settings in config.ini file in the parent dir

import requests
import telebot
import json
from telebot import types
import subprocess
import time
import os
import schedule
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
MEMPOOL_API_URL = 'https://mempool.space/api/v1/fees/recommended'

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

def handle_critical_error(error_message):
    """Handles critical errors, logging, sending Telegram message, and creating a flag file."""

    error_logger.critical(error_message)  # Log as critical
    bot.send_message(CHAT_ID, text=f"Critical error: {error_message}. Manual intervention required. Check logs.")
    with open(critical_error_log_path, 'w') as f:
        f.write(error_message)  # Create the flag file

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

UTXO_INPUT_SIZE = 57.5  # vBytes (approximate)
OUTPUT_SIZE = 43  # vBytes (approximate)
TRANSACTION_OVERHEAD = 10.5  # vBytes (approximate)

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

def calculate_utxos_required_and_fees(target_amount, fee_per_vbyte):
    utxos_data = sorted(get_and_calculate_utxos(), key=lambda x: x["amount_sat"], reverse=True)  # Sort by amount descending
    total_available = sum(utxo["amount_sat"] for utxo in utxos_data)

    if total_available < target_amount:
        error_message = f"Insufficient UTXOs: Need {target_amount} sats, have {total_available} sats"
        handle_critical_error(error_message)  # Call the error handler
        return -1, 0, None

    utxos_needed = 0
    accumulated_amount = 0
    selected_outpoints = []
    fee_cost = 0

    for utxo in utxos_data:
        utxos_needed += 1
        accumulated_amount += utxo["amount_sat"]
        selected_outpoints.append(utxo["outpoint"])

        tx_size = calculate_transaction_size(utxos_needed)
        fee_cost = tx_size * fee_per_vbyte

        if accumulated_amount >= target_amount + fee_cost:
            break

    return utxos_needed, fee_cost, selected_outpoints


def check_mempool_fees_and_profitability(order, fee_rate)
    response = requests.get(MEMPOOL_API_URL)
    data = response.json()
    if data:
        fast_fee = data['fastestFee']
        return fast_fee
    else:
        return None
    

def get_buyer_details_and_connect(pubkey)
    

def open_channel(pubkey, amount, fee_rate, outpoints)
    

def confirm_channel_point(order_id, channel_point)
    



if __name__ == "__main__":
    # target_statuses = ["WAITING_FOR_SELLER_APPROVAL", "WAITING_FOR_CHANNEL_OPEN"]
    target_statuses = ["SELLER_REJECTED"]
    poll_interval = 5.0  # Initial polling interval

    while True:
        order, extensions = monitor_sell_requests(target_statuses)

        if order is not None:
            print(f"Order found: {order}")  # Print order details
            print(f"Fee rate cap: {order.get('locked_fee_rate_cap')}")  # Print fee rate cap

        if extensions is not None:
            print(f"Extensions: {extensions}")  # Print extensions data
            print(f"Currently available credits: {extensions.get('cost', {}).get('throttleStatus', {}).get('currentlyAvailable')}")

        if order is not None:
            # Handle matching order
            match order['status']:
                case "WAITING_FOR_SELLER_APPROVAL":
                    # Process order for approval
                    print("Found a pending buying request")
                case "WAITING_FOR_CHANNEL_OPEN":
                    # Process order for channel opening
                    print("Found a pending channel opening request")
                    utxos = get_and_calculate_utxos()  # Get UTXOs
                case "SELLER_REJECTED":
                    # Test routine of Invoice and Accept Order
                    print("Running the Test Environment")
                    try:
                        payment_hash, invoice_request = create_lightning_invoice(
                            order['seller_invoice_amount'],
                            f"Magma-Channel-Sale-Order-ID:{order['id']}",
                            INVOICE_EXPIRY_SECONDS,
                        )
                        if invoice_request is not None:
                            accept_result = accept_order(order['id'], invoice_request)

                            print(f"Invoice request: {invoice_request}")  # Print for verification
                            print(f"Accept result: {accept_result}")  # Print for verification

                    except (AmbossAPIError, LNDError) as e:
                        error_logger.error("Error processing order %s: %s", order['id'], e)
                case _:
                    # Handle unexpected order status
                    error_logger.warning("Unexpected order status: %s", order['status'])
        else:
            # No matching order found (or API error)
            main_logger.info("No matching order found.")

        # Adjust poll_interval based on throttleStatus (if extensions are available)
        if extensions is not None:
            poll_interval = adjust_poll_interval(extensions, poll_interval)

        time.sleep(poll_interval)  # Wait before the next poll
