# Magma Channel Auto-Pricing Script (Lightning Network)
#
# Purpose:
# This script automates the process of pricing and managing Lightning Network channel selling
# offers on Amboss Magma. It queries current market offers, analyzes them to determine
# competitive pricing points, and then creates or updates the user's own sell offers.
# The pricing strategy aims to be competitive (e.g., top 10th percentile) without necessarily
# being the absolute cheapest, considering the quality of the node.
#
# Key Features:
# - Reads general configuration from `../config.ini` and Magma-specific settings from `Magma/magma_config.ini`.
# - Periodically fetches public Amboss Magma sell offers.
# - Analyzes market offers based on fixed fees, PPM rates, and potentially APR.
# - Calculates competitive pricing for the user's own pre-defined offer templates.
# - Supports managing multiple concurrent sell offers with different parameters.
# - Optionally queries LND for available on-chain balance to limit capital committed to sales.
# - Updates existing user offers or creates new ones on Amboss Magma.
# - Sends Telegram notifications summarizing pricing changes or actions taken.
# - Provides detailed logging to `logs/magma-market-fee.log`.
# - Includes a --dry-run mode to simulate actions without making live API changes.
#
# How to Run:
# 1. Ensure Python 3 is installed.
# 2. Install required Python packages: `pip install requests schedule python-telegram-bot configparser`
# 3. Ensure `../config.ini` exists and is configured (see config.ini.example).
# 4. Create `Magma/magma_config.ini` for Magma-specific settings (see example in script comments or docs).
# 5. Make the script executable: `chmod +x magma_market_fee.py`
# 6. Run the script: `python /path/to/Magma/magma_market_fee.py`
#    For dry run: `python /path/to/Magma/magma_market_fee.py --dry-run`
#    It's designed to be run periodically, e.g., by a systemd timer or cron job.

import argparse
import configparser
import datetime
import json
import logging
import os
import requests
import subprocess
import time
# Consider if telebot is needed or if you'll use python-telegram-bot
# For simplicity, assuming a similar send_telegram_notification structure
# import telebot # If reusing from magma_sale_process.py

# --- Global Configuration ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(SCRIPT_DIR)
GENERAL_CONFIG_FILE_PATH = os.path.join(PARENT_DIR, "config.ini")
MAGMA_CONFIG_FILE_PATH = os.path.join(SCRIPT_DIR, "magma_config.ini") # Specific to Magma settings
LOG_DIR = os.path.join(PARENT_DIR, "logs")
LOG_FILE_PATH = os.path.join(LOG_DIR, "magma-market-fee.log")
BLOCKS_PER_DAY = 144

# Parsers for different config files
general_config = configparser.ConfigParser()
magma_specific_config = configparser.ConfigParser()

# Global Amboss token and Telegram bot token/chat ID (loaded in main)
AMBOSS_TOKEN = None
TELEGRAM_BOT_TOKEN = None
TELEGRAM_CHAT_ID = None
LNCLI_PATH = "lncli" # Default, can be overridden by config
DRY_RUN_MODE = False


# --- GraphQL Queries/Mutations ---
GET_PUBLIC_MAGMA_OFFERS_QUERY = """
query ListMarketOffers($filter: MarketOfferFilterInput, $limit: Int, $nextToken: String, $sort: MarketOfferSortInput) {
  listMarketOffers(filter: $filter, limit: $limit, next_token: $nextToken, sort: $sort) {
    offers {
      offer_id
      apr_percent
      base_fee  # Fixed fee in sats
      fee_rate  # PPM rate
      min_channel_size
      max_channel_size
      min_channel_duration # Duration in blocks
      node_details {
        pubkey
        alias
      }
      # status # if available and needed for filtering
    }
    # next_token # For pagination if used
  }
}
"""

GET_MY_MAGMA_OFFERS_QUERY = """
query GetUserOffers {
  getUserOffers {
    list {
      id # This is the UserOffer ID, use this for update/delete
      status
      type
      offer_details { # This is of type Offer
        base_fee
        base_fee_cap
        fee_rate
        fee_rate_cap
        max_size
        min_block_length # Duration in blocks
        min_size
        total_size # Total liquidity backing this offer definition
        node_public_key # Should be our pubkey
        created_at
        updated_at
      }
    }
  }
}
"""

