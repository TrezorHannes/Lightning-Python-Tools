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
import argparse

# Get the path to the parent directory
parent_dir = os.path.dirname(os.path.abspath(__file__))

# path to the config.ini file located in the parent directory
config_file_path = os.path.join(parent_dir, "..", "config.ini")
config = configparser.ConfigParser()
config.read(config_file_path)

# Parse scheduling preferences
recurrence_raw = config["Schedule"]["recurrence"]
RECURRENCE = recurrence_raw.split("#")[0].strip().lower()

# Verify RECURRENCE
if RECURRENCE not in ["daily", "weekly"]:
    raise ValueError("Invalid recurrence setting. Choose from 'daily' or 'weekly'.")

TIME = config["Schedule"]["time"].strip()
DAY_OF_WEEK = config["Schedule"].get("day_of_week", "friday").strip()

# Check if TIME is valid
try:
    datetime.strptime(TIME, "%H:%M")
except ValueError:
    raise ValueError("Invalid time format. Use HH:MM (e.g., 09:00).")

# Other configuration variables
ADMIN_KEY = config["LNBits"]["admin_key"]
LNBITS_URL = config["LNBits"]["base_url"]
TOKEN = config["telegram"]["lnbits_bot_token"]
CHAT_ID = config["telegram"]["telegram_user_id"]
THRESHOLD_BALANCE = 1000000

# Initialize Telegram bot
bot = TeleBot(TOKEN)

# Set up logging
log_file_path = os.path.join(os.path.dirname(__file__), "..", "logs", "pocketmoney.log")
handler = RotatingFileHandler(log_file_path, maxBytes=10 * 1024 * 1024, backupCount=5)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[handler],
)
logger = logging.getLogger(__name__)

# Adjust logging levels for third-party libraries
logging.getLogger("requests").setLevel(logging.WARNING)


# Function to get the current exchange rate
def get_exchange_rate(fiat_currency):
    try:
        url = f"{LNBITS_URL}/rate/{fiat_currency}"
        logger.debug(f"Requesting exchange rate from: {url}")
        response = requests.get(url, headers={"accept": "application/json"})
        logger.debug(f"Response status code: {response.status_code}")
        response.raise_for_status()
        rate = response.json().get("rate")
        logger.debug(f"Exchange rate received: {rate}")
        return rate
    except requests.exceptions.RequestException as e:
        logger.error(f"Error fetching exchange rate: {e}")
        bot.send_message(CHAT_ID, f"Error fetching exchange rate: {e}")
        return None


# Function to create an invoice
def create_invoice(wallet_id, invoice_key, amount_eur, child_name, fiat_currency):
    exchange_rate = get_exchange_rate(fiat_currency)
    if exchange_rate is None:
        logger.error("Failed to retrieve exchange rate.")
        bot.send_message(CHAT_ID, "Failed to retrieve exchange rate.")
        return None, None

    amount_sats = int(amount_eur * exchange_rate)
    memo = f"Pocket Money for {child_name} - {RECURRENCE.capitalize()}"
    try:
        response = requests.post(
            f"{LNBITS_URL}/payments",
            headers={"X-Api-Key": invoice_key},
            json={"out": False, "amount": amount_sats, "memo": memo},
        )
        response.raise_for_status()
        invoice_data = response.json()
        return invoice_data.get("payment_request"), amount_sats
    except requests.exceptions.RequestException as e:
        logger.error(
            f"Error creating invoice for {child_name} with wallet {wallet_id}: {e}"
        )
        bot.send_message(
            CHAT_ID,
            f"Error creating invoice for {child_name} with wallet {wallet_id}: {e}",
        )
        return None, None


# Function to pay an invoice
def pay_invoice(payment_request):
    try:
        response = requests.post(
            f"{LNBITS_URL}/payments",
            headers={"X-Api-Key": ADMIN_KEY},
            json={"out": True, "bolt11": payment_request},
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
        response = requests.get(
            f"{LNBITS_URL}/wallet", headers={"X-Api-Key": ADMIN_KEY}
        )
        response.raise_for_status()
        balance = response.json().get("balance")
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
    # Parse the children wallets from the jason file
    wallets_file_path = os.path.join(parent_dir, "..", "wallets.json")
    with open(wallets_file_path, "r") as f:
        CHILDREN_WALLETS = json.load(f)

    for child, wallet_info in CHILDREN_WALLETS.items():
        wallet_id = wallet_info["wallet_id"]
        invoice_key = wallet_info["invoice_key"]
        fiat_amount = wallet_info["fiat_amount"]
        fiat_currency = wallet_info["fiat_currency"]

        payment_request, amount_sats = create_invoice(
            wallet_id, invoice_key, fiat_amount, child, fiat_currency
        )
        if payment_request:
            pay_invoice(payment_request)
            logger.info(
                f"Transferred {fiat_amount} {fiat_currency} ({amount_sats} satoshis) worth of pocket money to {child}"
            )
            bot.send_message(
                CHAT_ID,
                f"💶 🤑 Transferred {fiat_amount} {fiat_currency} ({amount_sats} satoshis) worth of pocket money to {child}",
            )
            # Check balance after each payment
            check_balance()


# Comment out the scheduling logic for testing
def schedule_transfers():
    if RECURRENCE == "daily":
        schedule.every().day.at(TIME).do(scheduled_transfer)
    elif RECURRENCE == "weekly":
        days = {
            "monday": schedule.every().monday,
            "tuesday": schedule.every().tuesday,
            "wednesday": schedule.every().wednesday,
            "thursday": schedule.every().thursday,
            "friday": schedule.every().friday,
            "saturday": schedule.every().saturday,
            "sunday": schedule.every().sunday,
        }
        try:
            days[DAY_OF_WEEK].at(TIME).do(scheduled_transfer)
        except KeyError:
            raise ValueError(
                "Invalid day of week. Choose from 'monday', 'tuesday', etc."
            )
    else:
        raise ValueError("Invalid recurrence setting. Choose from 'daily' or 'weekly'.")


def main():
    parser = argparse.ArgumentParser(description="Pocket Money Scheduler")
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Run the transfer once and exit, best for cronjobs",
    )
    args = parser.parse_args()

    if args.run_once:
        scheduled_transfer()
    else:
        schedule_transfers()
        while True:
            schedule.run_pending()
            time.sleep(60)


if __name__ == "__main__":
    main()
