import requests
import json
import configparser
import schedule
import time
import os
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
from telebot import TeleBot

# Get the path to the parent directory
parent_dir = os.path.dirname(os.path.abspath(__file__))

# path to the config.ini file located in the parent directory
config_file_path = os.path.join(parent_dir, '..', 'config.ini')
config = configparser.ConfigParser()
config.read(config_file_path)

# Parse scheduling preferences
RECURRENCE = config['Schedule']['recurrence']
TIME = config['Schedule']['time']
DAY_OF_WEEK = config['Schedule'].get('day_of_week', 'friday')
DAY_OF_MONTH = config['Schedule'].get('day_of_month', '1')

FIAT_CURRENCY = 'EUR'
THRESHOLD_BALANCE = 1000000

ADMIN_KEY = config['LNBits']['admin_key']
LNBITS_URL = config['LNBits']['base_url']
TOKEN = config['telegram']['lnbits_bot_token']
CHAT_ID = config['telegram']['telegram_user_id']

# Parse the children wallets from the jason file
wallets_file_path = os.path.join(parent_dir, '..', 'wallets.json')
with open(wallets_file_path, 'r') as f:
    CHILDREN_WALLETS = json.load(f)

# Initialize Telegram bot
bot = TeleBot(TOKEN)

# Set up logging
log_file_path = os.path.join(parent_dir, '..', 'logs', 'pocketmoney.log')
# Set up a rotating file handler
handler = RotatingFileHandler(
    log_file_path, 
    maxBytes=10*1024*1024,  # 10 MB
    backupCount=5
)

# Set up logging configuration
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[handler]
)

# Adjust logging levels for third-party libraries
logging.getLogger('requests').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Function to get the current exchange rate
def get_exchange_rate():
    try:
        url = f"{LNBITS_URL}/rate/{FIAT_CURRENCY}"
        logger.debug(f"Requesting exchange rate from: {url}")
        response = requests.get(url, headers={'accept': 'application/json'})
        logger.debug(f"Response status code: {response.status_code}")
        response.raise_for_status()
        rate = response.json().get('rate')
        logger.debug(f"Exchange rate received: {rate}")
        return rate
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching exchange rate: {e}")
        bot.send_message(CHAT_ID, f"Error fetching exchange rate: {e}")
        return None
    
# Comment out the scheduling logic for testing
def schedule_transfers():
     if RECURRENCE == 'daily':
         schedule.every().day.at(TIME).do(scheduled_transfer)
     elif RECURRENCE == 'weekly':
         schedule.every().week.at(TIME).do(scheduled_transfer).tag(DAY_OF_WEEK)
     else:
         raise ValueError("Invalid recurrence setting. Choose from 'daily', 'weekly', or 'monthly'.")


# Function to create an invoice
def create_invoice(wallet_id, invoice_key, amount_eur):
    exchange_rate = get_exchange_rate()
    if exchange_rate is None:
        logger.error("Failed to retrieve exchange rate.")
        bot.send_message(CHAT_ID, "Failed to retrieve exchange rate.")
        return None, None

    amount_sats = int(amount_eur * exchange_rate)
    try:
        response = requests.post(
            f"{LNBITS_URL}/payments",
            headers={"X-Api-Key": invoice_key},
            json={"out": False, "amount": amount_sats, "memo": "Pocket Money"}
        )
        response.raise_for_status()
        invoice_data = response.json()
        return invoice_data.get('payment_request'), amount_sats
    except requests.exceptions.RequestException as e:
        logger.error(f"Error creating invoice for wallet {wallet_id}: {e}")
        bot.send_message(CHAT_ID, f"Error creating invoice for wallet {wallet_id}: {e}")
        return None, None

# Function to pay an invoice
def pay_invoice(payment_request):
    try:
        response = requests.post(
            f"{LNBITS_URL}/payments",
            headers={"X-Api-Key": ADMIN_KEY},
            json={"out": True, "bolt11": payment_request}
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Error paying invoice: {e}")
        bot.send_message(CHAT_ID, f"Error paying invoice: {e}")
        return None

# Function to check wallet balance
def check_balance():
    try:
        response = requests.get(f"{LNBITS_URL}/wallet", headers={"X-Api-Key": ADMIN_KEY})
        response.raise_for_status()
        balance = response.json().get('balance')
        if balance < THRESHOLD_BALANCE:  # Example threshold for low balance warning
            send_telegram_warning(balance)
        return balance
    except requests.exceptions.RequestException as e:
        logger.error(f"Error checking balance: {e}")
        bot.send_message(CHAT_ID, f"Error checking balance: {e}")
        return None

# Function to send a warning if balance is low
def send_telegram_warning(balance):
    message = f"Warning: Parent wallet balance is low: {balance} sats"
    bot.send_message(CHAT_ID, message)

# Main function to run the scheduled transfer
def scheduled_transfer():
    for child, wallet_info in CHILDREN_WALLETS.items():
        wallet_id = wallet_info['wallet_id']
        invoice_key = wallet_info['invoice_key']
        fiat_amount = wallet_info['fiat_amount']
        
        payment_request, amount_sats = create_invoice(wallet_id, invoice_key, fiat_amount)
        if payment_request:
            pay_invoice(payment_request)
            bot.send_message(
                CHAT_ID, 
                f"💶 🤑 Transferred {fiat_amount} {FIAT_CURRENCY} ({amount_sats} satoshis) worth of pocket money to {child}"
            )

# Call the function to set up the schedule
schedule_transfers()

# Run the scheduler (commented out for testing)
while True:
    schedule.run_pending()
    time.sleep(60)

# Call the function directly for testing
# if __name__ == "__main__":
#     scheduled_transfer()