CREATE_MAGMA_OFFER_MUTATION = """
mutation CreateOffer($input: CreateOffer!) {
  createOffer(input: $input) # Returns the new Offer ID (String)
}
"""

UPDATE_MAGMA_OFFER_MUTATION = """
mutation UpdateOfferDetails($id: String!, $input: UpdateOfferDetailsInput!) {
  updateOfferDetails(id: $id, input: $input) {
    # Returns Offer object upon success
    base_fee
    fee_rate
    min_block_length
    min_size
    max_size
    total_size
  }
}
"""

DELETE_MAGMA_OFFER_MUTATION = """
mutation DeleteOffer($id: String!) {
  deleteOffer(id: $id) # Returns Boolean
}
"""


# --- Logging Setup ---
def setup_logging():
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR, exist_ok=True)
    
    # Log level from general config
    log_level_str = general_config.get("system", "log_level", fallback="INFO").upper()
    numeric_level = getattr(logging, log_level_str, logging.INFO)

    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s",
        handlers=[
            logging.handlers.RotatingFileHandler(
                LOG_FILE_PATH, maxBytes=10 * 1024 * 1024, backupCount=5
            ),
            logging.StreamHandler() # Also print to console
        ],
    )
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

# --- Telegram Notification ---
def send_telegram_notification(text, level="info"):
    """Sends a message to Telegram and logs it."""
    log_message = f"Telegram NOTIFICATION: {text}"
    if level == "error":
        logging.error(log_message)
    elif level == "warning":
        logging.warning(log_message)
    else:
        logging.info(log_message)

    if DRY_RUN_MODE:
        logging.info(f"DRY RUN: Would send Telegram notification: {text}")
        return

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram token or chat ID not configured. Skipping notification.")
        return

    try:
        from telegram import Bot
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode='Markdown')
    except ImportError:
        logging.error("python-telegram-bot library is not installed. Telegram notification failed.")
    except Exception as e:
        logging.error(f"Failed to send Telegram message: {e}")


# --- Amboss API Interaction ---
def _execute_amboss_graphql_request(payload: dict, operation_name: str = "AmbossGraphQL"):
    """
    Executes a GraphQL request to the Amboss API.
    """
    if not AMBOSS_TOKEN:
        logging.error("Amboss API token not configured.")
        return None

    # Check for DRY_RUN_MODE for mutations before making the call
    # This is a general check; specific functions will also have dry run logic.
    is_mutation = operation_name.lower().startswith(("create", "update", "delete", "toggle"))
    if DRY_RUN_MODE and is_mutation:
        logging.info(f"DRY RUN: Preventing API call for {operation_name}. Payload: {json.dumps(payload, indent=2)}")
        # Simulate a generic success structure for mutations if needed by calling code
        if "Create" in operation_name: return {"createOffer": f"dry-run-id-for-{operation_name}"}
        if "Update" in operation_name: return {"updateOfferDetails": {"id": "dry-run-updated-id", "status": "dry_run_simulated_update"}}
        if "Delete" in operation_name: return {"deleteOffer": True}
        return {"dryRunSimulatedSuccess": True}


    url = "https://api.amboss.space/graphql"
    headers = {
        "content-type": "application/json",
        "Authorization": f"Bearer {AMBOSS_TOKEN}",
    }
    logging.debug(f"Executing {operation_name} with payload: {json.dumps(payload, indent=2 if logging.getLogger().getEffectiveLevel() == logging.DEBUG else None)}")

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        response_json = response.json()
        if response_json.get("errors"):
            logging.error(f"GraphQL errors during {operation_name}: {response_json.get('errors')}")
            return None
        return response_json.get("data")
    except requests.exceptions.Timeout:
        logging.error(f"Timeout during {operation_name} to Amboss.")
        return None
    except requests.exceptions.HTTPError as e:
        logging.error(f"HTTP error during {operation_name} to Amboss: {e}. Response: {e.response.text}")
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

