# Magma Channel Auto-Pricing Script (Lightning Network)
#
# Purpose:
# This script automates the process of pricing and managing Lightning Network channel selling
# offers on Amboss Magma. It queries current market offers, analyzes them to determine
# competitive pricing points, and then creates or updates the user's own sell offers.
# The pricing strategy aims to be competitive (e.g., top 10th percentile) without necessarily
# being the absolute cheapest, considering the quality of the node.
# New offers are created in a 'USER_DISABLED' state, requiring manual review and
# enabling via the Amboss Magma UI or a subsequent script run that finds the template funded.
#
# Key Features:
# - Reads general configuration from `../config.ini` and Magma-specific settings from `Magma/magma_config.ini`.
# - Periodically fetches public Amboss Magma sell offers.
# - Analyzes market offers based on fixed fees, PPM rates, and potentially APR.
# - Calculates competitive pricing for the user's own pre-defined offer templates.
# - Supports managing multiple concurrent sell offers with different parameters.
# - Optionally queries LND for available on-chain balance (excluding Loop UTXOs) to limit capital committed.
# - Creates, updates, enables, or disables user offers on Amboss Magma.
# - Sends Telegram notifications summarizing pricing changes or actions taken.
# - Provides detailed logging to `logs/magma-market-fee.log`.
# - Includes a --dry-run mode to simulate actions without making live API changes.
#
# How to Run:
# 1. Ensure Python 3 is installed.
# 2. Install required Python packages: `pip install requests telebot configparser`
# 3. Ensure `../config.ini` exists and is configured (see config.ini.example).
# 4. Create `Magma/magma_config.ini` for Magma-specific settings.
# 5. Make the script executable: `chmod +x magma_market_fee.py`
# 6. Run the script: `python /path/to/Magma/magma_market_fee.py`
#    For dry run: `python /path/to/Magma/magma_market_fee.py --dry-run`
#    It's designed to be run periodically, e.g., by a systemd timer or cron job.

import argparse
import configparser
import json
import logging
import os
import requests
import subprocess
import telebot
import math

# --- Global Configuration ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(SCRIPT_DIR)
GENERAL_CONFIG_FILE_PATH = os.path.join(PARENT_DIR, "config.ini")
MAGMA_CONFIG_FILE_PATH = os.path.join(SCRIPT_DIR, "magma_config.ini")
LOG_DIR = os.path.join(PARENT_DIR, "logs")
LOG_FILE_PATH = os.path.join(LOG_DIR, "magma-market-fee.log")
BLOCKS_PER_DAY = 144

general_config = configparser.ConfigParser()
magma_specific_config = configparser.ConfigParser()

AMBOSS_TOKEN = None
TELEGRAM_BOT_TOKEN = None
TELEGRAM_CHAT_ID = None
LNCLI_PATH = "lncli"
DRY_RUN_MODE = False
MY_NODE_PUBKEY = None # Loaded from general_config [info] NODE

# --- GraphQL Queries/Mutations ---
GET_PUBLIC_MAGMA_OFFERS_QUERY = """
query GetPublicOffers($filter: OfferFilter, $sort: OfferSort, $first: Int, $after: String) {
  getOffers(filter: $filter, sort: $sort, first: $first, after: $after) {
    list {
      id
      offer_type
      base_fee
      fee_rate
      max_size
      min_block_length
      min_size
      seller_score
      status
      side
      total_size
      account # Pubkey of seller
      # orders { locked_size } # Not currently used in analysis
      # tags { name } # Not currently used in analysis
    }
    # pageInfo { hasNextPage endCursor } # For pagination if needed later
  }
}
"""

GET_MY_MAGMA_OFFERS_QUERY = """
query MyOffers {
  getUser {
    market {
      offers {
        list {
          id
          status
          offer_type
          base_fee
          # base_fee_cap # Not directly used by script logic for now
          fee_rate
          # fee_rate_cap # Not directly used by script logic for now
          max_size
          min_block_length
          min_size
          total_size
          # conditions { condition } # Not used
          # orders { locked_size } # Not used for own offer state decision
          # seller_score # Not relevant for own offers in this context
          side
          account # Our own pubkey
          # amboss_fee_rate # Not used
          # onchain_multiplier # Not used
          # onchain_priority # Not used
          # created_at and updated_at were in old query, not in new user provided one, and not used by script.
        }
      }
    }
  }
}
"""

CREATE_MAGMA_OFFER_MUTATION = """
mutation CreateOffer($input: CreateOffer!) {
  createOffer(input: $input)
}
"""

UPDATE_MAGMA_OFFER_MUTATION = """
mutation UpdateOfferDetails($id: String!, $input: UpdateOfferDetailsInput!) {
  updateOfferDetails(id: $id, input: $input) {
    base_fee fee_rate min_block_length min_size max_size total_size status # Include status if API returns it
  }
}
"""

# Using toggleOffer instead of deleteOffer
TOGGLE_MAGMA_OFFER_MUTATION = """
mutation ToggleOffer($toggleOfferId: String!) {
  toggleOffer(id: $toggleOfferId) # Returns Boolean
}
"""

# --- Logging Setup ---
def setup_logging():
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR, exist_ok=True)
    log_level_str = general_config.get("system", "log_level", fallback="INFO").upper()
    numeric_level = getattr(logging, log_level_str, logging.INFO)
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s",
        handlers=[
            logging.handlers.RotatingFileHandler(LOG_FILE_PATH, maxBytes=10*1024*1024, backupCount=5),
            logging.StreamHandler()])
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("telebot").setLevel(logging.WARNING)

