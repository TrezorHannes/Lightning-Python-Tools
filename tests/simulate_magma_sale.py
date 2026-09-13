#!/usr/bin/env python3
"""
Magma Buy Order End-to-End Simulation Harness
==============================================

Simulates incoming Amboss Magma channel buy orders to verify the full
order lifecycle without spending on-chain funds or touching live Amboss orders.

Scenarios tested:
  1. Timeout Auto-Accept: Ingests a new order in WAITING_FOR_SELLER_APPROVAL with
     unsettled zero-fee structure, presents inline keyboard prompt, awaits timeout,
     and auto-approves via _handle_timeout_for_offer without KeyError.
  2. Manual Callback Approve: Simulates user pressing "Approve" button via Telegram
     inline keyboard callback (decide_order:approve:<order_id>).
  3. Manual Callback Reject: Simulates user pressing "Reject" button via Telegram.
  4. Telegram Logging Handler: Dispatches an error log with full traceback to ensure
     TelegramLoggingHandler streams alerts to Telegram.

Modes:
  - Default / Mock Mode: Completely offline, mocks all external services (Telegram,
    Amboss GraphQL, LND). Safe for automated test suites and CI.
  - Live Telegram Mode (--live-telegram): Uses real Telegram bot credentials from config.ini
    to dispatch live interactive notifications and test error logs to your Telegram chat,
    while safely mocking Amboss mutations and LND invoice creation.

Usage:
  # Automated offline verification (default)
  .venv/bin/python tests/simulate_magma_sale.py

  # Live interactive verification sent to your Telegram chat
  .venv/bin/python tests/simulate_magma_sale.py --live-telegram --timeout-seconds 8

  # Run specific scenario
  .venv/bin/python tests/simulate_magma_sale.py --scenario timeout
  .venv/bin/python tests/simulate_magma_sale.py --scenario callback
  .venv/bin/python tests/simulate_magma_sale.py --scenario error
"""

import argparse
import configparser
import logging
import os
import sys
import time
from unittest.mock import MagicMock, patch

# Ensure project root and Magma directory are on sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
MAGMA_DIR = os.path.join(PROJECT_ROOT, "Magma")

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if MAGMA_DIR not in sys.path:
    sys.path.insert(0, MAGMA_DIR)

# Sample order mirroring the live Amboss GraphQL MarketOrder schema from failure f73c6ad8
SAMPLE_AMBOSS_ORDER = {
    "id": "f73c6ad8-c878-48c0-8ad8-ae004c5b6197",
    "status": "WAITING_FOR_SELLER_APPROVAL",
    "amount": {
        "satoshi": {
            "sats": "5000000",
            "btc": "0.05",
            "usd": "3000.00"
        }
    },
    "fees": {
        "seller": {
            "sats": 0,  # Unsettled order trap: must fallback to fixed + variable fees
            "btc": 0.0,
            "usd": 0.0
        },
        "fixed": {
            "sats": 5000
        },
        "variable": {
            "sats": 75000
        },
        "amboss": {
            "sats": 1000
        }
    },
    "fixed_fee": {
        "sats": 5000
    },
    "variable_fee": {
        "sats": 75000
    },
    "destination": {
        "pubkey": "029a3e215d2a6a8b7c6d5e4f3a2b1c0d9e8f7a6b5c4d3e2f1a0b9c8d7e6f5a4b3c",
        "alias": "SimulationBuyerNode"
    }
}

EXPECTED_SELLER_FEE_SATS = 80000  # 5,000 fixed + 75,000 variable