# --- LND Interaction ---
def get_lnd_onchain_balance():
    """Queries LND for its confirmed on-chain wallet balance."""
    global LNCLI_PATH # Reads from general_config
    try:
        LNCLI_PATH = general_config.get("paths", "lncli_path", fallback="lncli")
        command = [LNCLI_PATH, "walletbalance"]
        # In DRY_RUN_MODE, we might want to simulate this or use a fixed value.
        if DRY_RUN_MODE:
            logging.info("DRY RUN: Simulating LND wallet balance check. Returning 10,000,000 sats.")
            return 10000000 
        
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        output_json = json.loads(result.stdout)
        confirmed_balance = int(output_json.get("confirmed_balance", 0))
        logging.info(f"LND confirmed on-chain balance: {confirmed_balance} sats")
        return confirmed_balance
    except FileNotFoundError:
        logging.error(f"lncli command not found at '{LNCLI_PATH}'. Please check general config.ini [paths] lncli_path.")
        return 0
    except subprocess.CalledProcessError as e:
        logging.error(f"lncli walletbalance error: {e.stderr}")
        return 0
    except json.JSONDecodeError as e:
        logging.error(f"Error decoding lncli walletbalance JSON output: {e}")
        return 0
    except Exception as e:
        logging.exception("Unexpected error getting LND on-chain balance:")
        return 0

# --- Market Analysis & Pricing Logic ---
def fetch_public_magma_offers():
    """Fetches public sell offers from Amboss Magma."""
    logging.info("Fetching public Magma sell offers...")
    variables = {"limit": 100} 
    payload = {"query": GET_PUBLIC_MAGMA_OFFERS_QUERY, "variables": variables}
    data = _execute_amboss_graphql_request(payload, "ListMarketOffers") # This is a query, not a mutation
    if data and data.get("listMarketOffers", {}).get("offers"):
        offers = data["listMarketOffers"]["offers"]
        logging.info(f"Fetched {len(offers)} public Magma offers.")
        return offers
    else:
        logging.warning("No public Magma offers found or error in fetching.")
        return []

def calculate_apr(fixed_fee_sats, ppm_fee_rate, channel_size_sats, duration_days_float):
    """Approximate APR calculation. Duration is float for precision."""
    if channel_size_sats == 0 or duration_days_float == 0:
        return 0.0
    variable_fee_sats = (ppm_fee_rate / 1_000_000) * channel_size_sats
    total_fee_sats = fixed_fee_sats + variable_fee_sats
    apr = (total_fee_sats / channel_size_sats) * (365.0 / duration_days_float) * 100
    return round(apr, 2)