# --- Telegram Notification ---
def send_telegram_notification(text, level="info"):
    log_message = f"Telegram NOTIFICATION: {text}"
    if level == "error": logging.error(log_message)
    elif level == "warning": logging.warning(log_message)
    else: logging.info(log_message)

    if DRY_RUN_MODE:
        logging.info(f"DRY RUN: Would send Telegram notification: {text}")
        return

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram token or chat ID not configured. Skipping notification.")
        return
    try:
        bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
        bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode='Markdown')
    except Exception as e:
        logging.error(f"Failed to send Telegram message using telebot: {e}")

# --- Amboss API Interaction ---
def _execute_amboss_graphql_request(payload: dict, operation_name: str = "AmbossGraphQL"):
    if not AMBOSS_TOKEN:
        logging.error("Amboss API token not configured.")
        return None

    is_mutation = operation_name.lower().startswith(("create", "update", "delete", "toggle"))
    if DRY_RUN_MODE and is_mutation:
        logging.info(f"DRY RUN: Preventing API call for {operation_name}. Payload: {json.dumps(payload, indent=2)}")
        if "Create" in operation_name: return {"createOffer": f"dry-run-id-for-{operation_name}"}
        if "Update" in operation_name: return {"updateOfferDetails": {"id": "dry-run-updated-id", "status": "dry_run_simulated_update"}}
        if "Toggle" in operation_name: return {"toggleOffer": True} # Simulate successful toggle
        return {"dryRunSimulatedSuccess": True}

    url = "https://api.amboss.space/graphql"
    headers = {"content-type": "application/json", "Authorization": f"Bearer {AMBOSS_TOKEN}"}
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
def get_lncli_utxos(current_general_config):
    """
    Fetches LND UTXOs and filters out those known to be used by Loop.
    Adapted from magma_sale_process.py
    """
    lncli_cmd_path = current_general_config.get("paths", "lncli_path", fallback="lncli")
    command = [lncli_cmd_path, "listunspent", "--min_confs=3"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=20)
        lnd_utxos_data = json.loads(result.stdout)
        lnd_utxos = lnd_utxos_data.get("utxos", [])
    except FileNotFoundError:
        logging.error(f"lncli command not found at '{lncli_cmd_path}'.")
        return []
    except subprocess.CalledProcessError as e:
        logging.error(f"Error executing '{' '.join(command)}': {e.stderr}")
        return []
    except json.JSONDecodeError as e:
        logging.error(f"Error decoding JSON from '{' '.join(command)}': {e}")
        return []
    except Exception as e:
        logging.exception(f"Unexpected error getting LND UTXOs with '{' '.join(command)}':")
        return []

    loop_utxos = []
    loop_command_path_base = current_general_config.get("system", "path_command", fallback="")
    if loop_command_path_base:
        loop_exe_path = os.path.join(loop_command_path_base, "loop")
        if os.path.exists(loop_exe_path):
            # These paths might need to be configurable if not default
            loop_rpc = current_general_config.get("loop", "rpcserver", fallback="localhost:11010") # Example default
            loop_tls = current_general_config.get("loop", "tlscertpath", fallback="~/.loop/mainnet/tls.cert")
            # Loop macaroon might also be needed depending on loopd setup. For staticunspent, maybe not.
            
            # Expand tilde for tls path
            loop_tls_expanded = os.path.expanduser(loop_tls)

            # Check if tls cert exists, otherwise loop command might hang or error cryptically
            if not os.path.exists(loop_tls_expanded):
                logging.warning(f"Loop TLS cert not found at {loop_tls_expanded}, skipping Loop UTXO check.")
            else:
                litloop_cmd_parts = [
                    loop_exe_path,
                    f"--rpcserver={loop_rpc}",
                    f"--tlscertpath={loop_tls_expanded}",
                    # Add --macaroonpath if your loopd requires it for 'staticlistunspent'
                    "staticlistunspent" # Corrected from 'static listunspent'
                ]
                logging.debug(f"Executing Loop command: {' '.join(litloop_cmd_parts)}")
                try:
                    process = subprocess.Popen(litloop_cmd_parts, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    output, error = process.communicate(timeout=20) # Add timeout
                    output_decoded = output.decode("utf-8")
                    error_decoded = error.decode("utf-8").strip()
                    if error_decoded:
                        logging.warning(f"Loop staticlistunspent stderr: {error_decoded}")
                    
                    loop_data = json.loads(output_decoded)
                    loop_utxos_list = loop_data.get("utxos", [])
                    # Ensure loop_utxos are in the same format as LND UTXOs if direct comparison is needed
                    # For outpoint string comparison, it should be fine.
                    loop_utxos = [lu["outpoint"] for lu in loop_utxos_list if "outpoint" in lu] # Get list of outpoint strings
                    logging.info(f"Found {len(loop_utxos)} static loop UTXO outpoints.")
                except subprocess.TimeoutExpired:
                    logging.error(f"Timeout executing Loop command: {' '.join(litloop_cmd_parts)}")
                except json.JSONDecodeError as e:
                    logging.error(f"Error decoding litloop output: {e}. Output: {output_decoded}")
                except FileNotFoundError: # If loop_exe_path is somehow invalid despite os.path.exists
                    logging.error(f"Loop command not found at '{loop_exe_path}' during execution.")
                except Exception as e:
                    logging.exception(f"Error getting Loop static UTXOs with command '{' '.join(litloop_cmd_parts)}':")
        else:
            logging.debug(f"Loop executable not found at {loop_exe_path}, skipping Loop UTXO check.")
    else:
        logging.debug("Loop command path base (system.path_command) not configured, skipping Loop UTXO check.")

    if loop_utxos: # If we have loop outpoints to filter by
        filtered_lnd_utxos = [utxo for utxo in lnd_utxos if utxo.get("outpoint") not in loop_utxos]
    else:
        filtered_lnd_utxos = lnd_utxos

    filtered_lnd_utxos = sorted(filtered_lnd_utxos, key=lambda x: x.get("amount_sat", 0), reverse=True)
    logging.debug(f"Filtered LND UTXOs (excluding loop static addresses): {json.dumps(filtered_lnd_utxos, indent=2 if logging.getLogger().getEffectiveLevel() == logging.DEBUG else None)}")
    return filtered_lnd_utxos

def get_lnd_onchain_balance(current_general_config):
    """
    Calculates LND on-chain balance available for Magma, excluding Loop-managed UTXOs.
    """
    if DRY_RUN_MODE:
        logging.info("DRY RUN: Simulating LND wallet balance check. Returning 10,000,000 sats.")
        return 10000000
        
    available_utxos = get_lncli_utxos(current_general_config)
    confirmed_balance = sum(int(utxo.get("amount_sat", 0)) for utxo in available_utxos)
    logging.info(f"LND confirmed on-chain balance (excluding Loop UTXOs): {confirmed_balance} sats from {len(available_utxos)} UTXOs.")
    return confirmed_balance

# --- Market Analysis & Pricing Logic --- (analyze_and_price_offer and calculate_apr remain largely the same)
def fetch_public_magma_offers(node_pubkey_to_exclude, current_magma_config):
    logging.info("Fetching public Magma sell offers...")
    # Server-side filtering
    filters = {"type": "CHANNEL", "status": "ENABLED", "side": "SELL"}
    # Sort by seller_score descending to potentially get higher quality offers first, though primary sort for pricing is APR.
    # The API might not support complex sorts or specific fields like seller_score for sorting.
    # For now, let's not specify sort and do it client-side if needed for the initial seller_score percentile.
    # sort_by = {"field": "SELLER_SCORE", "direction": "DESC"} # Example if API supported
    
    variables = {"filter": filters, "first": 200} # Fetch a decent number for analysis, max 200-500 usually reasonable
    
    payload = {"query": GET_PUBLIC_MAGMA_OFFERS_QUERY, "variables": variables}
    data = _execute_amboss_graphql_request(payload, "GetPublicOffers")
    
    processed_offers = []
    if data and data.get("getOffers", {}).get("list"):
        raw_offers = data["getOffers"]["list"]
        min_seller_score_filter = current_magma_config.getfloat("magma_autoprice", "min_seller_score_filter", fallback=0.0)

        for offer in raw_offers:
            try:
                # Basic filtering already done by API, but double check and add client-side ones
                if offer.get("status") != "ENABLED" or offer.get("side") != "SELL" or offer.get("offer_type") != "CHANNEL":
                    continue
                if node_pubkey_to_exclude and offer.get("account") == node_pubkey_to_exclude:
                    logging.debug(f"Excluding own offer (Account: {offer.get('account')}) from market analysis.")
                    continue

                parsed_offer = {
                    "id": offer.get("id"), # Assuming 'id' is present for public offers too
                    "offer_type": offer.get("offer_type"),
                    "base_fee": int(offer.get("base_fee", 0)),
                    "fee_rate": int(offer.get("fee_rate", 0)),
                    "max_size": int(offer.get("max_size", 0)),
                    "min_block_length": int(offer.get("min_block_length", 0)),
                    "min_size": int(offer.get("min_size", 0)),
                    "seller_score": float(offer.get("seller_score", 0.0)),
                    "status": offer.get("status"),
                    "side": offer.get("side"),
                    "total_size": int(offer.get("total_size", 0)),
                    "account": offer.get("account"), # Seller's pubkey
                    "node_alias": offer.get("account") # Use account as alias placeholder if no direct alias
                }

                if parsed_offer["seller_score"] < min_seller_score_filter:
                    logging.debug(f"Excluding market offer {parsed_offer['id']} due to seller_score {parsed_offer['seller_score']} < {min_seller_score_filter}")
                    continue
                
                # Ensure essential fields for APR calculation are valid
                if not (parsed_offer["min_size"] > 0 and parsed_offer["min_block_length"] > 0 and parsed_offer["base_fee"] >= 0 and parsed_offer["fee_rate"] >= 0):
                    logging.debug(f"Skipping market offer {parsed_offer.get('id', 'N/A')} due to invalid numeric values for APR calc.")
                    continue

                processed_offers.append(parsed_offer)
            except (ValueError, TypeError) as e:
                logging.warning(f"Skipping market offer due to parsing error: {offer.get('id', 'N/A')}. Error: {e}")
                continue
        
        logging.info(f"Fetched and processed {len(processed_offers)} relevant public Magma CHANNEL/SELL/ENABLED offers (excluding own, score >= {min_seller_score_filter}).")
        return processed_offers
    else:
        logging.warning("No public Magma offers found or error in fetching.")
        return []
def calculate_apr(fixed_fee_sats, ppm_fee_rate, channel_size_sats, duration_days_float):
    if channel_size_sats == 0 or duration_days_float == 0: return 0.0
    variable_fee_sats = (ppm_fee_rate / 1_000_000) * channel_size_sats
    total_fee_sats = fixed_fee_sats + variable_fee_sats
    apr = (total_fee_sats / channel_size_sats) * (365.0 / duration_days_float) * 100
    return round(apr, 2)

def analyze_and_price_offer(market_offers, our_offer_template_config_proxy, current_magma_config):
    logging.info(f"Analyzing market for offer template: {our_offer_template_config_proxy.name}")
    our_size_template = our_offer_template_config_proxy.getint('channel_size_sats')
    our_duration_days_template = our_offer_template_config_proxy.getint('duration_days')

    relevant_offers = []
    for offer in market_offers: # Market offers are already pre-filtered (CHANNEL, ENABLED, SELL, not own, min_seller_score)
        try:
            # Fields directly from pre-processed market_offers
            size = offer['min_size'] 
            # Assuming we only compete with offers that are fixed size (min_size == max_size)
            if offer['max_size'] != size:
                 logging.debug(f"Skipping variable size market offer {offer.get('id')} for template {our_offer_template_config_proxy.name}")
                 continue
            
            duration_blocks = offer['min_block_length']
            fixed_fee = offer['base_fee']
            ppm_fee = offer['fee_rate']
            duration_days_market = duration_blocks / BLOCKS_PER_DAY if BLOCKS_PER_DAY > 0 else 0
            
            size_similarity_threshold = current_magma_config.getfloat("magma_autoprice", "size_similarity_threshold", fallback=0.50)
            duration_similarity_threshold = current_magma_config.getfloat("magma_autoprice", "duration_similarity_threshold", fallback=0.50)

            # Check if market offer is comparable to our template
            size_matches = (abs(size - our_size_template) / our_size_template < size_similarity_threshold if our_size_template > 0 else True)
            duration_matches = (abs(duration_days_market - our_duration_days_template) / our_duration_days_template < duration_similarity_threshold if our_duration_days_template > 0 else True)

            if size_matches and duration_matches:
                # APR calculation should use the market offer's actual size
                apr = calculate_apr(fixed_fee, ppm_fee, size, duration_days_market)
                relevant_offers.append({
                    "id": offer.get("id"), "size": size, "duration_blocks": duration_blocks,
                    "duration_days": duration_days_market, "base_fee": fixed_fee, "fee_rate": ppm_fee,
                    "apr": apr, 
                    "seller_score": offer.get("seller_score", 0.0), # Keep seller_score
                    "node_alias": offer.get("node_alias", offer.get("account", "N/A")) # 'node_alias' is 'account' here
                })
        except (ValueError, TypeError, ZeroDivisionError, KeyError) as e:
            logging.warning(f"Skipping market offer during analysis due to data issue: {offer.get('id', 'N/A')}, Error: {e}")
            continue
            
    if not relevant_offers:
        logging.warning(f"No relevant market offers found for comparison against template '{our_offer_template_config_proxy.name}'. Using fallback pricing.")
        new_fixed_fee = our_offer_template_config_proxy.getint('min_fixed_fee_sats', 0)
        new_ppm_fee = our_offer_template_config_proxy.getint('min_ppm_fee', 0)
        global_min_ppm = current_magma_config.getint("magma_autoprice", "global_min_ppm_fee", fallback=0)
        new_ppm_fee = max(new_ppm_fee, global_min_ppm)
        our_fallback_apr = calculate_apr(new_fixed_fee, new_ppm_fee, our_size_template, float(our_duration_days_template))
        logging.info(f"Using fallback pricing for {our_offer_template_config_proxy.name}: Fixed={new_fixed_fee}, PPM={new_ppm_fee}, APR={our_fallback_apr}%")
        return {"channel_size_sats": our_size_template, "duration_days": our_duration_days_template,
                "fixed_fee_sats": new_fixed_fee, "ppm_fee_rate": new_ppm_fee, "calculated_apr": our_fallback_apr}

    # Dual Percentile Logic
    # 1. Filter by seller_score percentile
    seller_score_percentile = current_magma_config.getfloat("magma_autoprice", "seller_score_percentile_target", fallback=10.0) / 100.0
    relevant_offers.sort(key=lambda x: x['seller_score'], reverse=True) # Sort by score descending
    
    num_top_score_sellers = math.ceil(len(relevant_offers) * seller_score_percentile)
    if num_top_score_sellers == 0 and len(relevant_offers) > 0 : # ensure at least one if relevant_offers exist
        num_top_score_sellers = 1 
        
    high_score_offers = relevant_offers[:num_top_score_sellers]

    if not high_score_offers: # If percentile resulted in empty or no relevant offers to begin with
        logging.warning(f"No high-score sellers found for template '{our_offer_template_config_proxy.name}' after score percentile. Using all relevant_offers for price benchmark or fallback if still none.")
        # If relevant_offers was populated but high_score_offers is empty due to percentile cut-off,
        # consider using all relevant_offers for pricing, or stick to the initial fallback logic if relevant_offers itself was empty.
        # The existing code path will handle if relevant_offers was empty.
        # If relevant_offers was not empty, but high_score_offers became empty (e.g. percentile too small for the count)
        # using 'relevant_offers' instead of 'high_score_offers' for price sort might be an option.
        # For now, if high_score_offers is empty, it means the earlier check for `if not relevant_offers` would trigger fallback.
        # If relevant_offers had items, but num_top_score_sellers became 0 (e.g. 0.05 percentile on 10 items), we made it pick at least 1.
        # So high_score_offers should not be empty if relevant_offers was not.
        # The case of empty relevant_offers is handled above.
        pass # Fallback logic is already triggered if relevant_offers is empty.

    offers_for_price_benchmark = high_score_offers if high_score_offers else relevant_offers # Fallback to all relevant if high_score subset is somehow empty

    if not offers_for_price_benchmark: # Should be caught by the first `if not relevant_offers`
        logging.error(f"Critical: No offers available for price benchmark for template {our_offer_template_config_proxy.name} even after fallbacks. Using template mins.")
        # This is a duplicate of the initial fallback, but as a safeguard.
        new_fixed_fee = our_offer_template_config_proxy.getint('min_fixed_fee_sats', 0)
        new_ppm_fee = our_offer_template_config_proxy.getint('min_ppm_fee', 0)
        global_min_ppm = current_magma_config.getint("magma_autoprice", "global_min_ppm_fee", fallback=0)
        new_ppm_fee = max(new_ppm_fee, global_min_ppm)
        our_fallback_apr = calculate_apr(new_fixed_fee, new_ppm_fee, our_size_template, float(our_duration_days_template))
        return {"channel_size_sats": our_size_template, "duration_days": our_duration_days_template,
                "fixed_fee_sats": new_fixed_fee, "ppm_fee_rate": new_ppm_fee, "calculated_apr": our_fallback_apr}

    # 2. Sort this (high-score or all relevant) group by APR for price percentile
    offers_for_price_benchmark.sort(key=lambda x: x['apr'] if x['apr'] is not None else float('inf'))
    logging.debug(f"Offers for price benchmark (post score filter) for {our_offer_template_config_proxy.name} (count: {len(offers_for_price_benchmark)}): {json.dumps(offers_for_price_benchmark, indent=2 if logging.getLogger().getEffectiveLevel() == logging.DEBUG else None)}")

    price_percentile_target = current_magma_config.getfloat("magma_autoprice", "pricing_strategy_percentile", fallback=10.0) / 100.0
    target_index = math.floor(len(offers_for_price_benchmark) * price_percentile_target) # floor to be less aggressive initially
    target_index = max(0, min(target_index, len(offers_for_price_benchmark) - 1))
    
    benchmark_offer_initial = offers_for_price_benchmark[target_index]
    logging.info(f"Initial price benchmark for '{our_offer_template_config_proxy.name}' (from {len(offers_for_price_benchmark)} offers, at {price_percentile_target*100:.0f}th percentile of APR): Score {benchmark_offer_initial['seller_score']}, APR {benchmark_offer_initial['apr']}% by '{benchmark_offer_initial['node_alias']}'")

    position_offset = current_magma_config.getint("magma_autoprice", "pricing_strategy_position_offset", fallback=1)
    # Ensure final_benchmark_index is within bounds of the offers_for_price_benchmark list
    final_benchmark_index = min(target_index + position_offset, len(offers_for_price_benchmark) - 1)
    final_benchmark_index = max(0, final_benchmark_index) # ensure it's not negative if offset is negative

    final_benchmark_offer = offers_for_price_benchmark[final_benchmark_index]
    logging.info(f"Final price benchmark for '{our_offer_template_config_proxy.name}' (index {final_benchmark_index}): Score {final_benchmark_offer['seller_score']}, APR {final_benchmark_offer['apr']}% by '{final_benchmark_offer['node_alias']}'")

# --- Manage Our Offers on Amboss ---
def fetch_my_current_offers():
    logging.info("Fetching my current Magma sell offers...")
    payload = {"query": GET_MY_MAGMA_OFFERS_QUERY}
    data = _execute_amboss_graphql_request(payload, "MyOffers") # Changed operation name for clarity
    
    processed_offers = []
    if data and data.get("getUser", {}).get("market", {}).get("offers", {}).get("list"):
        my_raw_offers = data["getUser"]["market"]["offers"]["list"]
        for offer_item in my_raw_offers:
            try:
                if not offer_item: continue
                # Filter for CHANNEL sell offers only
                if offer_item.get("offer_type") != "CHANNEL" or offer_item.get("side") != "SELL":
                    logging.debug(f"Skipping own offer {offer_item.get('id')} - not a CHANNEL sell offer. Type: {offer_item.get('offer_type')}, Side: {offer_item.get('side')}")
                    continue

                # Parse numbers, API returns them as strings or direct numbers sometimes
                details = {
                    "id": offer_item.get("id"),
                    "status": offer_item.get("status", "UNKNOWN").upper(), # Ensure upper for consistency e.g. ENABLED
                    "offer_type": offer_item.get("offer_type"),
                    "base_fee": int(offer_item.get("base_fee", 0)),
                    "fee_rate": int(offer_item.get("fee_rate", 0)),
                    "max_size": int(offer_item.get("max_size", 0)),
                    "min_block_length": int(offer_item.get("min_block_length", 0)),
                    "min_size": int(offer_item.get("min_size", 0)),
                    "total_size": int(offer_item.get("total_size", 0)),
                    "side": offer_item.get("side"),
                    "account": offer_item.get("account")
                }
                # Validate essential fields for matching
                if not (details["id"] and details["status"] != "UNKNOWN" and \
                        details["min_size"] is not None and details["min_block_length"] is not None):
                    logging.warning(f"Own offer {offer_item.get('id')} missing essential fields for processing. Skipping. Data: {offer_item}")
                    continue
                processed_offers.append(details)
            except (ValueError, TypeError, KeyError) as e:
                logging.warning(f"Error parsing own offer {offer_item.get('id', 'N/A')}: {e}. Offer data: {offer_item}")
                continue
        logging.info(f"Found and processed {len(processed_offers)} existing Magma CHANNEL sell offers.")
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
        # Simulate the structure Amboss might return for createOffer, including a simulated ID.
        return {"createOffer": f"dryrun-offer-id-{template_name.replace(' ', '_')}"}

    payload = {"query": CREATE_MAGMA_OFFER_MUTATION, "variables": {"input": amboss_offer_input}}
    data = _execute_amboss_graphql_request(payload, f"CreateMagmaOffer-{template_name}")
    if data and data.get("createOffer"): # createOffer returns the new Offer ID (String)
        new_offer_id = data.get("createOffer")
        logging.info(f"Successfully created Magma offer for '{template_name}'. New Offer ID: {new_offer_id}")
        return {"id": new_offer_id, "status_after_create": "ACTIVE"} # Assume ACTIVE, will be toggled
    else:
        logging.error(f"Failed to create Magma offer for '{template_name}'. Response: {data}")
        return None

def update_magma_offer(offer_id_to_update, pricing_details, template_capital_for_total_size, template_name):
    duration_blocks = pricing_details["duration_days"] * BLOCKS_PER_DAY
    amboss_offer_input = {
        "base_fee": pricing_details["fixed_fee_sats"], "fee_rate": pricing_details["ppm_fee_rate"],
        "min_size": pricing_details["channel_size_sats"], "max_size": pricing_details["channel_size_sats"], # Assuming fixed size for our offers
        "min_block_length": duration_blocks, 
        "total_size": template_capital_for_total_size
        # base_fee_cap and fee_rate_cap are part of CreateOfferInput but optional in UpdateOfferDetailsInput
        # If you want to update them, add them here. For now, matching previous behavior.
    }
    log_prefix = "DRY RUN: Would update" if DRY_RUN_MODE else "Updating"
    logging.info(f"{log_prefix} Magma offer ID {offer_id_to_update} for template '{template_name}' with: {amboss_offer_input}")

    if DRY_RUN_MODE:
        # Simulate the structure of the returned Offer object after update
        return {"id": offer_id_to_update, **amboss_offer_input, "status": "DRY_RUN_STATUS_POST_UPDATE"}

    payload = {"query": UPDATE_MAGMA_OFFER_MUTATION, "variables": {"id": offer_id_to_update, "input": amboss_offer_input}}
    data = _execute_amboss_graphql_request(payload, f"UpdateMagmaOffer-{offer_id_to_update}")
    if data and data.get("updateOfferDetails"):
        logging.info(f"Successfully updated Magma offer ID {offer_id_to_update}.")
        return data.get("updateOfferDetails") # This is the Offer object
    else:
        logging.error(f"Failed to update Magma offer ID {offer_id_to_update}. Response: {data}")
        return None

def toggle_magma_offer_status(offer_id, template_name_logging_info, target_log_status_str):
    log_prefix = "DRY RUN: Would toggle" if DRY_RUN_MODE else "Toggling"
    # target_log_status_str is for logging the intent, actual toggle flips the state
    logging.info(f"{log_prefix} Magma offer ID {offer_id} (info: '{template_name_logging_info}'). Intent from script: {target_log_status_str}.")
    
    if DRY_RUN_MODE:
        logging.info(f"DRY RUN: Simulating toggle for {offer_id} successful.")
        return True # Simulate success

    payload = {"query": TOGGLE_MAGMA_OFFER_MUTATION, "variables": {"toggleOfferId": offer_id}}
    data = _execute_amboss_graphql_request(payload, f"ToggleMagmaOffer-{offer_id}")
    if data and data.get("toggleOffer") is True: # toggleOffer returns Boolean
        logging.info(f"Successfully toggled status for Magma offer ID {offer_id}.")
        return True
    else:
        logging.error(f"Failed to toggle status for Magma offer ID {offer_id}. Response: {data}")
        return False

# --- Main Application Logic ---
def main():
    global general_config, magma_specific_config, AMBOSS_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DRY_RUN_MODE, LNCLI_PATH

    parser = argparse.ArgumentParser(description="Amboss Magma Auto-Pricing Script")
    parser.add_argument("--dry-run", action="store_true", help="Simulate actions without making live API changes.")
    args = parser.parse_args()
    DRY_RUN_MODE = args.dry_run

    if not os.path.exists(GENERAL_CONFIG_FILE_PATH):
        print(f"CRITICAL: General configuration file {GENERAL_CONFIG_FILE_PATH} not found. Exiting.")
        return
    general_config.read(GENERAL_CONFIG_FILE_PATH)
    
    # Setup logging early so issues with magma_config can be logged
    setup_logging() 

    if not os.path.exists(MAGMA_CONFIG_FILE_PATH):
        logging.critical(f"CRITICAL: Magma-specific configuration file {MAGMA_CONFIG_FILE_PATH} not found. Exiting.")
        # Try sending telegram if possible (tokens would be loaded after this block if file existed)
        # For now, just log and exit. Telegram setup happens later.
        return
    magma_specific_config.read(MAGMA_CONFIG_FILE_PATH)

    if DRY_RUN_MODE:
        logging.info("DRY RUN MODE ENABLED. No actual changes will be made to the API.")

    logging.info("Starting Magma Market Fee Updater script.")

    AMBOSS_TOKEN = general_config.get("credentials", "amboss_authorization", fallback=None)
    TELEGRAM_BOT_TOKEN = general_config.get("telegram", "magma_bot_token", fallback=general_config.get("telegram", "telegram_bot_token", fallback=None))
    TELEGRAM_CHAT_ID = general_config.get("telegram", "telegram_user_id", fallback=None)
    LNCLI_PATH = general_config.get("paths", "lncli_path", fallback="lncli")
    MY_NODE_PUBKEY = general_config.get("info", "NODE", fallback=None)

    if not MY_NODE_PUBKEY:
        logging.warning("Node pubkey (info.NODE) not found in general config.ini. Cannot exclude own offers from market analysis. This is not critical for operation but recommended.")

    if not magma_specific_config.getboolean("magma_autoprice", "enabled", fallback=False):
        logging.info("Magma autopricing is disabled in Magma config.ini. Exiting.")
        return
    if not AMBOSS_TOKEN:
        logging.critical("Amboss API token (amboss_authorization) not found in general config.ini. Exiting.")
        if not DRY_RUN_MODE : send_telegram_notification("☠️ Magma AutoPrice CRITICAL: Amboss API token not configured.", level="error")
        return

    total_lnd_balance_for_magma = get_lnd_onchain_balance(general_config)
    fraction_for_sale = magma_specific_config.getfloat("magma_autoprice", "lnd_balance_fraction_for_sale", fallback=0.0)
    capital_for_magma_total_config = int(total_lnd_balance_for_magma * fraction_for_sale)
    logging.info(f"LND Balance for Magma (Excl. Loop): {total_lnd_balance_for_magma} sats. Configured Fraction: {fraction_for_sale*100}%. Total Capital for Magma Offers: {capital_for_magma_total_config} sats.")

    if capital_for_magma_total_config == 0 and fraction_for_sale > 0:
        logging.warning("No capital available from LND balance for Magma offers based on current configuration.")
         # We might still want to manage existing offers (e.g. disable them if their template share makes their capital 0)
    
    
    market_offers = fetch_public_magma_offers(MY_NODE_PUBKEY, magma_specific_config)
    if market_offers is None: market_offers = [] 
    
    my_existing_offers = fetch_my_current_offers()
    
    offer_template_names = magma_specific_config.get("magma_autoprice", "our_offer_ids", fallback="").split(',')
    offer_template_names = [name.strip() for name in offer_template_names if name.strip()]

    if not offer_template_names:
        logging.warning("No offer templates defined in [magma_autoprice] our_offer_ids in Magma config. Exiting.")
        return

    actions_summary = []
    managed_offer_ids_this_run = set()

    for template_name in offer_template_names:
        section_name = f"magma_offer_{template_name}"
        if not magma_specific_config.has_section(section_name):
            logging.warning(f"Magma configuration section [{section_name}] not found. Skipping.")
            continue
        
        offer_template_proxy = magma_specific_config[section_name]
        template_channel_size = offer_template_proxy.getint("channel_size_sats")
        template_duration_days = offer_template_proxy.getint("duration_days")
        template_duration_blocks = template_duration_days * BLOCKS_PER_DAY
        share = offer_template_proxy.getfloat("capital_allocation_share", fallback=0.0)
        template_capital_limit_for_total_size = int(capital_for_magma_total_config * share)
        
        template_is_active_by_config = template_capital_limit_for_total_size >= template_channel_size and template_channel_size > 0 and \
                                       magma_specific_config.getboolean(section_name, "template_enabled", fallback=True)

        

        new_pricing = analyze_and_price_offer(market_offers, offer_template_proxy, magma_specific_config)
        if not new_pricing: 
            logging.error(f"Critical: Could not determine pricing for template '{template_name}'. Skipping.")
            actions_summary.append(f"❌ Error pricing {template_name}")
            continue

        found_matching_existing_offer = None
        for existing_offer in my_existing_offers:
            if existing_offer.get("min_size") == template_channel_size and \
               existing_offer.get("min_block_length") == template_duration_blocks:
                found_matching_existing_offer = existing_offer
                managed_offer_ids_this_run.add(existing_offer['id'])
                break
        
        if found_matching_existing_offer:
            offer_id = found_matching_existing_offer['id']
            current_status = found_matching_existing_offer.get("status", "UNKNOWN").upper() # API returns ENABLED/DISABLED
            needs_price_or_total_size_update = (
                found_matching_existing_offer.get("base_fee") != new_pricing["fixed_fee_sats"] or
                found_matching_existing_offer.get("fee_rate") != new_pricing["ppm_fee_rate"] or
                found_matching_existing_offer.get("total_size") != template_capital_limit_for_total_size
            )

            if needs_price_or_total_size_update:
                logging.info(f"Updating details for existing offer ID {offer_id} ('{template_name}'). Current total_size: {found_matching_existing_offer.get('total_size')}, New total_size: {template_capital_limit_for_total_size}")
                update_result = update_magma_offer(offer_id, new_pricing, template_capital_limit_for_total_size, template_name)
                if update_result: actions_summary.append(f"🔄 Updated {template_name} (ID {offer_id}): APR {new_pricing['calculated_apr']}% (F:{new_pricing['fixed_fee_sats']},PPM:{new_pricing['ppm_fee_rate']},TS:{template_capital_limit_for_total_size})")
                else: actions_summary.append(f"❌ Failed update {template_name} (ID {offer_id})")
            else:
                logging.info(f"Offer for template '{template_name}' (ID {offer_id}) pricing and total_size are up-to-date.")

            # Status management: API uses ENABLED/DISABLED
            if template_is_active_by_config and current_status == "DISABLED":
                logging.info(f"Template '{template_name}' (ID {offer_id}) is configured to be active and is currently DISABLED. Attempting to enable.")
                if toggle_magma_offer_status(offer_id, template_name, "ENABLED (enabling due to config/capital)"): actions_summary.append(f"▶️ Enabled {template_name} (ID {offer_id})")
                else: actions_summary.append(f"❌ Failed enable {template_name} (ID {offer_id})")
            elif not template_is_active_by_config and current_status == "ENABLED":
                logging.info(f"Template '{template_name}' (ID {offer_id}) is configured to be inactive (funding/config) and is currently ENABLED. Attempting to disable.")
                if toggle_magma_offer_status(offer_id, template_name, "DISABLED (disabling due to config/capital)"): actions_summary.append(f"⏸️ Disabled {template_name} (ID {offer_id}) - funding/config")
                else: actions_summary.append(f"❌ Failed disable {template_name} (ID {offer_id})")
            elif not needs_price_or_total_size_update : 
                 actions_summary.append(f"ℹ️ No change for {template_name} (ID {offer_id}, Status: {current_status})")

        else: # No existing offer found for this template
            if template_is_active_by_config:
                logging.info(f"Creating new offer for template '{template_name}'. Total capital for this offer: {template_capital_limit_for_total_size}")
                create_result = create_magma_offer(new_pricing, template_capital_limit_for_total_size, template_name)
                if create_result and create_result.get("createOffer"): # createOffer returns the new Offer ID string
                    new_id = create_result["createOffer"]
                    managed_offer_ids_this_run.add(new_id)
                    actions_summary.append(f"🚀 Created {template_name} (New ID {new_id}): APR {new_pricing['calculated_apr']}% (F:{new_pricing['fixed_fee_sats']},PPM:{new_pricing['ppm_fee_rate']},TS:{template_capital_limit_for_total_size}). Attempting to set to DISABLED state.")
                    
                    # Ensure new offer is initially DISABLED as per requirement.
                    # Amboss might create it ENABLED by default. A toggle will flip it.
                    # This assumes createOffer results in ENABLED, so one toggle makes it DISABLED.
                    # If createOffer could result in DISABLED, this logic needs a pre-check of status.
                    logging.info(f"Attempting to immediately toggle new offer {new_id} to ensure it is initially DISABLED.")
                    if toggle_magma_offer_status(new_id, f"{template_name} (post-create toggle to ensure DISABLED)", "DISABLED (initial set)"):
                        logging.info(f"Successfully toggled new offer {new_id} to ensure initial DISABLED state.")
                        actions_summary.append(f" राज्यातील Disabled new {template_name} (ID {new_id}) post-creation.") # A more direct summary
                    else:
                        logging.warning(f"Could not ensure new offer {new_id} is in DISABLED state post-creation via toggle.")
                        actions_summary.append(f"⚠️ Failed to ensure {template_name} (ID {new_id}) is DISABLED post-creation.")
                else: actions_summary.append(f"❌ Failed to create {template_name}")
            else:
                logging.info(f"Skipping creation for '{template_name}': Template not active by config (e.g. capital limit {template_capital_limit_for_total_size} < channel size {template_channel_size}).")
                actions_summary.append(f"⚠️ Skipped create {template_name} (not active by config/capital)")
    
    # Disable orphaned offers
    for existing_offer in my_existing_offers:
        if existing_offer['id'] not in managed_offer_ids_this_run:
            # Check if this orphaned offer is of type CHANNEL and side SELL before attempting to disable
            if existing_offer.get("offer_type") == "CHANNEL" and existing_offer.get("side") == "SELL":
                if existing_offer.get("status", "").upper() == "ENABLED":
                    logging.info(f"Disabling orphaned or unmanaged active CHANNEL/SELL offer ID {existing_offer['id']} (size {existing_offer.get('min_size')}).")
                    if toggle_magma_offer_status(existing_offer['id'], "Orphaned/Unmanaged", "DISABLED (orphaned)"):
                        actions_summary.append(f"👻 Disabled orphaned offer ID {existing_offer['id']}")
                    else:
                        actions_summary.append(f"❌ Failed to disable orphaned offer ID {existing_offer['id']}")
                else:
                    logging.debug(f"Orphaned CHANNEL/SELL offer {existing_offer['id']} is already not ENABLED (Status: {existing_offer.get('status')}). No action needed.")
            else:
                logging.debug(f"Skipping orphaned offer {existing_offer['id']} as it's not a CHANNEL/SELL offer managed by this script (Type: {existing_offer.get('offer_type')}, Side: {existing_offer.get('side')}).")



    if actions_summary:
        summary_message = "Magma AutoPrice Update Summary:\n" + "\n".join(actions_summary)
        summary_message += f"\n\nLND Balance (Excl. Loop): {total_lnd_balance_for_magma:,} sats"
        summary_message += f"\nTotal Capital for Magma (Configured): {capital_for_magma_total_config:,} sats"
        # Sum of 'total_size' isn't explicitly tracked here for final report but individual TCs are logged
        if DRY_RUN_MODE: summary_message = "[DRY RUN] " + summary_message
        send_telegram_notification(summary_message)
    else:
        logging.info("No specific actions taken on Magma offers in this run.")
        notify_on_no_change = not magma_specific_config.getboolean("magma_autoprice", "telegram_notify_on_change_only", fallback=True)
        if notify_on_no_change:
             send_telegram_notification("ℹ️ Magma AutoPrice: No changes made to offers in this run." + (" (DRY RUN)" if DRY_RUN_MODE else ""))

    logging.info("Magma Market Fee Updater script finished.")

if __name__ == "__main__":
    main()