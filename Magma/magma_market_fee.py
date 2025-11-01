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
import logging.handlers
import os
import requests
import subprocess
import telebot
import prettytable  # For dry run summary

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
MY_NODE_PUBKEY = None  # Loaded from general_config [info] NODE

# --- GraphQL Queries/Mutations ---
GET_PUBLIC_MAGMA_OFFERS_QUERY = """
query GetPublicOffers {
  getOffers {
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
      # orders { locked_size } # Not currently used in public market analysis
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
          orders { locked_size } # Crucial for available_size calculation
          # conditions { condition } # Not used
          # seller_score # Not relevant for own offers in this context
          side
          account # Our own pubkey
          # amboss_fee_rate # Not used
          # onchain_multiplier # Not used
          # onchain_priority # Not used
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

    # Get the root logger
    logger = logging.getLogger()
    logger.setLevel(numeric_level)

    # Create formatter
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s"
    )

    # Create handlers
    # Use logging.handlers.RotatingFileHandler
    rfh = logging.handlers.RotatingFileHandler(
        LOG_FILE_PATH, maxBytes=10 * 1024 * 1024, backupCount=5
    )
    rfh.setFormatter(formatter)

    sh = logging.StreamHandler()
    sh.setFormatter(formatter)

    # Add handlers to the root logger
    # Clear existing handlers if any, to avoid duplicate logs on re-runs in some environments
    if logger.hasHandlers():
        logger.handlers.clear()

    logger.addHandler(rfh)
    logger.addHandler(sh)

    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("telebot").setLevel(logging.WARNING)


# --- Telegram Notification ---
def send_telegram_notification(text, level="info"):
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
        logging.warning(
            "Telegram token or chat ID not configured. Skipping notification."
        )
        return
    try:
        bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
        bot.send_message(TELEGRAM_CHAT_ID, text, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Failed to send Telegram message using telebot: {e}")


# --- Amboss API Interaction ---
def _execute_amboss_graphql_request(
    payload: dict, operation_name: str = "AmbossGraphQL"
):
    if not AMBOSS_TOKEN:
        logging.error("Amboss API token not configured.")
        return None

    is_mutation = operation_name.lower().startswith(
        ("create", "update", "delete", "toggle")
    )
    if DRY_RUN_MODE and is_mutation:
        logging.info(
            f"DRY RUN: Preventing API call for {operation_name}. Payload: {json.dumps(payload, indent=2)}"
        )
        if "Create" in operation_name:
            return {"createOffer": f"dry-run-id-for-{operation_name}"}
        if "Update" in operation_name:
            return {
                "updateOfferDetails": {
                    "id": "dry-run-updated-id",
                    "status": "dry_run_simulated_update",
                }
            }
        if "Toggle" in operation_name:
            return {"toggleOffer": True}  # Simulate successful toggle
        return {"dryRunSimulatedSuccess": True}

    url = "https://api.amboss.space/graphql"
    headers = {
        "content-type": "application/json",
        "Authorization": f"Bearer {AMBOSS_TOKEN}",
    }
    logging.debug(
        f"Executing {operation_name} with payload: {json.dumps(payload, indent=2 if logging.getLogger().getEffectiveLevel() == logging.DEBUG else None)}"
    )
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=30)
        response.raise_for_status()
        response_json = response.json()
        if response_json.get("errors"):
            logging.error(
                f"GraphQL errors during {operation_name}: {response_json.get('errors')}"
            )
            return None
        return response_json.get("data")
    except requests.exceptions.Timeout:
        logging.error(f"Timeout during {operation_name} to Amboss.")
        return None
    except requests.exceptions.HTTPError as e:
        logging.error(
            f"HTTP error during {operation_name} to Amboss: {e}. Response: {e.response.text}"
        )
        return None
    except requests.exceptions.RequestException as e:
        logging.error(f"API request error during {operation_name} to Amboss: {e}")
        return None
    except json.JSONDecodeError as e:
        logging.error(
            f"Failed to decode JSON response during {operation_name} from Amboss: {e}"
        )
        return None
    except Exception as e:
        logging.exception(
            f"Unexpected error during _execute_amboss_graphql_request for {operation_name}:"
        )
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
        result = subprocess.run(
            command, capture_output=True, text=True, check=True, timeout=20
        )
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
        logging.exception(
            f"Unexpected error getting LND UTXOs with '{' '.join(command)}':"
        )
        return []

    # Get static loop addresses UTXOs if loop binary is available
    loop_utxos = []
    loop_path = ""
    if (
        "system" in current_general_config
        and "path_command" in current_general_config["system"]
    ):
        loop_command_path = current_general_config["system"]["path_command"]
        if loop_command_path:  # Ensure path_command is not empty
            loop_path = os.path.join(loop_command_path, "loop")

    try:
        if loop_path and os.path.exists(loop_path):
            # Construct the litloop command, using parameters from config or common defaults
            loop_rpc = current_general_config.get(
                "loop", "rpcserver", fallback="localhost:8443"
            )
            loop_tls = current_general_config.get(
                "loop", "tlscertpath", fallback="~/.lit/tls.cert"
            )
            loop_tls_expanded = os.path.expanduser(
                loop_tls
            )  # Expand tilde for tls path

            # Check if tls cert exists, otherwise loop command might hang or error cryptically
            if not os.path.exists(loop_tls_expanded):
                logging.warning(
                    f"Loop TLS cert not found at {loop_tls_expanded}, skipping Loop UTXO check. "
                    f"Please ensure your 'loop' config in config.ini, specifically 'tlscertpath', points to the correct location, "
                    f"or that loopd is set up with a TLS certificate at the default path."
                )
            else:
                # Using shell=True and a single string command as per magma_sale_process.py for robust execution
                litloop_cmd = f"{loop_path} --rpcserver={loop_rpc} --tlscertpath={loop_tls_expanded} static listunspent"
                logging.debug(f"Executing Loop command: {litloop_cmd}")
                process = subprocess.Popen(
                    litloop_cmd,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    # Removed timeout from Popen, it will be in communicate()
                )
                output, error = process.communicate(timeout=20)  # Timeout moved here
                output_decoded = output.decode("utf-8").strip()
                error_decoded = error.decode("utf-8").strip()

                if error_decoded:
                    logging.warning(f"Loop static listunspent stderr: {error_decoded}")

                try:
                    # Robustly handle empty or non-JSON output
                    if output_decoded:
                        loop_data = json.loads(output_decoded)
                        loop_utxos = loop_data.get("utxos", [])
                        logging.info(f"Found {len(loop_utxos)} static loop UTXOs")
                    else:
                        logging.info(
                            "Loop static listunspent command returned empty stdout. Assuming no static UTXOs."
                        )
                        loop_utxos = []  # No UTXOs if output is empty
                except json.JSONDecodeError as e:
                    logging.error(
                        f"Error decoding litloop output: {e}. Raw Output: '{output_decoded}'"
                    )
                    loop_utxos = []  # Default to empty to proceed gracefully
    except subprocess.TimeoutExpired:
        logging.error(f"Timeout executing Loop command: {litloop_cmd}")
        loop_utxos = []
    except (
        FileNotFoundError
    ):  # If loop_exe_path is somehow invalid despite os.path.exists
        logging.error(f"Loop command not found at '{loop_path}' during execution.")
        loop_utxos = []
    except Exception as e:
        logging.exception(
            f"Error checking for loop binary or getting static UTXOs: {e}"
        )
        loop_utxos = []

    # Create a set of loop outpoints for efficient lookup
    loop_outpoints = {utxo.get("outpoint") for utxo in loop_utxos}

    # Filter out UTXOs that are in the loop outpoints set
    filtered_lnd_utxos = [
        utxo for utxo in lnd_utxos if utxo.get("outpoint") not in loop_outpoints
    ]

    # Sort filtered utxos based on amount_sat in reverse order
    filtered_lnd_utxos = sorted(
        filtered_lnd_utxos, key=lambda x: x.get("amount_sat", 0), reverse=True
    )

    logging.debug(
        f"Filtered LND UTXOs (excluding loop static addresses): {json.dumps(filtered_lnd_utxos, indent=2 if logging.getLogger().getEffectiveLevel() == logging.DEBUG else None)}"
    )
    return filtered_lnd_utxos


def get_lnd_onchain_balance(current_general_config):
    """
    Calculates LND on-chain balance available for Magma, excluding Loop-managed UTXOs.
    """

    available_utxos = get_lncli_utxos(current_general_config)
    confirmed_balance = sum(int(utxo.get("amount_sat", 0)) for utxo in available_utxos)
    logging.info(
        f"LND confirmed on-chain balance (excluding Loop UTXOs): {confirmed_balance} sats from {len(available_utxos)} UTXOs."
    )
    return confirmed_balance


# --- Market Analysis & Pricing Logic --- (analyze_and_price_offer and calculate_apr remain largely the same)
def fetch_public_magma_offers(node_pubkey_to_exclude, current_magma_config):
    logging.info("Fetching public Magma sell offers...")
    payload = {"query": GET_PUBLIC_MAGMA_OFFERS_QUERY}
    data = _execute_amboss_graphql_request(payload, "GetPublicOffers")

    processed_offers = []
    if data and data.get("getOffers", {}).get("list"):
        raw_offers = data["getOffers"]["list"]

        # Correctly get min_seller_score_filter from the [magma_autoprice] section
        min_seller_score_filter = get_config_float_with_comment_stripping(
            current_magma_config["magma_autoprice"],
            "min_seller_score_filter",
            fallback=0.0,
        )

        # DEBUG: Log the raw data for verification
        logging.debug(f"Raw offers count: {len(raw_offers)}")
        logging.debug(f"Min seller score filter: {min_seller_score_filter}")

        for offer in raw_offers:
            try:
                if (
                    offer.get("status") != "ENABLED"
                    or offer.get("side") != "SELL"
                    or offer.get("offer_type") != "CHANNEL"
                ):
                    continue
                if (
                    node_pubkey_to_exclude
                    and offer.get("account") == node_pubkey_to_exclude
                ):
                    logging.debug(
                        f"Excluding own offer (Account: {offer.get('account')}) from market analysis."
                    )
                    continue

                parsed_offer = {
                    "id": offer.get("id"),
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
                    "account": offer.get("account"),
                    "node_alias": offer.get("account"),
                }

                # DEBUG: Log each offer's score for verification
                logging.debug(
                    f"Offer {parsed_offer.get('id', 'N/A')}: score={parsed_offer['seller_score']}, base_fee={parsed_offer['base_fee']}, fee_rate={parsed_offer['fee_rate']}"
                )

                if parsed_offer["seller_score"] < min_seller_score_filter:
                    logging.debug(
                        f"Excluding market offer {parsed_offer.get('id','N/A')} due to seller_score {parsed_offer['seller_score']} < {min_seller_score_filter}"
                    )
                    continue

                if not (
                    parsed_offer["min_size"] > 0
                    and parsed_offer["min_block_length"] > 0
                    and parsed_offer["base_fee"] >= 0
                    and parsed_offer["fee_rate"] >= 0
                ):
                    logging.debug(
                        f"Skipping market offer {parsed_offer.get('id', 'N/A')} due to invalid numeric values for APR calc."
                    )
                    continue

                processed_offers.append(parsed_offer)
            except (ValueError, TypeError, KeyError) as e:
                logging.warning(
                    f"Skipping market offer due to parsing error: {offer.get('id', 'N/A')}. Error: {e}. Offer data: {offer}"
                )
                continue

        # DEBUG: Log final filtered results
        logging.debug(f"Final processed offers count: {len(processed_offers)}")
        if processed_offers:
            scores = [offer["seller_score"] for offer in processed_offers]
            fees = [offer["base_fee"] for offer in processed_offers]
            ppm_rates = [offer["fee_rate"] for offer in processed_offers]
            logging.debug(f"Score range: {min(scores)} - {max(scores)}")
            logging.debug(f"Fee range: {min(fees)} - {max(fees)}")
            logging.debug(f"PPM range: {min(ppm_rates)} - {max(ppm_rates)}")

        logging.info(
            f"Fetched and processed {len(processed_offers)} relevant public Magma CHANNEL/SELL/ENABLED offers (excluding own, score >= {min_seller_score_filter})."
        )
        return processed_offers
    else:
        logging.warning("No public Magma offers found or error in fetching.")
        return []


def calculate_apr(fixed_fee_sats, ppm_fee_rate, channel_size_sats, duration_days_float):
    if channel_size_sats == 0 or duration_days_float == 0:
        return 0.0
    variable_fee_sats = (ppm_fee_rate / 1_000_000) * channel_size_sats
    total_fee_sats = fixed_fee_sats + variable_fee_sats
    apr = (total_fee_sats / channel_size_sats) * (365.0 / duration_days_float) * 100
    return round(apr, 2)


def analyze_and_price_offer(
    template_name,
    our_offer_template_config_proxy,
    globally_relevant_public_offers,
    config_magma,
    existing_offer_details=None,
):
    logging.info(f"Analyzing market for offer template: {template_name}")

    # Use helper for comment stripping
    template_channel_size = get_config_int_with_comment_stripping(
        our_offer_template_config_proxy, "channel_size_sats"
    )
    template_duration_days = get_config_int_with_comment_stripping(
        our_offer_template_config_proxy, "duration_days"
    )
    template_min_fixed_fee = get_config_int_with_comment_stripping(
        our_offer_template_config_proxy, "min_fixed_fee_sats"
    )
    template_max_fixed_fee = get_config_int_with_comment_stripping(
        our_offer_template_config_proxy, "max_fixed_fee_sats"
    )
    template_min_ppm = get_config_int_with_comment_stripping(
        our_offer_template_config_proxy, "min_ppm_fee"
    )
    template_max_ppm = get_config_int_with_comment_stripping(
        our_offer_template_config_proxy, "max_ppm_fee"
    )
    # target_apr_min and max already use the float helper, which is good.
    template_target_apr_min = get_config_float_with_comment_stripping(
        our_offer_template_config_proxy, "target_apr_min", fallback=0.0
    )  # Use fallback
    template_target_apr_max = get_config_float_with_comment_stripping(
        our_offer_template_config_proxy, "target_apr_max", fallback=100.0
    )  # Use fallback

    # This one is for a key directly in [magma_autoprice], not the template-specific proxy, so it's different
    global_min_ppm_fee_config = get_config_int_with_comment_stripping(
        config_magma, "magma_autoprice", "global_min_ppm_fee", fallback=0
    )  # Use fallback

    # --- Market Analysis ---
    # 1. Filter globally_relevant_public_offers further if needed (e.g., by score percentile)
    min_score_filter_cfg = get_config_float_with_comment_stripping(
        config_magma, "magma_autoprice", "min_seller_score_filter", fallback=0.0
    )  # Use fallback

    # Filter by score first
    high_score_offers = [
        offer
        for offer in globally_relevant_public_offers
        if offer.get("seller_score", 0.0) >= min_score_filter_cfg
    ]
    high_score_offers.sort(key=lambda x: x.get("seller_score", 0.0), reverse=True)
    logging.info(
        f"Template {template_name}: Found {len(high_score_offers)} offers with seller score >= {min_score_filter_cfg}."
    )

    # The user's request is to consider ALL offers with seller score >= min_score_filter_cfg
    # for the pricing pool, instead of further selecting a top N percentile by score.
    pricing_pool = high_score_offers
    logging.info(
        f"Template {template_name}: Using {len(pricing_pool)} offers (all with seller score >= {min_score_filter_cfg}) as the pricing pool."
    )

    # --- Pricing Logic ---
    proposed_fixed_fee = template_min_fixed_fee
    proposed_ppm_fee = template_min_ppm
    benchmark_source_info = "Fallback: Template Minima"  # Default if no market data
    raw_market_fixed_str = "-"
    raw_market_ppm_str = "-"

    if pricing_pool:
        # Sort by PPM first (lower is better), then by Fixed Fee (lower is better)
        # This defines "competitiveness" within the pricing pool
        pricing_pool.sort(
            key=lambda x: (int(x.get("fee_rate", 0)), int(x.get("base_fee", 0)))
        )

        pricing_strategy_percentile = get_config_int_with_comment_stripping(
            config_magma,
            "magma_autoprice",
            "pricing_strategy_percentile",
            fallback=10,
        )
        pricing_strategy_position_offset = get_config_int_with_comment_stripping(
            config_magma,
            "magma_autoprice",
            "pricing_strategy_position_offset",
            fallback=1,
        )

        # Determine target index based on percentile and offset
        target_idx_float = (len(pricing_pool) - 1) * (
            pricing_strategy_percentile / 100.0
        )
        target_idx = int(round(target_idx_float))  # Round to nearest offer

        # Apply offset: make it slightly less competitive (higher index)
        # The goal is to be in the top X percentile, but not necessarily the absolute best if offset is > 0
        # If percentile is 10th (aiming for better than 90%), and list has 10 items, index could be 0 or 1.
        # Offset makes it e.g. 1 or 2.
        target_idx = min(
            len(pricing_pool) - 1, target_idx + pricing_strategy_position_offset - 1
        )  # -1 because offset is 1-based
        target_idx = max(0, target_idx)  # Ensure it's not negative

        logging.info(
            f"Template {template_name}: Competitive pool size: {len(pricing_pool)}. Target Idx for pricing (0-based): {target_idx} (Percentile: {pricing_strategy_percentile}%, Offset: {pricing_strategy_position_offset})"
        )

        target_offer = pricing_pool[target_idx]

        raw_market_fixed = int(target_offer.get("base_fee", template_min_fixed_fee))
        raw_market_ppm = int(target_offer.get("fee_rate", template_min_ppm))
        raw_market_fixed_str = str(raw_market_fixed)
        raw_market_ppm_str = str(raw_market_ppm)

        # Our proposed fee is based on this target offer
        proposed_fixed_fee = raw_market_fixed
        proposed_ppm_fee = raw_market_ppm

        benchmark_source_info = f"Market @ P{pricing_strategy_percentile}(+{pricing_strategy_position_offset-1}) of {len(pricing_pool)} peers"
        logging.info(
            f"Template {template_name}: Using market rates from peer (Fixed: {raw_market_fixed}, PPM: {raw_market_ppm}). Source: {benchmark_source_info}"
        )

    else:
        logging.warning(
            f"Template {template_name}: No offers found in the pricing pool (after seller score filter). "
            f"Proceeding with fallback pricing."
        )
        benchmark_source_info = f"Fallback: No Score-Filtered Offers"
        # Fallback to template minimums
        proposed_fixed_fee = template_min_fixed_fee
        proposed_ppm_fee = template_min_ppm
        logging.info(
            f"Template {template_name}: Using template minimums due to: {benchmark_source_info}"
        )

    # Apply template and global boundaries
    original_proposed_ppm = proposed_ppm_fee
    proposed_fixed_fee = max(
        template_min_fixed_fee, min(proposed_fixed_fee, template_max_fixed_fee)
    )
    proposed_ppm_fee = max(template_min_ppm, min(proposed_ppm_fee, template_max_ppm))

    if proposed_ppm_fee != original_proposed_ppm:
        logging.info(
            f"Template {template_name}: PPM fee {original_proposed_ppm} (derived from {benchmark_source_info}) clamped to {proposed_ppm_fee} by template limits (Min:{template_min_ppm}/Max:{template_max_ppm})."
        )

    # Apply global minimum PPM if it's higher
    if global_min_ppm_fee_config > proposed_ppm_fee:
        logging.info(
            f"Template {template_name}: PPM fee {proposed_ppm_fee} (post-template clamp) further adjusted to {global_min_ppm_fee_config} by global_min_ppm_fee {global_min_ppm_fee_config}."
        )
        proposed_ppm_fee = global_min_ppm_fee_config

    calculated_apr = calculate_apr(
        proposed_fixed_fee,  # Correct: fixed_fee_sats
        proposed_ppm_fee,  # Correct: ppm_fee_rate
        template_channel_size,  # Correct: channel_size_sats
        template_duration_days,  # Correct: duration_days_float
    )

    if (
        not (template_target_apr_min <= calculated_apr <= template_target_apr_max)
        and template_target_apr_min > 0
    ):  # Only warn if min APR is set
        logging.warning(
            f"Calculated APR {calculated_apr:.2f}% for template '{template_name}' is outside its target range "
            f"({template_target_apr_min}% - {template_target_apr_max}%). Final values: Fixed={proposed_fixed_fee}, PPM={proposed_ppm_fee}."
        )

    result = {
        "channel_size_sats": template_channel_size,
        "duration_days": template_duration_days,
        "fixed_fee_sats": proposed_fixed_fee,
        "ppm_fee_rate": proposed_ppm_fee,
        "calculated_apr": float(
            f"{calculated_apr:.2f}"
        ),  # Store as float with 2 decimal precision
        "benchmark_source": benchmark_source_info,
        "raw_market_fixed": raw_market_fixed_str,
        "raw_market_ppm": raw_market_ppm_str,
    }
    logging.info(
        f"Determined pricing for {template_name}: Fixed={result['fixed_fee_sats']}, PPM={result['ppm_fee_rate']}, APR={result['calculated_apr']:.2f}%. Source: {result['benchmark_source']}. RawMkt F:{result['raw_market_fixed']}, P:{result['raw_market_ppm']}"
    )
    return result


# --- Manage Our Offers on Amboss ---
def fetch_my_current_offers():
    logging.info("Fetching my current Magma sell offers...")
    payload = {"query": GET_MY_MAGMA_OFFERS_QUERY}
    data = _execute_amboss_graphql_request(payload, "MyOffers")

    processed_offers = []
    if data and data.get("getUser", {}).get("market", {}).get("offers", {}).get("list"):
        my_raw_offers = data["getUser"]["market"]["offers"]["list"]
        for offer_item in my_raw_offers:
            try:
                if not offer_item:
                    continue
                offer_id = offer_item.get("id", "N/A_ID")
                logging.debug(
                    f"Processing own offer item ID {offer_id}: {json.dumps(offer_item)}"
                )

                if (
                    offer_item.get("offer_type") != "CHANNEL"
                    or offer_item.get("side") != "SELL"
                ):
                    logging.debug(
                        f"Skipping own offer {offer_id} - not a CHANNEL sell offer. Type: {offer_item.get('offer_type')}, Side: {offer_item.get('side')}"
                    )
                    continue

                total_size_sats = int(offer_item.get("total_size", 0))

                orders_data = offer_item.get("orders")
                locked_size_str = "0"
                if orders_data and isinstance(orders_data, dict):
                    locked_size_str = orders_data.get("locked_size", "0")
                elif (
                    orders_data is not None
                ):  # orders field exists but not a dict, log warning
                    logging.warning(
                        f"Offer ID {offer_id} has 'orders' field but it's not a dictionary: {orders_data}. Defaulting locked_size to 0."
                    )

                locked_size_sats = int(
                    locked_size_str if locked_size_str is not None else "0"
                )  # Ensure int conversion
                available_size_sats = total_size_sats - locked_size_sats

                logging.debug(
                    f"Offer ID {offer_id}: total_size={total_size_sats}, orders_data={orders_data}, parsed_locked_size_str='{locked_size_str}', locked_size_sats={locked_size_sats}, calculated_available_size={available_size_sats}"
                )

                current_fixed_fee = int(offer_item.get("base_fee", 0))
                current_ppm_rate = int(offer_item.get("fee_rate", 0))
                current_min_size = int(offer_item.get("min_size", 0))
                current_duration_blocks = int(offer_item.get("min_block_length", 0))
                current_duration_days = (
                    current_duration_blocks / BLOCKS_PER_DAY
                    if BLOCKS_PER_DAY > 0
                    else 0
                )
                current_apr = calculate_apr(
                    current_fixed_fee,
                    current_ppm_rate,
                    current_min_size,
                    current_duration_days,
                )

                details = {
                    "id": offer_id,
                    "status": offer_item.get("status", "UNKNOWN").upper(),
                    "offer_type": offer_item.get("offer_type"),
                    "base_fee": current_fixed_fee,
                    "fee_rate": current_ppm_rate,
                    "max_size": int(offer_item.get("max_size", 0)),
                    "min_block_length": current_duration_blocks,
                    "min_size": current_min_size,
                    "total_size": total_size_sats,
                    "locked_size": locked_size_sats,
                    "available_size": available_size_sats,
                    "side": offer_item.get("side"),
                    "account": offer_item.get("account"),
                    "duration_days": current_duration_days,  # Store for display
                    "apr": current_apr,  # Store for display
                }
                if not (
                    details["id"] != "N/A_ID"
                    and details["status"] != "UNKNOWN"
                    and details["min_size"] is not None
                    and details["min_block_length"] is not None
                ):
                    logging.warning(
                        f"Own offer {offer_id} missing essential fields for processing. Skipping. Data: {offer_item}"
                    )
                    continue
                processed_offers.append(details)
            except (ValueError, TypeError, KeyError) as e:
                logging.warning(
                    f"Error parsing own offer {offer_id}: {e}. Offer data: {offer_item}"
                )
                continue
        logging.info(
            f"Found and processed {len(processed_offers)} existing Magma CHANNEL sell offers."
        )
        return processed_offers
    logging.warning("No existing Magma sell offers found or error fetching.")
    return []


def create_magma_offer(pricing_details, template_capital_for_total_size, template_name):
    duration_blocks = pricing_details["duration_days"] * BLOCKS_PER_DAY
    amboss_offer_input = {
        "base_fee": pricing_details["fixed_fee_sats"],
        "fee_rate": pricing_details["ppm_fee_rate"],
        "min_size": pricing_details["channel_size_sats"],
        "max_size": pricing_details["channel_size_sats"],
        "min_block_length": duration_blocks,
        "total_size": template_capital_for_total_size,
        "base_fee_cap": pricing_details["fixed_fee_sats"],
        "fee_rate_cap": pricing_details["ppm_fee_rate"],
    }
    log_prefix = "DRY RUN: Would create" if DRY_RUN_MODE else "Creating"
    logging.info(
        f"{log_prefix} new Magma offer for template '{template_name}' with: {amboss_offer_input}"
    )

    if DRY_RUN_MODE:
        # Simulate the structure Amboss might return for createOffer, including a simulated ID.
        return {"createOffer": f"dryrun-offer-id-{template_name.replace(' ', '_')}"}

    payload = {
        "query": CREATE_MAGMA_OFFER_MUTATION,
        "variables": {"input": amboss_offer_input},
    }
    data = _execute_amboss_graphql_request(payload, f"CreateMagmaOffer-{template_name}")
    if data and data.get(
        "createOffer"
    ):  # createOffer returns the new Offer ID (String)
        new_offer_id = data.get("createOffer")
        logging.info(
            f"Successfully created Magma offer for '{template_name}'. New Offer ID: {new_offer_id}"
        )
        return {
            "id": new_offer_id,
            "status_after_create": "ACTIVE",
        }  # Assume ACTIVE, will be toggled
    else:
        logging.error(
            f"Failed to create Magma offer for '{template_name}'. Response: {data}"
        )
        return None


def update_magma_offer(
    offer_id_to_update, pricing_details, template_capital_for_total_size, template_name
):
    duration_blocks = pricing_details["duration_days"] * BLOCKS_PER_DAY
    amboss_offer_input = {
        "base_fee": pricing_details["fixed_fee_sats"],
        "fee_rate": pricing_details["ppm_fee_rate"],
        "min_size": pricing_details["channel_size_sats"],
        "max_size": pricing_details[
            "channel_size_sats"
        ],  # Assuming fixed size for our offers
        "min_block_length": duration_blocks,
        "total_size": template_capital_for_total_size,
        # base_fee_cap and fee_rate_cap are part of CreateOfferInput but optional in UpdateOfferDetailsInput
        # If you want to update them, add them here. For now, matching previous behavior.
    }
    log_prefix = "DRY RUN: Would update" if DRY_RUN_MODE else "Updating"
    logging.info(
        f"{log_prefix} Magma offer ID {offer_id_to_update} for template '{template_name}' with: {amboss_offer_input}"
    )

    if DRY_RUN_MODE:
        # Simulate the structure of the returned Offer object after update
        return {
            "id": offer_id_to_update,
            **amboss_offer_input,
            "status": "DRY_RUN_STATUS_POST_UPDATE",
        }

    payload = {
        "query": UPDATE_MAGMA_OFFER_MUTATION,
        "variables": {"id": offer_id_to_update, "input": amboss_offer_input},
    }
    data = _execute_amboss_graphql_request(
        payload, f"UpdateMagmaOffer-{offer_id_to_update}"
    )
    if data and data.get("updateOfferDetails"):
        logging.info(f"Successfully updated Magma offer ID {offer_id_to_update}.")
        return data.get("updateOfferDetails")  # This is the Offer object
    else:
        logging.error(
            f"Failed to update Magma offer ID {offer_id_to_update}. Response: {data}"
        )
        return None


def toggle_magma_offer_status(
    offer_id, template_name_logging_info, target_log_status_str
):
    log_prefix = "DRY RUN: Would toggle" if DRY_RUN_MODE else "Toggling"
    # target_log_status_str is for logging the intent, actual toggle flips the state
    logging.info(
        f"{log_prefix} Magma offer ID {offer_id} (info: '{template_name_logging_info}'). Intent from script: {target_log_status_str}."
    )

    if DRY_RUN_MODE:
        logging.info(f"DRY RUN: Simulating toggle for {offer_id} successful.")
        return True  # Simulate success

    payload = {
        "query": TOGGLE_MAGMA_OFFER_MUTATION,
        "variables": {"toggleOfferId": offer_id},
    }
    data = _execute_amboss_graphql_request(payload, f"ToggleMagmaOffer-{offer_id}")
    if data and data.get("toggleOffer") is True:  # toggleOffer returns Boolean
        logging.info(f"Successfully toggled status for Magma offer ID {offer_id}.")
        return True
    else:
        logging.error(
            f"Failed to toggle status for Magma offer ID {offer_id}. Response: {data}"
        )
        return False


# --- Main Application Logic ---
def main():
    global general_config, magma_specific_config, AMBOSS_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DRY_RUN_MODE, LNCLI_PATH, MY_NODE_PUBKEY

    HELP_TEXT = """
    Amboss Magma Auto-Pricing Script.
    ---------------------------------
    This script automates pricing for Amboss Magma channel sell offers.
    It fetches market data, analyzes it based on your configuration,
    and can create, update, enable, or disable your offers.

    Features:
    - Reads settings from ../config.ini and Magma/magma_config.ini.
    - Dual percentile pricing: targets top sellers by score, then their prices.
    - Excludes own offers from market analysis.
    - Manages capital allocation based on LND balance and offer shares.
    - Provides Telegram notifications and detailed logging.

    Disclaimer:
    ---------------------------------
    USE THIS SCRIPT AT YOUR OWN RISK.
    Automated trading and offer management involve financial risk.
    The author(s) of this script hold NO RESPONSIBILITY for any financial
    losses, incorrect offer placements, or any other issues that may arise
    from its use.

    ALWAYS run with --dry-run first to simulate actions and verify behavior
    against your expectations and configuration. Review logs carefully.
    Ensure your configuration files (../config.ini, Magma/magma_config.ini)
    are correctly set up before running live.
    """

    parser = argparse.ArgumentParser(
        description="Amboss Magma Auto-Pricing Script.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HELP_TEXT,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate actions without making live API changes. Fetches real LND balance and market data, but does not execute Amboss mutations.",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Force execution without interactive confirmation. Use with caution, intended for automated execution (e.g., systemd).",
    )
    args = parser.parse_args()
    DRY_RUN_MODE = args.dry_run

    if not os.path.exists(GENERAL_CONFIG_FILE_PATH):
        print(
            f"CRITICAL: General configuration file {GENERAL_CONFIG_FILE_PATH} not found. Exiting."
        )
        # No logging before setup_logging is called
        return
    general_config.read(GENERAL_CONFIG_FILE_PATH)
    # Logging setup MUST happen after general_config is read, but before any critical logging.
    setup_logging()

    if not os.path.exists(MAGMA_CONFIG_FILE_PATH):
        logging.critical(
            f"CRITICAL: Magma-specific configuration file {MAGMA_CONFIG_FILE_PATH} not found. Exiting."
        )
        return
    magma_specific_config.read(MAGMA_CONFIG_FILE_PATH)

    # Confirmation for LIVE mode
    if not DRY_RUN_MODE and not args.force:
        logging.warning(
            "LIVE MODE: Script will attempt to make actual changes to Amboss Magma offers."
        )
        print("\n" + "=" * 70)
        print("CONFIRMATION REQUIRED TO PROCEED IN LIVE MODE")
        print("=" * 70)
        print("This script will attempt to: ")
        print("- Fetch your LND on-chain balance.")
        print("- Fetch public Amboss Magma offers and your own offers.")
        print("- Analyze market data based on your configuration in:")
        print(f"  - {GENERAL_CONFIG_FILE_PATH}")
        print(f"  - {MAGMA_CONFIG_FILE_PATH}")
        print(
            "- Potentially CREATE, UPDATE, ENABLE, or DISABLE your Amboss Magma offers."
        )
        print(
            "\nReview your configurations and understand the script's actions before proceeding."
        )
        print(
            "It is STRONGLY recommended to run with '--dry-run' first if you haven't."
        )
        print("=" * 70)
        confirm = input(
            "Type 'yes' to proceed with LIVE execution, or anything else to abort: "
        )
        if confirm.lower() != "yes":
            logging.info("User aborted LIVE execution.")
            print("Aborted by user.")
            return
        logging.info("User confirmed LIVE execution.")
    elif DRY_RUN_MODE:
        logging.info(
            "DRY RUN MODE ENABLED. Real LND balance and market data will be fetched. No actual changes will be made to Amboss API (mutations)."
        )
    elif args.force:  # Live mode with --force
        logging.info(
            "--force flag used, proceeding with LIVE execution without interactive confirmation."
        )

    # This log will only go to file if console is for tables in dry_run
    logging.info("Starting Magma Market Fee Updater script.")

    AMBOSS_TOKEN = general_config.get(
        "credentials", "amboss_authorization", fallback=None
    )
    TELEGRAM_BOT_TOKEN = general_config.get(
        "telegram",
        "magma_bot_token",
        fallback=general_config.get("telegram", "telegram_bot_token", fallback=None),
    )
    TELEGRAM_CHAT_ID = general_config.get("telegram", "telegram_user_id", fallback=None)
    LNCLI_PATH = general_config.get("paths", "lncli_path", fallback="lncli")
    MY_NODE_PUBKEY = general_config.get("info", "NODE", fallback=None)

    if not MY_NODE_PUBKEY:
        logging.warning(
            "Node pubkey (info.NODE) not found in general config.ini. Cannot exclude own offers from market analysis."
        )

    if not magma_specific_config.getboolean(
        "magma_autoprice", "enabled", fallback=False
    ):
        logging.info("Magma autopricing is disabled in Magma config.ini. Exiting.")
        send_telegram_notification(
            "ℹ️ Magma AutoPrice: Script ran but autopricing is disabled in magma_config.ini. No actions taken.",
            level="warning",
        )
        return

    if not AMBOSS_TOKEN:
        logging.critical(
            "Amboss API token (amboss_authorization) not found in general config.ini. Exiting."
        )
        send_telegram_notification(
            "☠️ Magma AutoPrice CRITICAL: Amboss API token not configured.",
            level="error",
        )
        return

    # --- Data Fetching ---
    total_lnd_balance_for_magma = get_lnd_onchain_balance(general_config)
    fraction_for_sale = get_config_float_with_comment_stripping(
        magma_specific_config["magma_autoprice"],
        "lnd_balance_fraction_for_sale",
        fallback=0.0,
    )
    capital_for_magma_total_config = int(
        total_lnd_balance_for_magma * fraction_for_sale
    )

    market_offers = fetch_public_magma_offers(MY_NODE_PUBKEY, magma_specific_config)
    if market_offers is None:
        market_offers = []

    my_existing_offers_raw = fetch_my_current_offers()

    offer_template_names = magma_specific_config.get(
        "magma_autoprice", "our_offer_ids", fallback=""
    ).split(",")
    offer_template_names = [
        name.strip() for name in offer_template_names if name.strip()
    ]

    if not offer_template_names:
        logging.warning(
            "No offer templates defined in [magma_autoprice] our_offer_ids in Magma config. Exiting."
        )
        send_telegram_notification(
            "⚠️ Magma AutoPrice: No offer templates (our_offer_ids) defined in magma_config.ini. No actions taken.",
            level="warning",
        )
        return

    actions_summary_for_telegram = []
    managed_offer_ids_this_run = set()

    dry_run_lnd_capital_summary_data = []
    dry_run_key_config_summary_data = []
    dry_run_existing_offers_summary_data = []
    dry_run_proposed_actions_data = []
    dry_run_orphaned_actions_data = []

    if DRY_RUN_MODE:
        dry_run_lnd_capital_summary_data.append(
            {
                "Metric": "LND On-Chain Balance (Excl. Loop)",
                "Value": f"{total_lnd_balance_for_magma:,} sats",
            }
        )
        dry_run_lnd_capital_summary_data.append(
            {
                "Metric": "Configured Fraction for Sale",
                "Value": f"{fraction_for_sale*100:.2f}%",
            }
        )
        dry_run_lnd_capital_summary_data.append(
            {
                "Metric": "Total Capital for Magma Offers",
                "Value": f"{capital_for_magma_total_config:,} sats",
            }
        )

        config_keys_to_show = [
            "seller_score_percentile_target",
            "pricing_strategy_percentile",
            "pricing_strategy_position_offset",
            "min_seller_score_filter",
            "size_similarity_threshold",
            "duration_similarity_threshold",
            "global_min_ppm_fee",
        ]
        for key in config_keys_to_show:
            dry_run_key_config_summary_data.append(
                {
                    "Setting": f"[magma_autoprice] {key}",
                    "Value": magma_specific_config.get(
                        "magma_autoprice", key, fallback="N/A"
                    ),
                }
            )

        for offer in my_existing_offers_raw:
            dry_run_existing_offers_summary_data.append(
                {
                    "Offer ID": offer["id"],
                    "Status": offer["status"],
                    "Min Size": f"{offer['min_size']:,}",
                    "Total Size": f"{offer['total_size']:,}",
                    "Avail. Size": f"{offer['available_size']:,}",
                    "Duration (Days)": f"{offer['duration_days']:.1f}",
                    "Fixed": offer["base_fee"],
                    "PPM": offer["fee_rate"],
                    "APR (%)": offer["apr"],
                }
            )

    # --- Main Processing Loop for Templates ---
    for template_name in offer_template_names:
        section_name = f"magma_offer_{template_name}"
        if not magma_specific_config.has_section(section_name):
            logging.warning(
                f"Magma configuration section [{section_name}] not found. Skipping."
            )
            actions_summary_for_telegram.append(
                f"❓ Missing config for template {template_name}"
            )
            if DRY_RUN_MODE:
                dry_run_proposed_actions_data.append(
                    {
                        "Template": template_name,
                        "Action": "ERROR",
                        "Reason": "Missing config section",
                        "Cur Fixed": "-",
                        "Cur PPM": "-",
                        "Cur APR": "-",
                        "Benchmark Source": "N/A",
                        "Mkt Fixed (Raw)": "-",
                        "Mkt PPM (Raw)": "-",
                        "Prop Fixed": "-",
                        "Prop PPM": "-",
                        "Prop APR": "-",
                        "Prop Capital": "-",
                        "Projected Status": "ERROR",
                    }
                )
            continue

        offer_template_proxy = magma_specific_config[section_name]
        template_channel_size = get_config_int_with_comment_stripping(
            offer_template_proxy, "channel_size_sats"
        )
        template_duration_days = get_config_int_with_comment_stripping(
            offer_template_proxy, "duration_days"
        )
        template_duration_blocks = template_duration_days * BLOCKS_PER_DAY
        share = get_config_float_with_comment_stripping(
            offer_template_proxy, "capital_allocation_share", fallback=0.0
        )
        template_capital_limit_for_total_size = int(
            capital_for_magma_total_config * share
        )
        template_enabled_by_config_file = magma_specific_config.getboolean(
            section_name, "template_enabled", fallback=True
        )

        template_is_active_by_config = (
            template_capital_limit_for_total_size >= template_channel_size
            and template_channel_size > 0
            and template_enabled_by_config_file
        )

        new_pricing = analyze_and_price_offer(
            template_name, offer_template_proxy, market_offers, magma_specific_config
        )
        if not new_pricing:
            logging.error(
                f"Critical: Could not determine pricing for template '{template_name}'. Skipping."
            )
            actions_summary_for_telegram.append(f"❌ Error pricing {template_name}")
            if DRY_RUN_MODE:
                dry_run_proposed_actions_data.append(
                    {
                        "Template": template_name,
                        "Action": "ERROR",
                        "Reason": "Pricing analysis failed",
                        "Cur Fixed": "-",
                        "Cur PPM": "-",
                        "Cur APR": "-",
                        "Benchmark Source": (
                            new_pricing.get("benchmark_source", "N/A")
                            if new_pricing
                            else "N/A"
                        ),  # Safely get, though new_pricing is None here
                        "Mkt Fixed (Raw)": (
                            new_pricing.get("raw_market_fixed", "-")
                            if new_pricing
                            else "-"
                        ),
                        "Mkt PPM (Raw)": (
                            new_pricing.get("raw_market_ppm", "-")
                            if new_pricing
                            else "-"
                        ),
                        "Prop Fixed": "-",
                        "Prop PPM": "-",
                        "Prop APR": "-",
                        "Prop Capital": f"{template_capital_limit_for_total_size:,}",
                        "Projected Status": "ERROR",
                    }
                )
            continue

        prop_fixed_val = new_pricing["fixed_fee_sats"]
        prop_ppm_val = new_pricing["ppm_fee_rate"]
        prop_apr_val = new_pricing["calculated_apr"]
        benchmark_source_val = new_pricing["benchmark_source"]
        mkt_fixed_raw_val = new_pricing["raw_market_fixed"]
        mkt_ppm_raw_val = new_pricing["raw_market_ppm"]
        prop_capital_val = f"{template_capital_limit_for_total_size:,}"

        found_matching_existing_offer = None
        for existing_offer in my_existing_offers_raw:
            if (
                existing_offer.get("min_size") == template_channel_size
                and existing_offer.get("min_block_length") == template_duration_blocks
            ):
                found_matching_existing_offer = existing_offer
                managed_offer_ids_this_run.add(existing_offer["id"])
                break

        current_fixed_val = "-"
        current_ppm_val = "-"
        current_apr_val = "-"
        # current_total_capital_val = "-" # This was commented, but it's fine as it's defined in the if block

        action_log = ""
        reason_log = ""
        projected_status_val = "N/A"  # Default, will be updated

        if found_matching_existing_offer:
            offer_id = found_matching_existing_offer["id"]
            current_status = found_matching_existing_offer.get(
                "status", "UNKNOWN"
            ).upper()
            projected_status_val = current_status  # Initialize with current status

            current_fixed_val = found_matching_existing_offer.get("base_fee", "-")
            current_ppm_val = found_matching_existing_offer.get("fee_rate", "-")
            current_apr_val = found_matching_existing_offer.get("apr", "-")
            # current_total_capital_val = f"{found_matching_existing_offer.get('total_size', '-'):,}" # Already part of dry run existing offers table

            needs_price_or_total_size_update = (
                found_matching_existing_offer.get("base_fee")
                != new_pricing["fixed_fee_sats"]
                or found_matching_existing_offer.get("fee_rate")
                != new_pricing["ppm_fee_rate"]
                or found_matching_existing_offer.get("total_size")
                != template_capital_limit_for_total_size
            )

            action_taken_this_template = False  # Flag to check if any primary action occurred for combined logging

            if needs_price_or_total_size_update:
                action_log = "UPDATE"
                reason_log = f"Price/Cap Update. New Cap: {prop_capital_val}"
                logging.info(
                    f"Updating details for existing offer ID {offer_id} ('{template_name}'). Current total_size: {found_matching_existing_offer.get('total_size')}, New total_size: {template_capital_limit_for_total_size}"
                )
                update_result = update_magma_offer(
                    offer_id,
                    new_pricing,
                    template_capital_limit_for_total_size,
                    template_name,
                )
                if update_result:
                    actions_summary_for_telegram.append(
                        f"🔄 Updated {template_name} (ID {offer_id[:8]}): APR {prop_apr_val}% (F:{prop_fixed_val},PPM:{prop_ppm_val},TS:{prop_capital_val})"
                    )
                else:
                    actions_summary_for_telegram.append(
                        f"❌ Failed update {template_name} (ID {offer_id[:8]})"
                    )
                    action_log = "UPDATE_FAIL"  # Overwrite action_log
                    projected_status_val = "UPDATE_FAIL"  # Status reflects failure
                action_taken_this_template = True

            if template_is_active_by_config and current_status == "DISABLED":
                if action_log:
                    action_log += "/"  # Combine actions like "UPDATE/ENABLE"
                action_log += "ENABLE"
                reason_log_segment = "Config active, offer DISABLED"
                reason_log = (
                    f"{reason_log}; {reason_log_segment}"
                    if reason_log
                    else reason_log_segment
                )

                logging.info(
                    f"Template '{template_name}' (ID {offer_id}) is configured to be active and is currently DISABLED. Attempting to enable."
                )
                if toggle_magma_offer_status(
                    offer_id, template_name, "ENABLED (enabling due to config/capital)"
                ):
                    actions_summary_for_telegram.append(
                        f"▶️ Enabled {template_name} (ID {offer_id[:8]})"
                    )
                    projected_status_val = "ENABLED"  # Update projected status
                else:
                    actions_summary_for_telegram.append(
                        f"❌ Failed enable {template_name} (ID {offer_id[:8]})"
                    )
                    projected_status_val = (
                        projected_status_val + "+ENABLE_FAIL"
                        if projected_status_val not in ["UPDATE_FAIL", "ENABLED"]
                        else "ENABLE_FAIL"
                    )
                    if "_FAIL" not in action_log:
                        action_log += "_FAIL"
                action_taken_this_template = True
            elif not template_is_active_by_config and current_status == "ENABLED":
                if action_log:
                    action_log += "/"
                action_log += "DISABLE"
                reason_log_segment = "Config INACTIVE, offer ENABLED"
                reason_log = (
                    f"{reason_log}; {reason_log_segment}"
                    if reason_log
                    else reason_log_segment
                )

                logging.info(
                    f"Template '{template_name}' (ID {offer_id}) is configured to be inactive (funding/config) and is currently ENABLED. Attempting to disable."
                )
                if toggle_magma_offer_status(
                    offer_id,
                    template_name,
                    "DISABLED (disabling due to config/capital)",
                ):
                    actions_summary_for_telegram.append(
                        f"⏸️ Disabled {template_name} (ID {offer_id[:8]}) - funding/config"
                    )
                    projected_status_val = "DISABLED"
                else:
                    actions_summary_for_telegram.append(
                        f"❌ Failed disable {template_name} (ID {offer_id[:8]})"
                    )
                    projected_status_val = (
                        projected_status_val + "+DISABLE_FAIL"
                        if projected_status_val not in ["UPDATE_FAIL", "DISABLED"]
                        else "DISABLE_FAIL"
                    )
                    if "_FAIL" not in action_log:
                        action_log += "_FAIL"
                action_taken_this_template = True

            if not action_taken_this_template:
                action_log = "NO_CHANGE"
                reason_log = "Price & Status OK"
                actions_summary_for_telegram.append(
                    f"ℹ️ No change for {template_name} (ID {offer_id[:8]}, Status: {current_status})"
                )
                projected_status_val = current_status

            if (
                DRY_RUN_MODE
            ):  # This append is for the 'if found_matching_existing_offer:' block
                dry_run_proposed_actions_data.append(
                    {
                        "Template": template_name,
                        "Action": action_log if action_log else "NO_ACTION_LOGGED",
                        "Reason": reason_log.strip("; "),
                        "Cur Fixed": current_fixed_val,
                        "Cur PPM": current_ppm_val,
                        "Cur APR": current_apr_val,  # Corrected: Use _val
                        "Benchmark Source": benchmark_source_val,
                        "Mkt Fixed (Raw)": mkt_fixed_raw_val,
                        "Mkt PPM (Raw)": mkt_ppm_raw_val,
                        "Prop Fixed": prop_fixed_val,
                        "Prop PPM": prop_ppm_val,
                        "Prop APR": prop_apr_val,
                        "Prop Capital": prop_capital_val,
                        "Projected Status": projected_status_val,
                    }
                )
        else:  # No existing offer found for this template
            action_log = "CREATE"  # Default for this branch
            reason_log = "New template"
            projected_status_val = "DISABLED (New)"

            if template_is_active_by_config:
                logging.info(
                    f"Creating new offer for template '{template_name}'. Total capital for this offer: {template_capital_limit_for_total_size:,}"
                )
                create_result = create_magma_offer(
                    new_pricing, template_capital_limit_for_total_size, template_name
                )
                if create_result and create_result.get("createOffer"):
                    new_id = create_result["createOffer"]
                    managed_offer_ids_this_run.add(new_id)
                    actions_summary_for_telegram.append(
                        f"🚀 Created {template_name} (New ID {new_id[:8]}): APR {prop_apr_val}% (F:{prop_fixed_val},PPM:{prop_ppm_val},TS:{prop_capital_val}). Initial: DISABLED."
                    )

                    logging.info(
                        f"Attempting to immediately toggle new offer {new_id} to ensure it is initially DISABLED."
                    )
                    if not toggle_magma_offer_status(
                        new_id,
                        f"{template_name} (post-create toggle to ensure DISABLED)",
                        "DISABLED (initial set)",
                    ):
                        logging.warning(
                            f"Could not ensure new offer {new_id} is in DISABLED state post-creation via toggle."
                        )
                        actions_summary_for_telegram.append(
                            f"⚠️ Failed to ensure {template_name} (ID {new_id[:8]}) is DISABLED post-creation."
                        )
                        projected_status_val = (
                            "CREATE_BUT_TOGGLE_FAIL"  # Update projected status
                        )
                else:
                    actions_summary_for_telegram.append(
                        f"❌ Failed to create {template_name}"
                    )
                    action_log = "CREATE_FAIL"  # Update action_log for dry run
                    projected_status_val = "CREATE_FAIL"  # Update projected status
            else:
                action_log = "SKIP_CREATE"  # Update action_log for dry run
                if not template_enabled_by_config_file:
                    reason_log = "Explicitly disabled in config."
                elif template_channel_size <= 0:
                    reason_log = (
                        f"Invalid template_channel_size ({template_channel_size})."
                    )
                else:
                    reason_log = f"Capital limit {template_capital_limit_for_total_size:,} < channel size {template_channel_size:,}."
                logging.info(
                    f"Skipping creation for '{template_name}': Template not active by config. Reason: {reason_log}"
                )
                actions_summary_for_telegram.append(
                    f"⚠️ Skipped create {template_name} (Not active: {reason_log})"
                )
                projected_status_val = "NOT_CREATED"  # Update projected status

            if DRY_RUN_MODE:  # This append is for the 'else (no existing offer):' block
                dry_run_proposed_actions_data.append(
                    {
                        "Template": template_name,
                        "Action": action_log,
                        "Reason": reason_log,
                        "Cur Fixed": "-",
                        "Cur PPM": "-",
                        "Cur APR": "-",
                        "Benchmark Source": benchmark_source_val,
                        "Mkt Fixed (Raw)": mkt_fixed_raw_val,
                        "Mkt PPM (Raw)": mkt_ppm_raw_val,
                        "Prop Fixed": prop_fixed_val,
                        "Prop PPM": prop_ppm_val,
                        "Prop APR": prop_apr_val,
                        "Prop Capital": prop_capital_val,
                        "Projected Status": projected_status_val,
                    }
                )

    # --- Orphaned Offer Handling ---
    # ... (rest of main function, including Dry Run Console Output and Telegram Notification, remains largely the same but uses the refined dry_run_proposed_actions_data) ...
    # Ensure this section is outside the 'for template_name in offer_template_names:' loop

    for existing_offer in my_existing_offers_raw:
        if existing_offer["id"] not in managed_offer_ids_this_run:
            if (
                existing_offer.get("offer_type") == "CHANNEL"
                and existing_offer.get("side") == "SELL"
            ):
                if existing_offer.get("status", "").upper() == "ENABLED":
                    display_size = existing_offer.get(
                        "available_size", existing_offer.get("total_size", "N/A")
                    )
                    display_size_formatted = (
                        f"{display_size:,}"
                        if isinstance(display_size, int)
                        else display_size
                    )

                    logging.info(
                        f"Disabling orphaned or unmanaged active CHANNEL/SELL offer ID {existing_offer['id']} (available size {display_size_formatted} sats)."
                    )
                    action_msg_tg = f"👻 Disabled orphaned offer ID {existing_offer['id'][:8]} (avail: {display_size_formatted} sats)"

                    if toggle_magma_offer_status(
                        existing_offer["id"],
                        "Orphaned/Unmanaged",
                        "DISABLED (orphaned)",
                    ):
                        actions_summary_for_telegram.append(action_msg_tg)
                        if DRY_RUN_MODE:
                            dry_run_orphaned_actions_data.append(
                                {
                                    "Offer ID": existing_offer["id"],
                                    "Action": "DISABLE_ORPHAN",
                                    "Reason": "Not matched to active template, was ENABLED",
                                    "Available Size": display_size_formatted,
                                }
                            )
                    else:
                        actions_summary_for_telegram.append(
                            f"❌ Failed to disable orphaned offer ID {existing_offer['id'][:8]}"
                        )
                        if DRY_RUN_MODE:
                            dry_run_orphaned_actions_data.append(
                                {
                                    "Offer ID": existing_offer["id"],
                                    "Action": "DISABLE_ORPHAN_FAIL",
                                    "Reason": "Not matched to active template, was ENABLED",
                                    "Available Size": display_size_formatted,
                                }
                            )
                else:
                    # Only log if debugging, otherwise it's expected
                    if logging.getLogger().getEffectiveLevel() <= logging.DEBUG:
                        logging.debug(
                            f"Orphaned CHANNEL/SELL offer {existing_offer['id']} is already not ENABLED (Status: {existing_offer.get('status')}). No action needed."
                        )
            # else: # No need to log skipped non-CHANNEL/SELL offers unless very verbose
            #     logging.debug(f"Skipping orphaned offer {existing_offer['id']} as it's not a CHANNEL/SELL offer managed by this script (Type: {existing_offer.get('offer_type')}, Side: {existing_offer.get('side')}).")

    # --- Dry Run Console Output ---
    if DRY_RUN_MODE:
        # Suppress console logging during dry run to show only the summary
        console_handler = None
        for handler in logging.getLogger().handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.handlers.RotatingFileHandler
            ):
                console_handler = handler
                console_handler.setLevel(logging.ERROR)  # Only show errors on console

        print("\n" + "=" * 60)
        print(" MAGMA AUTOPRICE DRY RUN SUMMARY 🔷")
        print("=" * 60)

        # 1. CAPITAL OVERVIEW (Essential for business decisions)
        print(f"\n💰 CAPITAL & FUNDING")
        print(f"   Available Balance: {total_lnd_balance_for_magma:,} sats")
        print(
            f"   Allocated to Magma: {capital_for_magma_total_config:,} sats ({fraction_for_sale*100:.0f}%)"
        )

        # 2. MARKET CONDITIONS (What you need to know about competition)
        print(f"\n📊 MARKET CONDITIONS")
        print(
            f"   Competitive Offers: {len(market_offers)} sellers (score ≥{magma_specific_config.get('magma_autoprice', 'min_seller_score_filter', fallback='85')})"
        )

        # Show market pricing insights - FIXED: Ensure we're using properly filtered data
        if market_offers:
            # Calculate market ranges for quick insight - ONLY from score-filtered offers
            fixed_fees = [offer["base_fee"] for offer in market_offers]
            ppm_rates = [offer["fee_rate"] for offer in market_offers]
            fixed_fees.sort()
            ppm_rates.sort()

            print(f"   Fixed Fee Range: {fixed_fees[0]:,} - {fixed_fees[-1]:,} sats")
            print(f"   PPM Rate Range: {ppm_rates[0]:,} - {ppm_rates[-1]:,} ppm")

            # Show where we're positioning
            target_percentile = magma_specific_config.getint(
                "magma_autoprice", "pricing_strategy_percentile", fallback=10
            )
            target_idx = int(len(fixed_fees) * target_percentile / 100)
            print(
                f"   Target Position: Top {target_percentile}% (rank ~{target_idx+1} of {len(fixed_fees)})"
            )

        # 3. YOUR OFFERS STATUS (Current state) - FIXED: Use available_size instead of min_size
        print(f"\n YOUR OFFERS STATUS")
        active_offers = [o for o in my_existing_offers_raw if o["status"] == "ENABLED"]
        disabled_offers = [
            o for o in my_existing_offers_raw if o["status"] == "DISABLED"
        ]

        if active_offers:
            print(f"   Active: {len(active_offers)} offers")
            for offer in active_offers:
                # Use available_size (total_size - locked_size) instead of min_size
                available_size = offer.get("available_size", 0)
                print(
                    f"     • {available_size:,} sats × {offer['duration_days']:.0f}d: {offer['base_fee']:,} + {offer['fee_rate']:,}ppm ({offer['apr']:.1f}% APR)"
                )
        else:
            print("   Active: None")

        if disabled_offers:
            print(f"   Disabled: {len(disabled_offers)} offers")

        # 4. PROPOSED ACTIONS (What will happen)
        print(f"\n🎯 PROPOSED ACTIONS")
        if dry_run_proposed_actions_data:
            for proposal in dry_run_proposed_actions_data:
                template = proposal["Template"]
                action = proposal["Action"]

                if action == "CREATE":
                    print(f"   ➕ CREATE {template}")
                    print(f"      Size: {proposal.get('Prop Capital', 'N/A')} sats")
                    print(
                        f"      Pricing: {proposal.get('Prop Fixed', '-')} + {proposal.get('Prop PPM', '-')}ppm ({proposal.get('Prop APR', '-')}% APR)"
                    )
                    print(
                        f"      Market: Based on {proposal.get('Benchmark Source', 'N/A')}"
                    )

                elif action == "UPDATE":
                    print(f"   🔄 UPDATE {template}")
                    print(
                        f"      Current: {proposal.get('Cur Fixed', '-')} + {proposal.get('Cur PPM', '-')}ppm ({proposal.get('Cur APR', '-')}% APR)"
                    )
                    print(
                        f"      Proposed: {proposal.get('Prop Fixed', '-')} + {proposal.get('Prop PPM', '-')}ppm ({proposal.get('Prop APR', '-')}% APR)"
                    )

                elif action == "NO_CHANGE":
                    print(f"   ✅ {template}: No changes needed")

                elif action.startswith("SKIP"):
                    print(f"   ⏭️  {template}: {proposal.get('Reason', 'Skipped')}")

                elif action.endswith("_FAIL"):
                    print(
                        f"   ❌ {template}: {action} - {proposal.get('Reason', 'Failed')}"
                    )
        else:
            print("   No actions proposed")

        # 5. ORPHANED OFFERS (Cleanup actions)
        if dry_run_orphaned_actions_data:
            print(f"\n CLEANUP ACTIONS")
            for orphan in dry_run_orphaned_actions_data:
                print(
                    f"   🚫 Disable orphaned offer: {orphan['Offer ID'][:8]}... ({orphan['Available Size']} sats)"
                )

        # 6. KEY INSIGHTS (Business intelligence) - ENHANCED
        print(f"\n💡 KEY INSIGHTS")

        # APR warnings with actionable advice
        apr_warnings = []
        for proposal in dry_run_proposed_actions_data:
            if proposal.get("Prop APR") and proposal.get("Prop APR") != "-":
                try:
                    apr = float(proposal["Prop APR"])
                    template = proposal["Template"]
                    if apr < 5.0:
                        apr_warnings.append(f"   ⚠️  {template}: Low APR ({apr:.1f}%)")
                        apr_warnings.append(
                            f"      → Increase min_fixed_fee_sats or min_ppm_fee in [{template}]"
                        )
                        apr_warnings.append(
                            f"      → Or lower target_apr_min in [{template}]"
                        )
                    elif apr > 20.0:
                        apr_warnings.append(
                            f"   ⚠️  {template}: High APR ({apr:.1f}%) - may be uncompetitive"
                        )
                        apr_warnings.append(
                            f"      → Decrease max_fixed_fee_sats or max_ppm_fee in [{template}]"
                        )
                except (ValueError, TypeError):
                    pass

        if apr_warnings:
            for warning in apr_warnings:
                print(warning)
        else:
            print("   ✅ All proposed APRs are within reasonable ranges")

        # Market positioning insight with actionable advice
        if market_offers and dry_run_proposed_actions_data:
            print(
                f"   Market positioning: Targeting top {target_percentile}% of {len(market_offers)} competitive sellers"
            )
            if target_percentile <= 10:
                print(
                    f"      → For higher fees: increase pricing_strategy_percentile (currently {target_percentile})"
                )
            elif target_percentile >= 50:
                print(
                    f"      → For more competitive pricing: decrease pricing_strategy_percentile (currently {target_percentile})"
                )

        print("\n" + "=" * 60)

        # Restore console logging level
        if console_handler:
            console_handler.setLevel(logging.INFO)

        # Keep detailed logging for file but not console
        detailed_log_parts = []
        for title, data, fields in [
            ("LND & Capital", dry_run_lnd_capital_summary_data, ["Metric", "Value"]),
            ("Configuration", dry_run_key_config_summary_data, ["Setting", "Value"]),
            (
                "Existing Offers",
                dry_run_existing_offers_summary_data,
                [
                    "Offer ID",
                    "Status",
                    "Min Size",
                    "Total Size",
                    "Avail. Size",
                    "Duration (Days)",
                    "Fixed",
                    "PPM",
                    "APR (%)",
                ],
            ),
            (
                "Proposed Actions",
                dry_run_proposed_actions_data,
                [
                    "Template",
                    "Action",
                    "Reason",
                    "Cur Fixed",
                    "Cur PPM",
                    "Cur APR",
                    "Benchmark Source",
                    "Mkt Fixed (Raw)",
                    "Mkt PPM (Raw)",
                    "Prop Fixed",
                    "Prop PPM",
                    "Prop APR",
                    "Prop Capital",
                    "Projected Status",
                ],
            ),
            (
                "Orphaned Actions",
                dry_run_orphaned_actions_data,
                ["Offer ID", "Action", "Reason", "Available Size"],
            ),
        ]:
            if data:
                detailed_log_parts.append(f"\n--- {title} ---")
                try:
                    from prettytable import PrettyTable

                    table = PrettyTable()
                    table.field_names = fields
                    for row_data in data:
                        table.add_row(
                            [str(row_data.get(field, "-")) for field in fields]
                        )
                    table.align = "l"
                    detailed_log_parts.append(table.get_string())
                except ImportError:
                    # Fallback to simple format for logging
                    for row_data in data:
                        row_str = " | ".join(
                            [str(row_data.get(field, "-")) for field in fields]
                        )
                        detailed_log_parts.append(row_str)

        if detailed_log_parts:
            logging.info("Detailed Dry Run Data:\n" + "\n".join(detailed_log_parts))

    # --- Telegram Notification ---
    if actions_summary_for_telegram:
        telegram_message_parts = []
        if DRY_RUN_MODE:
            telegram_message_parts.append("🔷 *Magma AutoPrice DRY RUN Summary* 🔷")
        else:
            telegram_message_parts.append("✅ *Magma AutoPrice Update Summary* ✅")

        telegram_message_parts.append(f"\n*LND & Capital:*")
        telegram_message_parts.append(
            f"  Balance (Excl. Loop): {total_lnd_balance_for_magma:,} sats"
        )
        telegram_message_parts.append(
            f"  Total for Magma: {capital_for_magma_total_config:,} sats ({fraction_for_sale*100:.0f}%)"
        )

        if DRY_RUN_MODE and dry_run_proposed_actions_data:
            telegram_message_parts.append("\n*Proposed Actions on Templates:*")
            for proposal in dry_run_proposed_actions_data:
                action_line = f"  *{proposal['Template']}*: {proposal['Action']}"
                if proposal.get("Reason") and proposal["Reason"] != "New template":
                    action_line += f" ({proposal['Reason']})"

                # Show proposed pricing if action involves it (Create, Update)
                if proposal["Action"] not in [
                    "SKIP_CREATE",
                    "ERROR",
                    "NO_CHANGE",
                ] and not proposal["Action"].endswith("_FAIL"):
                    action_line += f" -> Fix:{proposal.get('Prop Fixed', '-')}, PPM:{proposal.get('Prop PPM', '-')}, APR:{proposal.get('Prop APR', '-')}%"
                    if (
                        proposal.get("Benchmark Source")
                        and proposal["Benchmark Source"] != "Fallback: Template Mins"
                    ):
                        action_line += f" (MktRaw F:{proposal.get('Mkt Fixed (Raw)')} P:{proposal.get('Mkt PPM (Raw)')}, Src: {proposal.get('Benchmark Source')})"

                telegram_message_parts.append(action_line)
            if dry_run_orphaned_actions_data:
                telegram_message_parts.append("\n*Orphaned Offers Actions:*")
                for orphan in dry_run_orphaned_actions_data:
                    telegram_message_parts.append(
                        f"  ID {orphan['Offer ID'][:8]}: {orphan['Action']} (Avail: {orphan['Available Size']})"
                    )

        else:
            telegram_message_parts.append("\n*Actions Taken/Attempted:*")
            if actions_summary_for_telegram:  # This is the list of strings for TG
                for action_item in actions_summary_for_telegram:
                    telegram_message_parts.append(f"  {action_item}")
            else:
                telegram_message_parts.append(
                    "  No specific actions taken on Magma offers."
                )

        final_telegram_message = "\n".join(telegram_message_parts)
        send_telegram_notification(final_telegram_message)

    else:
        logging.info("No specific actions to report for Magma offers in this run.")
        notify_on_no_change = not magma_specific_config.getboolean(
            "magma_autoprice", "telegram_notify_on_change_only", fallback=True
        )
        if notify_on_no_change:
            no_change_message = f"ℹ️ Magma AutoPrice: No changes to offers.{' (DRY RUN)' if DRY_RUN_MODE else ''}\nLND Bal: {total_lnd_balance_for_magma:,} sats, Magma Cap: {capital_for_magma_total_config:,} sats."
            send_telegram_notification(no_change_message)

    logging.info("Magma Market Fee Updater script finished.")


# Helper functions for config parsing to strip inline comments
def get_config_int_with_comment_stripping(
    config_proxy_section_or_parser, key_or_section, option_if_parser=None, fallback=None
):
    """
    Reads an integer from config, stripping inline comments.
    Can accept either a ConfigParser object + section + option, or a SectionProxy + option.
    """
    value_str = None
    section_name_for_error = "N/A"
    option_name_for_error = "N/A"
    try:
        if option_if_parser is not None:  # Called with parser, section, option
            parser = config_proxy_section_or_parser
            section_name_for_error = key_or_section
            option_name_for_error = option_if_parser
            value_str = parser.get(key_or_section, option_if_parser)
        else:  # Called with section_proxy, option
            section_proxy = config_proxy_section_or_parser
            option_name_for_error = key_or_section
            section_name_for_error = getattr(section_proxy, "name", "UnknownSection")
            value_str = section_proxy.get(key_or_section)

        if value_str is None:
            if fallback is not None:
                return int(fallback)
            raise ValueError(
                f"Config key '{option_name_for_error}' in section '{section_name_for_error}' not found and no fallback."
            )
        return int(value_str.split("#")[0].strip())
    except (configparser.NoOptionError, configparser.NoSectionError):
        if fallback is not None:
            return int(fallback)
        logging.error(
            f"Config key '{option_name_for_error}' in section '{section_name_for_error}' not found and no fallback provided."
        )
        raise
    except ValueError as e:
        logging.error(
            f"Invalid integer value for config key '{option_name_for_error}' in section '{section_name_for_error}': '{value_str}'. Error: {e}"
        )
        if fallback is not None:
            logging.warning(
                f"Using fallback value {fallback} for key '{option_name_for_error}'."
            )
            return int(fallback)
        raise


def get_config_float_with_comment_stripping(
    config_proxy_section_or_parser, key_or_section, option_if_parser=None, fallback=None
):
    """
    Reads a float from config, stripping inline comments.
    Can accept either a ConfigParser object + section + option, or a SectionProxy + option.
    """
    value_str = None
    section_name_for_error = "N/A"
    option_name_for_error = "N/A"
    try:
        if option_if_parser is not None:  # Called with parser, section, option
            parser = config_proxy_section_or_parser
            section_name_for_error = key_or_section
            option_name_for_error = option_if_parser
            value_str = parser.get(key_or_section, option_if_parser)
        else:  # Called with section_proxy, option
            section_proxy = config_proxy_section_or_parser
            option_name_for_error = key_or_section
            section_name_for_error = getattr(section_proxy, "name", "UnknownSection")
            value_str = section_proxy.get(key_or_section)

        if value_str is None:
            if fallback is not None:
                return float(fallback)
            raise ValueError(
                f"Config key '{option_name_for_error}' in section '{section_name_for_error}' not found and no fallback provided."
            )
        return float(value_str.split("#")[0].strip())
    except (configparser.NoOptionError, configparser.NoSectionError):
        if fallback is not None:
            return float(fallback)
        logging.error(
            f"Config key '{option_name_for_error}' in section '{section_name_for_error}' not found and no fallback provided."
        )
        raise
    except ValueError as e:
        logging.error(
            f"Invalid float value for config key '{option_name_for_error}' in section '{section_name_for_error}': '{value_str}'. Error: {e}"
        )
        if fallback is not None:
            logging.warning(
                f"Using fallback value {fallback} for key '{option_name_for_error}'."
            )
            return float(fallback)
        raise


if __name__ == "__main__":
    main()