def analyze_and_price_offer(market_offers, our_offer_template_config, current_magma_config):
    """
    Analyzes market offers and determines pricing for our offer.
    Uses current_magma_config for [magma_autoprice] settings.
    """
    logging.info(f"Analyzing market for offer template: {our_offer_template_config['name']}")
    
    our_size_template = our_offer_template_config.getint('channel_size_sats')
    our_duration_days_template = our_offer_template_config.getint('duration_days')

    relevant_offers = []
    for offer in market_offers:
        try:
            size = int(offer.get('min_channel_size', 0))
            if offer.get('max_channel_size') != size and offer.get('max_channel_size') is not None:
                 pass 
            duration_blocks = int(offer.get('min_channel_duration', 0))
            fixed_fee = int(offer.get('base_fee', 0))
            ppm_fee = int(offer.get('fee_rate', 0))
            duration_days_market = duration_blocks / BLOCKS_PER_DAY if BLOCKS_PER_DAY > 0 else 0
            size_similarity_threshold = 0.50 
            duration_similarity_threshold = 0.50

            if (abs(size - our_size_template) / our_size_template < size_similarity_threshold if our_size_template > 0 else True) and \
               (abs(duration_days_market - our_duration_days_template) / our_duration_days_template < duration_similarity_threshold if our_duration_days_template > 0 else True) and \
               (fixed_fee >= 0 and ppm_fee >= 0 and size > 0 and duration_blocks > 0):
                apr = offer.get('apr_percent')
                if apr is None and duration_days_market > 0:
                    apr = calculate_apr(fixed_fee, ppm_fee, size, duration_days_market)
                elif apr is None: apr = 0
                relevant_offers.append({
                    "id": offer.get("offer_id"), "size": size, "duration_blocks": duration_blocks,
                    "duration_days": duration_days_market, "base_fee": fixed_fee, "fee_rate": ppm_fee,
                    "apr": apr, "node_alias": offer.get("node_details", {}).get("alias", "N/A")})
        except (ValueError, TypeError, ZeroDivisionError) as e:
            logging.warning(f"Skipping market offer due to data issue or zero division: {offer.get('offer_id', 'N/A')}, Error: {e}")
            continue
            
    if not relevant_offers:
        logging.warning("No relevant market offers found for comparison against template.")
        new_fixed_fee = our_offer_template_config.getint('min_fixed_fee_sats', 0)
        new_ppm_fee = our_offer_template_config.getint('min_ppm_fee', 0)
        global_min_ppm = current_magma_config.getint("magma_autoprice", "global_min_ppm_fee", fallback=0)
        new_ppm_fee = max(new_ppm_fee, global_min_ppm)
        our_fallback_apr = calculate_apr(new_fixed_fee, new_ppm_fee, our_size_template, float(our_duration_days_template))
        logging.info(f"Using fallback pricing for {our_offer_template_config['name']}: Fixed={new_fixed_fee}, PPM={new_ppm_fee}, APR={our_fallback_apr}%")
        return {"channel_size_sats": our_size_template, "duration_days": our_duration_days_template,
                "fixed_fee_sats": new_fixed_fee, "ppm_fee_rate": new_ppm_fee, "calculated_apr": our_fallback_apr}

    relevant_offers.sort(key=lambda x: x['apr'] if x['apr'] is not None else float('inf'))
    logging.debug(f"Relevant sorted offers: {json.dumps(relevant_offers, indent=2)}")

    percentile_target = current_magma_config.getfloat("magma_autoprice", "pricing_strategy_percentile", fallback=10) / 100.0
    target_index = int(len(relevant_offers) * percentile_target)
    target_index = max(0, min(target_index, len(relevant_offers) - 1))
    benchmark_offer = relevant_offers[target_index]
    logging.info(f"Initial benchmark offer ({percentile_target*100:.0f}th percentile): ID {benchmark_offer['id']}, Alias {benchmark_offer['node_alias']}, APR {benchmark_offer['apr']}%")

    position_offset = current_magma_config.getint("magma_autoprice", "pricing_strategy_position_offset", fallback=1)
    final_benchmark_index = min(target_index + position_offset, len(relevant_offers) - 1)
    final_benchmark_offer = relevant_offers[final_benchmark_index]
    logging.info(f"Final benchmark for our pricing (index {final_benchmark_index}): ID {final_benchmark_offer['id']}, Alias {final_benchmark_offer['node_alias']}, APR {final_benchmark_offer['apr']}%")

    new_fixed_fee = final_benchmark_offer['base_fee']
    new_ppm_fee = final_benchmark_offer['fee_rate']
    new_fixed_fee = max(our_offer_template_config.getint('min_fixed_fee_sats', 0),
                        min(our_offer_template_config.getint('max_fixed_fee_sats', 1000000), new_fixed_fee))
    new_ppm_fee = max(our_offer_template_config.getint('min_ppm_fee', 0),
                      min(our_offer_template_config.getint('max_ppm_fee', 10000), new_ppm_fee))
    global_min_ppm = current_magma_config.getint("magma_autoprice", "global_min_ppm_fee", fallback=0)
    new_ppm_fee = max(new_ppm_fee, global_min_ppm)
    our_new_apr = calculate_apr(new_fixed_fee, new_ppm_fee, our_size_template, float(our_duration_days_template))
    target_apr_min = our_offer_template_config.getfloat('target_apr_min', 0)
    target_apr_max = our_offer_template_config.getfloat('target_apr_max', 100)

    if not (target_apr_min <= our_new_apr <= target_apr_max):
        logging.warning(f"Calculated APR {our_new_apr}% for template '{our_offer_template_config['name']}' is outside its target range ({target_apr_min}% - {target_apr_max}%). Offer values: Fixed={new_fixed_fee}, PPM={new_ppm_fee}.")
        
    final_pricing = {"channel_size_sats": our_size_template, "duration_days": our_duration_days_template,
                     "fixed_fee_sats": new_fixed_fee, "ppm_fee_rate": new_ppm_fee, "calculated_apr": our_new_apr}
    logging.info(f"Determined pricing for {our_offer_template_config['name']}: {final_pricing}")
    return final_pricing