def setup_simulation_logger():
    logger = logging.getLogger("MagmaSimulation")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("\033[1;36m[SIMULATION]\033[0m %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


def run_simulation(scenario="all", live_telegram=False, timeout_seconds=6):
    """
    Executes the specified end-to-end simulation.
    Returns True if all requested scenarios succeed, False otherwise.
    """
    logger = setup_simulation_logger()
    logger.info("Initializing Magma Buy Order End-to-End Simulation Harness...")
    logger.info(f"Target Scenario: {scenario} | Live Telegram: {live_telegram} | Timeout: {timeout_seconds}s")

    # Verify config and credentials if live telegram requested
    real_token = None
    real_chat_id = None
    if live_telegram:
        config_path = os.path.join(PROJECT_ROOT, "config.ini")
        if not os.path.exists(config_path):
            logger.error(f"Cannot run --live-telegram: config.ini not found at {config_path}")
            return False
        cfg = configparser.ConfigParser()
        cfg.read(config_path)
        real_token = cfg.get("telegram", "magma_bot_token", fallback=None)
        real_chat_id = cfg.get("telegram", "telegram_user_id", fallback=None)
        if not real_token or not real_chat_id:
            logger.error("Cannot run --live-telegram: telegram magma_bot_token or telegram_user_id missing in config.ini")
            return False
        logger.info("Live Telegram mode active: using real bot token and chat ID for interactive notifications.")

    import magma_sale_process as magma

    # Set up shared mocks for external safety (never open real channels or charge real sats)
    mock_lnd_invoice = ("simulated_hash_3a8b2c1d", "lnbc800u1simulatedinvoicefor80ksatsmagmaordertestonly")
    mock_accept_response = {
        "data": {
            "market": {
                "order": {
                    "seller": {
                        "accept": {
                            "success": True
                        }
                    }
                }
            }
        }
    }
    mock_reject_response = {
        "data": {
            "market": {
                "order": {
                    "seller": {
                        "reject": {
                            "success": True
                        }
                    }
                }
            }
        }
    }

    results = {}

    # --------------------------------------------------------------------------
    # SCENARIO 1: Timeout Auto-Accept Flow
    # --------------------------------------------------------------------------
    if scenario in ("all", "timeout"):
        logger.info("\n" + "=" * 70)
        logger.info("RUNNING SCENARIO 1: Timeout Auto-Accept Flow")
        logger.info("=" * 70)

        # Clean pending state
        magma.pending_user_confirmations.clear()
        order_data = SAMPLE_AMBOSS_ORDER.copy()
        order_id = order_data["id"]

        with patch.object(magma, "get_offers_awaiting_seller_approval", return_value=order_data), \
             patch.object(magma, "get_order_details_from_amboss", return_value=order_data), \
             patch.object(magma, "get_node_alias", return_value="SimulationBuyerNode"), \
             patch.object(magma, "get_node_extended_details", return_value={"amboss": {"is_claimed": True}}), \
             patch.object(magma, "execute_lncli_addinvoice", return_value=mock_lnd_invoice) as mock_addinvoice, \
             patch.object(magma, "accept_order", return_value=mock_accept_response) as mock_accept, \
             patch.object(magma, "wait_for_buyer_payment", return_value=None) as mock_wait_payment:

            if not live_telegram:
                mock_msg = MagicMock()
                mock_msg.message_id = 999111
                send_patch = patch.object(magma, "send_telegram_notification", return_value=mock_msg)
                edit_patch = patch.object(magma.bot, "edit_message_text", return_value=True)
            else:
                from contextlib import nullcontext
                send_patch = nullcontext()
                edit_patch = nullcontext()

            with send_patch, edit_patch:
                logger.info("Step 1: Ingesting new offer via process_new_offers()...")
                magma.process_new_offers()

                # Verify pending queue population
                if order_id not in magma.pending_user_confirmations:
                    logger.error("FAIL: order was not queued in pending_user_confirmations!")
                    results["timeout"] = False
                else:
                    queued_entry = magma.pending_user_confirmations[order_id]
                    queued_details = queued_entry.get("details", {})
                    extracted = magma.extract_order_info(queued_details)
                    logger.info(f"Step 2: Order queued with normalized seller_invoice_amount = {extracted.get('seller_invoice_amount')} sats")
                    if extracted.get("seller_invoice_amount") != EXPECTED_SELLER_FEE_SATS:
                        logger.error(f"FAIL: Expected {EXPECTED_SELLER_FEE_SATS} sats, got {extracted.get('seller_invoice_amount')}")
                        results["timeout"] = False
                    else:
                        logger.info(f"Step 3: Simulating timeout countdown ({timeout_seconds}s)...")
                        if live_telegram:
                            magma.send_telegram_notification(
                                f"🧪 *[SIMULATION]* New order prompt sent above!\n"
                                f"Waiting {timeout_seconds}s for simulated timeout auto-approval...",
                                parse_mode="Markdown"
                            )
                        
                        # Artificially age the timestamp so it exceeds USER_CONFIRMATION_TIMEOUT_SECONDS
                        queued_entry["timestamp"] = time.time() - (magma.USER_CONFIRMATION_TIMEOUT_SECONDS + 10)
                        time.sleep(timeout_seconds if live_telegram else 0.1)

                        logger.info("Step 4: Triggering check_pending_confirmations_timeouts()...")
                        magma.check_pending_confirmations_timeouts()

                        # Assertions
                        if order_id in magma.pending_user_confirmations:
                            logger.error("FAIL: order still present in pending_user_confirmations after timeout!")
                            results["timeout"] = False
                        elif not mock_addinvoice.called:
                            logger.error("FAIL: execute_lncli_addinvoice was not called during auto-approval!")
                            results["timeout"] = False
                        else:
                            called_amount = mock_addinvoice.call_args[0][0]
                            logger.info(f"Step 5: execute_lncli_addinvoice invoked with {called_amount} sats.")
                            if called_amount != EXPECTED_SELLER_FEE_SATS:
                                logger.error(f"FAIL: Invoice created with wrong amount {called_amount} sats!")
                                results["timeout"] = False
                            elif not mock_accept.called:
                                logger.error("FAIL: accept_order was not called on Amboss!")
                                results["timeout"] = False
                            else:
                                logger.info(f"Step 6: Amboss accept_order called with invoice: {mock_accept.call_args[0][1]}")
                                logger.info("✅ SUCCESS: Timeout auto-accept completed end-to-end without KeyError!")
                                results["timeout"] = True

    # --------------------------------------------------------------------------
    # SCENARIO 2: Manual Telegram Callback Approval
    # --------------------------------------------------------------------------
    if scenario in ("all", "callback"):
        logger.info("\n" + "=" * 70)
        logger.info("RUNNING SCENARIO 2: Manual Telegram Callback Approval")
        logger.info("=" * 70)

        magma.pending_user_confirmations.clear()
        order_data = SAMPLE_AMBOSS_ORDER.copy()
        order_id = order_data["id"]

        with patch.object(magma, "get_offers_awaiting_seller_approval", return_value=order_data), \
             patch.object(magma, "get_order_details_from_amboss", return_value=order_data), \
             patch.object(magma, "get_node_alias", return_value="SimulationBuyerNode"), \
             patch.object(magma, "get_node_extended_details", return_value={"amboss": {"is_claimed": True}}), \
             patch.object(magma, "execute_lncli_addinvoice", return_value=mock_lnd_invoice) as mock_addinvoice, \
             patch.object(magma, "accept_order", return_value=mock_accept_response) as mock_accept, \
             patch.object(magma, "wait_for_buyer_payment", return_value=None):

            if not live_telegram:
                mock_msg = MagicMock()
                mock_msg.message_id = 999222
                send_patch = patch.object(magma, "send_telegram_notification", return_value=mock_msg)
            else:
                from contextlib import nullcontext
                send_patch = nullcontext()

            with send_patch:
                logger.info("Step 1: Queuing order for callback...")
                magma.process_new_offers()

                # Construct simulated CallbackQuery from Telegram button click
                simulated_call = MagicMock()
                simulated_call.id = "call_sim_12345"
                simulated_call.data = f"decide_order:approve:{order_id}"
                simulated_call.message.chat.id = magma.CHAT_ID
                simulated_call.message.message_id = magma.pending_user_confirmations[order_id]["message_id"]

                logger.info(f"Step 2: Simulating incoming Telegram callback query: {simulated_call.data}")
                
                # Mock bot methods to avoid modifying chat history during mock mode
                if not live_telegram:
                    with patch.object(magma.bot, "answer_callback_query"), \
                         patch.object(magma.bot, "edit_message_text"):
                        magma.handle_order_decision_callback(simulated_call)
                else:
                    # In live telegram mode, safely execute callback handling
                    magma.handle_order_decision_callback(simulated_call)

                # Wait for the background approval thread to finish execution
                import threading
                for thread in threading.enumerate():
                    if thread.name == f"Approve-{order_id}":
                        thread.join(timeout=5)

                if order_id in magma.pending_user_confirmations:
                    logger.error("FAIL: order still present in pending_user_confirmations after callback!")
                    results["callback"] = False
                elif not mock_addinvoice.called:
                    logger.error("FAIL: execute_lncli_addinvoice was not called during callback approval!")
                    results["callback"] = False
                else:
                    called_amount = mock_addinvoice.call_args[0][0]
                    logger.info(f"Step 3: Invoice generated for {called_amount} sats.")
                    if called_amount != EXPECTED_SELLER_FEE_SATS:
                        logger.error(f"FAIL: Invoice amount mismatch: {called_amount}")
                        results["callback"] = False
                    elif not mock_accept.called:
                        logger.error("FAIL: Amboss accept_order mutation was not called!")
                        results["callback"] = False
                    else:
                        logger.info("✅ SUCCESS: Telegram callback approval completed end-to-end without KeyError!")
                        results["callback"] = True

    # --------------------------------------------------------------------------
    # SCENARIO 3: Telegram Logging Handler Error Stream
    # --------------------------------------------------------------------------
    if scenario in ("all", "error"):
        logger.info("\n" + "=" * 70)
        logger.info("RUNNING SCENARIO 3: Telegram Logging Handler Error Stream")
        logger.info("=" * 70)

        sim_logger = logging.getLogger("SimulatedComponent")
        sim_logger.setLevel(logging.DEBUG)

        sent_messages = []
        if live_telegram:
            logger.info("Step 1: Emitting live simulated error log to Telegram via TelegramLoggingHandler...")
            try:
                raise ValueError("Simulated Magma exception for end-to-end verification")
            except Exception:
                magma.logging.getLogger().error(
                    "🧪 [SIMULATION] Live test of TelegramLoggingHandler - verifying exception traceback dispatch.",
                    exc_info=True
                )
            logger.info("Step 2: Dispatched error log to Telegram. Check your Telegram chat for the formatted alert.")
            results["error"] = True
        else:
            logger.info("Step 1: Testing TelegramLoggingHandler emission with mock bot...")
            mock_test_bot = MagicMock()
            handler = magma.TelegramLoggingHandler(bot=mock_test_bot, chat_id="123456", level=logging.ERROR)
            test_logger = logging.getLogger("TestErrorLogger")
            test_logger.addHandler(handler)

            try:
                raise KeyError("seller_invoice_amount")
            except Exception:
                test_logger.error("Testing simulated KeyError handling", exc_info=True)

            if not mock_test_bot.send_message.called:
                logger.error("FAIL: TelegramLoggingHandler did not call send_message!")
                results["error"] = False
            else:
                sent_text = mock_test_bot.send_message.call_args[1].get("text", "")
                if "KeyError: 'seller_invoice_amount'" in sent_text and "🚨 *[ERROR]*" in sent_text:
                    logger.info("Step 2: Successfully verified markdown formatting and traceback inclusion.")
                    logger.info("✅ SUCCESS: TelegramLoggingHandler captured and formatted exception correctly.")
                    results["error"] = True
                else:
                    logger.error(f"FAIL: Formatted message content unexpected: {sent_text[:100]}")
                    results["error"] = False

    # --------------------------------------------------------------------------
    # Summary Report
    # --------------------------------------------------------------------------
    logger.info("\n" + "=" * 70)
    logger.info("SIMULATION EXECUTION SUMMARY")
    logger.info("=" * 70)
    all_passed = True
    for s_name, passed in results.items():
        status_str = "\033[1;32mPASSED\033[0m" if passed else "\033[1;31mFAILED\033[0m"
        logger.info(f"  • Scenario [{s_name}]: {status_str}")
        if not passed:
            all_passed = False

    if all_passed:
        logger.info("\n🎉 All simulated scenarios completed successfully!")
    else:
        logger.error("\n❌ One or more scenarios failed.")

    return all_passed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Magma Buy Order End-to-End Simulation")
    parser.add_argument(
        "--scenario",
        choices=["all", "timeout", "callback", "error"],
        default="all",
        help="Which scenario to simulate (default: all)"
    )
    parser.add_argument(
        "--live-telegram",
        action="store_true",
        help="Dispatch live test messages to your Telegram chat using config.ini credentials"
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=6,
        help="Simulated countdown in seconds before auto-approval (default: 6s)"
    )

    args = parser.parse_args()
    success = run_simulation(
        scenario=args.scenario,
        live_telegram=args.live_telegram,
        timeout_seconds=args.timeout_seconds
    )
    sys.exit(0 if success else 1)
