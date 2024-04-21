#!/usr/bin/env python3

# This script monitors the mempool fees and adjusts the LNDg AR-Enabled setting accordingly.
# When the mempool fees are high, the script will disable AR to prevent the node from getting stuck with unconfirmed transactions.
# When the mempool fees are low, the script will enable AR to allow the node to take advantage of the lower fees.

'''
To create a systemd service for the script, create a file with the following contents:

[Unit]
Description=Mempool Rebalancer Trigger
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /path/to/mempool_rebalancer_trigger.py
Restart=on-failure

[Install]
WantedBy=multi-user.target

Save the file with a .service extension (e.g., mempool_rebalancer_trigger.service) and copy it to the /etc/systemd/system directory. Then, run the following commands:

sudo systemctl daemon-reload
sudo systemctl enable mempool_rebalancer_trigger.service
sudo systemctl start mempool_rebalancer_trigger.service
'''

import requests
import logging
import time
import datetime
import os
import configparser


# Get the path to the parent directory
parent_dir = os.path.dirname(os.path.abspath(__file__))

# path to the config.ini file located in the parent directory
config_file_path = os.path.join(parent_dir, '..', 'config.ini')
config = configparser.ConfigParser()
config.read(config_file_path)

# Get the current timestamp
def get_current_timestamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# Variables
MEMPOOL_API_URL = 'https://mempool.space/api/v1/fees/recommended'
AR_ENABLED_API_URL = 'http://localhost:8889/api/settings/AR-Enabled/?format=api'
MEMPOOL_FEE_THRESHOLD = 150  # Adjust this value as needed

# LNDg API credentials and endpoints. Retrievable from lndg/data/lndg-admin.txt
username = config['credentials']['lndg_username']
password = config['credentials']['lndg_password']

# Logfile definition
log_file_path = os.path.join(parent_dir, '..', 'logs', 'mempool_rebalancer_trigger.log')
logging.basicConfig(filename=log_file_path, level=logging.DEBUG) 

# Error classes
class MempoolAPIError(Exception):
    """Represents an error when interacting with the Mempool API."""

    def __init__(self, message, status_code=None, response_data=None):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data

class LNDGAPIError(Exception):
    """Represents an error when interacting with the LNDg API."""

    def __init__(self, message, status_code=None, response_data=None):
        super().__init__(message)
        self.status_code = status_code
        self.response_data = response_data


def check_mempool_fees():
    """Checks the mempool API and adjusts LNDg AR-Enabled setting if necessary."""

    try:
        response = requests.get(MEMPOOL_API_URL)
        response.raise_for_status()  # Raise exception for HTTP errors

        data = response.json()
        if data:
            half_hour_fee = data['halfHourFee']
            logging.info(f"{get_current_timestamp()}: [MEMPOOL] Half-hour mempool fee: {half_hour_fee}")
            if int(half_hour_fee) > MEMPOOL_FEE_THRESHOLD:
                return False
            else:
                return True
        else:
            return None

    except requests.exceptions.RequestException as e:
        raise MempoolAPIError("Mempool API unavailable") from e
    return None


def adjust_ar_enabled(ar_enabled):
    timestamp = get_current_timestamp()
    # Convert the boolean ar_enabled to "1" for True or "0" for False
    ar_enabled_str = "1" if ar_enabled else "0"

    try:
        response = requests.put(AR_ENABLED_API_URL, json={"value": ar_enabled_str}, auth=(username, password))
        
        if response.status_code == 200:
            logging.info(f"{timestamp}: AR-Enabled setting adjusted to {ar_enabled_str}")
            print(f"{timestamp}: AR-Enabled setting adjusted to {ar_enabled_str}")
        else:
            logging.error(f"{timestamp}: Failed to adjust AR-Enabled setting to {ar_enabled_str}: Status Code {response.status_code}")
            print(f"{timestamp}: Failed to adjust AR-Enabled setting to {ar_enabled_str}: Status Code {response.status_code}")

    except requests.exceptions.RequestException as e:
        logging.error(f"{timestamp}: LNDg API request failed: {e}")
        print(f"{timestamp}: LNDg API request failed: {e}")
        raise LNDGAPIError("LNDg API unavailable") from e


if __name__ == "__main__":
    try:
        while True:
            # Check mempool fees
            mempool_fees_ok = check_mempool_fees()

            # Adjust AR-Enabled setting if necessary
            if mempool_fees_ok is not None:
                if mempool_fees_ok:
                    ar_enabled = True
                    print("Mempool fees OK")
                else:
                    ar_enabled = False
                    print("Mempool fees Not OK")
                adjust_ar_enabled(ar_enabled)

                # Log the change
                timestamp = get_current_timestamp()
                log_message = f"{timestamp}: AR-Enabled setting adjusted to {ar_enabled}"
                
                logging.info(log_message)

            # Wait before the next check
            time.sleep(1800)  # Wait 1/2 hour

    except KeyboardInterrupt:
        print("Exiting...")