# --- Manage Our Offers on Amboss ---
def fetch_my_current_offers():
    logging.info("Fetching my current Magma sell offers...")
    payload = {"query": GET_MY_MAGMA_OFFERS_QUERY}
    data = _execute_amboss_graphql_request(payload, "GetUserOffers") # Query, not mutation

    if data and data.get("getUserOffers", {}).get("list"):
        my_raw_offers = data["getUserOffers"]["list"]
        processed_offers = []
        for offer_item in my_raw_offers:
            if offer_item and 'offer_details' in offer_item and offer_item['offer_details']:
                combined_details = {"id": offer_item.get("id"), "status": offer_item.get("status"), **offer_item["offer_details"]}
                processed_offers.append(combined_details)
            else: logging.warning(f"Found user offer item without full details: {offer_item.get('id')}")
        logging.info(f"Found {len(processed_offers)} existing Magma sell offers with details.")
        return processed_offers
    logging.warning("No existing Magma sell offers found or error fetching.")
    return []

def create_magma_offer(pricing_details, template_capital_for_total_size, template_name):
    duration_blocks = pricing_details["duration_days"] * BLOCKS_PER_DAY
    amboss_offer_input = {
        "base_fee": pricing_details["fixed_fee_sats"], "fee_rate": pricing_details["ppm_fee_rate"],
        "min_size": pricing_details["channel_size_sats"], "max_size": pricing_details["channel_size_sats"],
        "min_block_length": duration_blocks, "total_size": template_capital_for_total_size,
        "base_fee_cap": pricing_details["fixed_fee_sats"], "fee_rate_cap": pricing_details["ppm_fee_rate"],
    }
    log_prefix = "DRY RUN: Would create" if DRY_RUN_MODE else "Creating"
    logging.info(f"{log_prefix} new Magma offer for template '{template_name}' with: {amboss_offer_input}")

    if DRY_RUN_MODE:
        return {"id": f"dry-run-new-{template_name}", "createOffer": f"dry-run-new-{template_name}"}

    payload = {"query": CREATE_MAGMA_OFFER_MUTATION, "variables": {"input": amboss_offer_input}}
    data = _execute_amboss_graphql_request(payload, f"CreateMagmaOffer-{template_name}")
    if data and data.get("createOffer"):
        new_offer_id = data.get("createOffer")
        logging.info(f"Successfully created Magma offer for '{template_name}'. New Offer ID: {new_offer_id}")
        return {"id": new_offer_id, **data}
    else:
        logging.error(f"Failed to create Magma offer for '{template_name}'. Response: {data}")
        return None

def update_magma_offer(offer_id_to_update, pricing_details, template_capital_for_total_size, template_name):
    duration_blocks = pricing_details["duration_days"] * BLOCKS_PER_DAY
    amboss_offer_input = {
        "base_fee": pricing_details["fixed_fee_sats"], "fee_rate": pricing_details["ppm_fee_rate"],
        "min_size": pricing_details["channel_size_sats"], "max_size": pricing_details["channel_size_sats"],
        "min_block_length": duration_blocks, "total_size": template_capital_for_total_size,
        "base_fee_cap": pricing_details["fixed_fee_sats"], "fee_rate_cap": pricing_details["ppm_fee_rate"],
    }
    log_prefix = "DRY RUN: Would update" if DRY_RUN_MODE else "Updating"
    logging.info(f"{log_prefix} Magma offer ID {offer_id_to_update} for template '{template_name}' with: {amboss_offer_input}")

    if DRY_RUN_MODE:
        return {"updateOfferDetails": {"id": offer_id_to_update, **amboss_offer_input}}

    payload = {"query": UPDATE_MAGMA_OFFER_MUTATION, "variables": {"id": offer_id_to_update, "input": amboss_offer_input}}
    data = _execute_amboss_graphql_request(payload, f"UpdateMagmaOffer-{offer_id_to_update}")
    if data and data.get("updateOfferDetails"):
        logging.info(f"Successfully updated Magma offer ID {offer_id_to_update}. Response: {data.get('updateOfferDetails')}")
        return data.get("updateOfferDetails")
    else:
        logging.error(f"Failed to update Magma offer ID {offer_id_to_update}. Response: {data}")
        return None

