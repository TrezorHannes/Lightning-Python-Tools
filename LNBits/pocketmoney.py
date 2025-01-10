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
FIAT_AMOUNT = 10
THRESHOLD_BALANCE = 1000000

ADMIN_KEY = config['LNBits']['admin_key']
INVOICE_KEY = config['LNBits']['invoice_key']
BASE_URL = 'http://localhost:5000/api/v1'
TOKEN = config['telegram']['magma_bot_token']
CHAT_ID = config['telegram']['telegram_user_id']

# Parse the children wallets from the config file
CHILDREN_WALLETS = json.loads(config['LNBits']['wallets'])

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
        response = requests.get(f"{BASE_URL}/rate/EUR")
        response.raise_for_status()
        return response.json().get('rate')
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching exchange rate: {e}")
        bot.send_message(CHAT_ID, f"Error fetching exchange rate: {e}")
        return None
    
def schedule_transfers():
    if RECURRENCE == 'daily':
        schedule.every().day.at(TIME).do(scheduled_transfer)
    elif RECURRENCE == 'weekly':
        schedule.every().week.at(TIME).do(scheduled_transfer).tag(DAY_OF_WEEK)
    elif RECURRENCE == 'monthly':
        # schedule.every() doesn't have a month attribute, so we need to use a different approach
        schedule.every(30).days.at(TIME).do(scheduled_transfer).tag(DAY_OF_MONTH)
    else:
        raise ValueError("Invalid recurrence setting. Choose from 'daily', 'weekly', or 'monthly'.")

# Function to create an invoice
def create_invoice(wallet_id, invoice_key, amount_eur):
    amount_sats = int(amount_eur * get_exchange_rate())
    try:
        response = requests.post(
            f"{BASE_URL}/payments",
            headers={"X-Api-Key": invoice_key},
            json={"out": False, "amount": amount_sats, "memo": "Pocket Money"}
        )
        response.raise_for_status()
        invoice_data = response.json()
        return invoice_data.get('payment_request')
    except requests.exceptions.RequestException as e:
        logger.error(f"Error creating invoice for wallet {wallet_id}: {e}")
        bot.send_message(CHAT_ID, f"Error creating invoice for wallet {wallet_id}: {e}")
        return None

# Function to pay an invoice
def pay_invoice(payment_request):
    try:
        response = requests.post(
            f"{BASE_URL}/payments",
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
        response = requests.get(f"{BASE_URL}/wallet", headers={"X-Api-Key": ADMIN_KEY})
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
        payment_request = create_invoice(wallet_id, invoice_key, 10)  # Example amount in EUR
        if payment_request:
            pay_invoice(payment_request)
            bot.send_message(CHAT_ID, f"Transferred pocket money to {child}")

# Call the function to set up the schedule
schedule_transfers()

# Run the scheduler
while True:
    schedule.run_pending()
    time.sleep(60)