def delete_magma_offer(offer_id_to_delete, template_name_logging_info):
    log_prefix = "DRY RUN: Would delete" if DRY_RUN_MODE else "Deleting"
    logging.info(f"{log_prefix} Magma offer ID {offer_id_to_delete} (info: '{template_name_logging_info}')")

    if DRY_RUN_MODE:
        return True
        
    payload = {"query": DELETE_MAGMA_OFFER_MUTATION, "variables": {"id": offer_id_to_delete}}
    data = _execute_amboss_graphql_request(payload, f"DeleteMagmaOffer-{offer_id_to_delete}")
    if data and data.get("deleteOffer") is True:
        logging.info(f"Successfully deleted Magma offer ID {offer_id_to_delete}.")
        return True
    else:
        logging.error(f"Failed to delete Magma offer ID {offer_id_to_delete}. Response: {data}")
        return False

# --- Main Application Logic ---
def main():
    global general_config, magma_specific_config, AMBOSS_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DRY_RUN_MODE

    parser = argparse.ArgumentParser(description="Amboss Magma Auto-Pricing Script")
    parser.add_argument("--dry-run", action="store_true", help="Simulate actions without making live API changes.")
    args = parser.parse_args()
    DRY_RUN_MODE = args.dry_run

    if not os.path.exists(GENERAL_CONFIG_FILE_PATH):
        print(f"CRITICAL: General configuration file {GENERAL_CONFIG_FILE_PATH} not found. Exiting.")
        return
    general_config.read(GENERAL_CONFIG_FILE_PATH)

    if not os.path.exists(MAGMA_CONFIG_FILE_PATH):
        # If general config is loaded, logging might be set up.
        # Try to log this critical error before exiting.
        setup_logging() # Attempt to set up logging to catch this.
        logging.critical(f"CRITICAL: Magma-specific configuration file {MAGMA_CONFIG_FILE_PATH} not found. Exiting.")
        # Try sending telegram if possible
        AMBOSS_TOKEN = general_config.get("credentials", "amboss_authorization", fallback=None) # Needed for _execute_amboss_graphql_request called by send_telegram_notification
        TELEGRAM_BOT_TOKEN = general_config.get("telegram", "magma_bot_token", fallback=general_config.get("telegram", "telegram_bot_token", fallback=None))
        TELEGRAM_CHAT_ID = general_config.get("telegram", "telegram_user_id", fallback=None)
        if not DRY_RUN_MODE : send_telegram_notification(f"☠️ Magma AutoPrice CRITICAL: Magma config file `{MAGMA_CONFIG_FILE_PATH}` not found. Script cannot run.", level="error")
        return
    magma_specific_config.read(MAGMA_CONFIG_FILE_PATH)

    setup_logging() # Call again to ensure it uses the final log level from general_config

    if DRY_RUN_MODE:
        logging.info("乾燥実行モードが有効です。APIへの実際の変更は行われません。") # "Dry run mode is enabled. No actual changes will be made to the API." in Japanese for fun, then English.
        logging.info("DRY RUN MODE ENABLED. No actual changes will be made to the API.")

    logging.info("Starting Magma Market Fee Updater script.")

    AMBOSS_TOKEN = general_config.get("credentials", "amboss_authorization", fallback=None)
    TELEGRAM_BOT_TOKEN = general_config.get("telegram", "magma_bot_token", fallback=general_config.get("telegram", "telegram_bot_token", fallback=None))
    TELEGRAM_CHAT_ID = general_config.get("telegram", "telegram_user_id", fallback=None)

    if not magma_specific_config.getboolean("magma_autoprice", "enabled", fallback=False):
        logging.info("Magma autopricing is disabled in Magma config.ini. Exiting.")
        return
    
    if not AMBOSS_TOKEN:
        logging.critical("Amboss API token (amboss_authorization) not found in general config.ini. Exiting.")
        if not DRY_RUN_MODE : send_telegram_notification("☠️ Magma AutoPrice CRITICAL: Amboss API token not configured. Script cannot run.", level="error")
        return

    total_lnd_balance = get_lnd_onchain_balance()
    fraction_for_sale = magma_specific_config.getfloat("magma_autoprice", "lnd_balance_fraction_for_sale", fallback=0.0)
    capital_for_magma_total = int(total_lnd_balance * fraction_for_sale)
    logging.info(f"Total LND balance: {total_lnd_balance} sats. Fraction for sale: {fraction_for_sale*100}%. Total capital for Magma: {capital_for_magma_total} sats.")

    if capital_for_magma_total == 0 and fraction_for_sale > 0:
        logging.warning("No capital available from LND balance for Magma offers based on current configuration.")
    
    market_offers = fetch_public_magma_offers()
    if market_offers is None: # Explicitly check for None if fetch_public_magma_offers can return it on error
        logging.error("Failed to fetch market offers due to an API error. Pricing decisions will rely on fallbacks or skip.")
        market_offers = [] # Ensure it's an empty list to proceed with fallback logic
    
    my_existing_offers = fetch_my_current_offers()
    
    offer_template_names = magma_specific_config.get("magma_autoprice", "our_offer_ids", fallback="").split(',')
    offer_template_names = [name.strip() for name in offer_template_names if name.strip()]

    if not offer_template_names:
        logging.warning("No offer templates defined in [magma_autoprice] our_offer_ids in Magma config. Exiting.")
        return

    actions_summary = []
    capital_committed_this_run = 0 
    offers_to_delete_ids = {offer['id']: offer.get('min_size', 'UnknownSize') for offer in my_existing_offers}

    for template_name in offer_template_names:
        section_name = f"magma_offer_{template_name}"
        if not magma_specific_config.has_section(section_name):
            logging.warning(f"Magma configuration section [{section_name}] not found for offer template '{template_name}'. Skipping.")
            continue
        
        offer_config_section_proxy = magma_specific_config[section_name]
        # Create a dictionary from the proxy for easier use, and add name
        current_offer_template_config = dict(offer_config_section_proxy.items())
        current_offer_template_config['name'] = template_name # Ensure name is present

        # Helper to get typed values from the copied dict
        def get_typed_config_val(key, type_func, fallback):
            return type_func(current_offer_template_config.get(key, fallback)) if key in current_offer_template_config else fallback
        
        template_channel_size = get_typed_config_val("channel_size_sats", int, 0)
        template_duration_days = get_typed_config_val("duration_days", int, 0)
        template_duration_blocks = template_duration_days * BLOCKS_PER_DAY
        
        share = get_typed_config_val("capital_allocation_share", float, 0.0)
        template_capital_limit = int(capital_for_magma_total * share)

        if template_capital_limit < template_channel_size and template_channel_size > 0:
            logging.info(f"Offer template '{template_name}': Not enough allocated capital ({template_capital_limit} sats) for its channel size ({template_channel_size} sats). Skipping creation/update.")
            actions_summary.append(f"⚠️ Skipped {template_name} (insufficient capital: {template_capital_limit} < {template_channel_size})")
            continue
        
        # Pass the magma_specific_config object for [magma_autoprice] settings
        new_pricing = analyze_and_price_offer(market_offers, offer_config_section_proxy, magma_specific_config)

        if not new_pricing: 
            logging.error(f"Critical: Could not determine pricing for offer template '{template_name}' even with fallback. Skipping.")
            actions_summary.append(f"❌ Error pricing {template_name}")
            continue

        found_matching_existing_offer = None
        for existing_offer in my_existing_offers:
            if existing_offer.get("min_size") == template_channel_size and \
               existing_offer.get("min_block_length") == template_duration_blocks:
                found_matching_existing_offer = existing_offer
                if existing_offer['id'] in offers_to_delete_ids:
                    del offers_to_delete_ids[existing_offer['id']] 
                break
        
        if found_matching_existing_offer:
            offer_id = found_matching_existing_offer['id']
            needs_update = False
            if found_matching_existing_offer.get("base_fee") != new_pricing["fixed_fee_sats"] or \
               found_matching_existing_offer.get("fee_rate") != new_pricing["ppm_fee_rate"] or \
               found_matching_existing_offer.get("total_size") != template_capital_limit:
                needs_update = True
            
            if needs_update:
                update_result = update_magma_offer(offer_id, new_pricing, template_capital_limit, template_name)
                if update_result:
                    actions_summary.append(f"✅ Updated {template_name} (ID {offer_id}): APR {new_pricing['calculated_apr']}% (Fixed: {new_pricing['fixed_fee_sats']}, PPM: {new_pricing['ppm_fee_rate']}, TotalSize: {template_capital_limit})")
                    capital_committed_this_run += template_capital_limit 
                else: actions_summary.append(f"❌ Failed to update {template_name} (ID {offer_id})")
            else:
                logging.info(f"Offer for template '{template_name}' (ID {offer_id}) is already up-to-date. No changes needed.")
                actions_summary.append(f"ℹ️ No change for {template_name} (ID {offer_id})")
                capital_committed_this_run += found_matching_existing_offer.get("total_size", 0) 
        else:
            if template_capital_limit >= template_channel_size : 
                create_result = create_magma_offer(new_pricing, template_capital_limit, template_name)
                if create_result and create_result.get("id"): 
                    new_id = create_result["id"]
                    actions_summary.append(f"🚀 Created {template_name} (New ID {new_id}): APR {new_pricing['calculated_apr']}% (Fixed: {new_pricing['fixed_fee_sats']}, PPM: {new_pricing['ppm_fee_rate']}, TotalSize: {template_capital_limit})")
                    capital_committed_this_run += template_capital_limit
                else: actions_summary.append(f"❌ Failed to create {template_name}")
            else:
                logging.info(f"Skipping creation of new offer for '{template_name}': Template capital limit ({template_capital_limit}) is less than channel size ({template_channel_size}).")
                actions_summary.append(f"⚠️ Skipped create {template_name} (template capital too low for one channel)")

    if offers_to_delete_ids: 
        logging.info(f"Found {len(offers_to_delete_ids)} existing offers not matching current templates. Attempting deletion.")
        for offer_id_to_del, offer_size_info in offers_to_delete_ids.items():
            if delete_magma_offer(offer_id_to_del, f"Unmanaged/Old (Size {offer_size_info})"):
                actions_summary.append(f"🗑️ Deleted unmanaged/old offer ID {offer_id_to_del} (Was size: {offer_size_info})")
            else: actions_summary.append(f"❌ Failed to delete unmanaged/old offer ID {offer_id_to_del}")

    if actions_summary:
        summary_message = "Magma AutoPrice Update Summary:\n" + "\n".join(actions_summary)
        summary_message += f"\n\nLND Balance: {total_lnd_balance:,} sats"
        summary_message += f"\nTotal Capital for Magma (Configured): {capital_for_magma_total:,} sats"
        summary_message += f"\nSum of 'total_size' for active/processed offers: {capital_committed_this_run:,} sats"
        if DRY_RUN_MODE: summary_message = "[DRY RUN] " + summary_message
        send_telegram_notification(summary_message)
    else:
        logging.info("No specific actions (create/update/delete) taken on Magma offers in this run.")
        notify_on_no_change = not magma_specific_config.getboolean("magma_autoprice", "telegram_notify_on_change_only", fallback=True)
        if notify_on_no_change:
             send_telegram_notification("ℹ️ Magma AutoPrice: No changes made to offers in this run." + (" (DRY RUN)" if DRY_RUN_MODE else ""))

    logging.info("Magma Market Fee Updater script finished.")

if __name__ == "__main__":
    main()