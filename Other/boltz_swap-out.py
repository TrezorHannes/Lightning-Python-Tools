#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
boltz_swap-out.py: Automates Lightning Network (LN) to Liquid Bitcoin (L-BTC) swaps.

Purpose:
This script facilitates swapping out Bitcoin from the Lightning Network to Liquid Bitcoin.
It identifies suitable Lightning channels for the swap source using the LNDg API,
initiates the swap via the Boltz Client (`boltzcli`), and then pays the resulting Lightning
invoice using `lncli`, with options for multi-path payments and retries.

Prerequisites:
1.  LNDg instance accessible via API.
2.  Boltz Client (`boltzd` daemon) installed, configured, and running.
3.  `boltzcli` (Boltz Client command-line interface) installed.
4.  `lncli` (Lightning Network Daemon command-line interface) installed and configured.
5.  `pscli` (Elements/Liquid command-line interface, e.g., from Blockstream Green)
    installed for generating L-BTC addresses.
6.  A `config.ini` file in the parent directory (`../config.ini`) containing:
    - LNDg API credentials and URL.
    - Paths to `lncli`, `pscli`, and `boltzcli` executables.
    - Paths to `boltzd`'s gRPC TLS certificate and admin macaroon for `boltzcli` authentication.


Usage:
python boltz_swap-out.py --amount <sats_to_swap> [OPTIONS]

Example:
python boltz_swap-out.py --amount 1000000 --capacity 2000000 --local-fee-limit 5 --debug

Options:
  --amount SATS          (Required) The amount in satoshis to swap from LN to L-BTC.
  --capacity SATS        Minimum local balance on a channel to be a swap candidate.
                         (Default: 3000000)
  --local-fee-limit PPM  Maximum local fee rate (ppm) for candidate channels.
                         (Default: 10)
  --max-parts INT        Max parts for `lncli payinvoice --max_parts`. (Default: 16)
  --payment-timeout STR  Timeout for `lncli payinvoice` (e.g., "10m", "1h").
                         (Default: "10m")
  --payment_fee_limit_percent FLOAT
                        Maximum total fee for the LN payment as a percentage of the payment amount (e.g., 0.5 for 0.5%%).
                        (Default: 1.0)
  --description STR      Optional description for the Boltz swap invoice.
                         (Default: "LNDg-Boltz-Swap-Out")
  --debug                Enable debug mode: prints commands, no actual execution.
                         (Highly recommended for first runs).
  --verbose              Enable verbose output, showing full details of executed commands like LND connection parameters.
  --custom-destination-address ADDRESS
                         Manually specify the L-BTC destination address.
                         If set, skips 'pscli' address generation.
                         A confirmation prompt will appear unless --force is used.
  --force, -f            Skip confirmation prompts (e.g., for custom destination address).
  --lncli-cltv-limit INT Set the CLTV limit (max time lock in blocks) for lncli payinvoice/queryroutes. (Default: 0, LND's default).
                         A lower value might make payments resolve or fail faster.
  --queryroutes-timeout STR Timeout for an individual lncli queryroutes attempt (e.g., '10s', '30s'). Default: 20s.
  --outgoing-channel-id  Specify a single outgoing channel ID to use for payment. Skips LNDg API query and batching process.
                         If specified but no channel ID provided, script will prompt for one.
  -h, --help             Show this help message and exit.

Workflow:
1.  Fetches a new L-BTC address using `pscli` OR uses a custom-provided address.
2.  Queries LNDg API to find suitable outgoing channels based on `--capacity`
    and `--local-fee-limit`.
3.  Initiates a "reverse swap" (LN -> L-BTC) using `boltzcli createreverseswap`.
    This command requires the `--external-pay` flag to prevent auto-payment and
    will be called with `boltzd`'s TLS cert and admin macaroon.
4.  Parses the Lightning invoice from the Boltz `boltzcli` response.
5.  Attempts to pay the invoice using `lncli payinvoice`, utilizing the
    candidate channels (`--outgoing_chan_id`).
6.  If payment fails, it retries with the next batch of candidate channels.

Disclaimer:
This script interacts with real Bitcoin and Lightning Network funds.
Use with extreme caution. The authors are not responsible for any loss of funds.
Always test thoroughly with small amounts and use the --debug flag first.
Ensure all paths in `config.ini` are correctly set.
"""

import argparse
import configparser
import json
import os
import subprocess
import sys
import time
import requests  # Added for LNDg API calls
import math
import re
import signal
import logging
from logging.handlers import RotatingFileHandler
import binascii

# --- Global Logger ---
script_logger = logging.getLogger(__name__)


# --- ANSI Color Codes ---
class Colors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"


def setup_logging():
    """Sets up a rotating file logger."""
    # Calculate log file path relative to the script's location
    script_file_path = os.path.abspath(__file__)
    project_root_dir = os.path.dirname(os.path.dirname(script_file_path))
    logs_dir = os.path.join(project_root_dir, "logs")
    if not os.path.exists(logs_dir):
        os.makedirs(logs_dir, exist_ok=True)

    log_file_path = os.path.join(logs_dir, "boltz_swap-out.log")

    handler = RotatingFileHandler(
        log_file_path, maxBytes=10 * 1024 * 1024, backupCount=5  # 10 MB
    )
    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)

    script_logger.addHandler(handler)
    script_logger.setLevel(logging.INFO)  # Default to INFO, can be changed if needed

    # Optionally, reduce verbosity of third-party libraries for the file log
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def print_color(text, color_code, bold=False):
    """Prints text in a specified color and optionally bold."""
    if bold:
        print(f"{color_code}{Colors.BOLD}{text}{Colors.ENDC}")
    else:
        print(f"{color_code}{text}{Colors.ENDC}")


def print_bold_step(text, is_subprocess=False, color_code=Colors.OKBLUE):
    """Prints a step description in bold, optionally with a specific color."""
    # The is_subprocess parameter isn't used yet, but kept for potential future use.
    # You might want to change the color or add a prefix based on it.
    print(f"{color_code}{Colors.BOLD}{text}{Colors.ENDC}")


def parse_arguments():
    """Parses command-line arguments."""

    # --- Dynamic Epilog/Disclaimer Logic ---
    epilog_text = "Automated LN to L-BTC swaps. Use with caution. Refer to script source for full disclaimer."  # Default fallback
    current_docstring = __doc__  # Assign to a variable to ensure it's fetched once
    if current_docstring:  # Check if __doc__ is not None
        disclaimer_marker = "Disclaimer:"
        disclaimer_start_index = current_docstring.find(disclaimer_marker)
        if disclaimer_start_index != -1:
            # Take everything from "Disclaimer:" onwards
            epilog_text = current_docstring[disclaimer_start_index:].strip()
        else:
            # Fallback if "Disclaimer:" marker not found in __doc__ but __doc__ exists
            epilog_text = "Disclaimer: Use at your own risk. This script involves real cryptocurrency transactions."
    # --- End Dynamic Epilog/Disclaimer Logic ---

    parser = argparse.ArgumentParser(
        description="--- Boltz LN to L-BTC Swap Initiator ---",
        epilog=epilog_text,
        formatter_class=argparse.RawTextHelpFormatter,  # To preserve newlines in epilog
    )

    # Calculate default config path relative to the script's location
    # Assumes config.ini is in the parent directory of the script's parent directory
    # e.g., if script is /.../project_name/Other/script.py, config.ini is /.../project_name/config.ini
    script_file_path = os.path.abspath(__file__)
    project_root_dir = os.path.dirname(
        os.path.dirname(script_file_path)
    )  # Gets to .../Lightning-Python-Tools/
    default_config_location = os.path.join(project_root_dir, "config.ini")

    parser.add_argument(
        "--amount",
        "-a",
        type=int,
        required=True,
        help="Amount in satoshis for the swap.",
    )
    parser.add_argument(
        "--capacity",
        "-c",
        type=int,
        default=2000000,
        help="Minimum local balance of channels to consider for payment (default: 2,000,000 sats).",
    )
    parser.add_argument(
        "--local-fee-limit",
        "-lfl",
        type=int,
        default=10,
        help="Maximum local routing fee in PPM for LNDg channels to consider for payment (default: 10 ppm).",
    )
    parser.add_argument(
        "--lndg-api",
        type=str,
        default="http://localhost:8080",
        help="LNDg API endpoint (default: http://localhost:8080).",
    )
    parser.add_argument(
        "--payment-timeout",
        "-T",
        type=str,
        default="5m",
        help="Timeout for lncli payment attempts (e.g., 1m, 5m, 10m - default: 5m).",
    )

    parser.add_argument(
        "--ppm",
        "-P",
        type=int,
        help="Maximum fee in Parts Per Million (PPM) for the lncli payment, calculated based on the swap amount. If not set, defaults to 0 sats for lncli --fee_limit.",
    )

    parser.add_argument(
        "--max-parts",
        "-mp",
        type=int,
        default=None,
        help="Maximum number of parts for Multi-Path Payments (MPP) for the lncli payment. If omitted, lncli's default (currently 16) is used. Set to 1 to effectively disable MPP via this flag.",
    )

    parser.add_argument(
        "--config",
        type=str,
        default=default_config_location,  # Use the calculated relative path
        help="Path to the configuration file (default: ../config.ini relative to script's parent dir).",
    )
    parser.add_argument(
        "--description",
        "-d",
        type=str,
        default="LNDg-Boltz-Swap-Out",
        help="Description for the Boltz swap invoice.",
    )
    parser.add_argument(
        "--debug",
        "-D",
        action="store_true",
        help="Enable debug mode (no actual transactions).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose output, showing full details of executed commands like LND connection parameters and detailed error output.",
    )
    parser.add_argument(
        "--max-payment-attempts",
        type=int,
        default=None,
        help="Maximum number of payment attempts with different channel batches (default: try all available candidates in batches of 3).",
    )
    parser.add_argument(
        "--custom-destination-address",
        type=str,
        default=None,
        help="Manually specify the L-BTC destination address. Skips 'pscli' generation. Prompts for confirmation unless --force is used.",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Skip confirmation prompts (e.g., for custom destination address).",
    )
    parser.add_argument(
        "--lncli-cltv-limit",
        type=int,
        default=0,
        help="Set the CLTV limit (max time lock in blocks) for lncli payinvoice/queryroutes. (Default: 0, LND's default). "
        "A lower value might make payments resolve or fail faster.",
    )
    parser.add_argument(
        "--queryroutes-timeout",
        type=str,
        default="20s",
        help="Timeout for an individual lncli queryroutes attempt (e.g., '10s', '30s'). Default: 20s.",
    )
    parser.add_argument(
        "--outgoing-channel-id",
        type=str,
        default=None,
        help="Specify a single outgoing channel ID to use for payment. Skips LNDg API query and batching process. "
        "If specified but no channel ID provided, script will prompt for one.",
    )

    # Using parser.description which should be set from the constructor.
    # No need to check __doc__ here again for the main title printout,
    # as parser.description is used for the help message's title.
    args = parser.parse_args()

    # This part prints the title at the start of the script execution
    # It's separate from the help message's description/epilog
    main_title = "--- Boltz LN to L-BTC Swap Initiator ---"  # Default title
    if current_docstring:
        first_line_match = re.match(r"^\s*---(.*?)---", current_docstring)
        if first_line_match:
            main_title = f"--- {first_line_match.group(1).strip()} ---"
    print(main_title)

    if args.debug:
        print("DEBUG MODE ENABLED: No actual transactions.")
        script_logger.setLevel(logging.DEBUG)  # More verbose logging in debug mode
    print(f"Swap Amount (LN): {args.amount} sats")

    # Only show LNDg-specific settings if not using single channel mode
    if not args.outgoing_channel_id:
        print(f"Min Local Balance (candidates): {args.capacity} sats")
        print(f"Max Local Fee (LNDg candidates): {args.local_fee_limit} ppm")

    print(f"LNDg API: {args.lndg_api}")
    print(f"LNCLI Payment Timeout: {args.payment_timeout}")

    if args.max_parts is not None:
        print(f"LNCLI Max Parts (MPP): {args.max_parts}")
    else:
        print("LNCLI Max Parts (MPP): Using lncli's default (currently 16)")

    if args.ppm is not None:
        calculated_fee_sats = math.floor(args.amount * args.ppm / 1_000_000)
        print(
            f"LNCLI Payment Fee Limit: {args.ppm} PPM (approx. {calculated_fee_sats} sats for this amount)"
        )
    else:
        print(
            "LNCLI Payment Fee Limit: Using 0 sats (explicitly setting --fee_limit 0 for lncli as --ppm not provided)"
        )

    print("LNCLI CLTV Limit: Using LND's default")

    print(f"LNCLI QueryRoutes Timeout per attempt: {args.queryroutes_timeout}")

    # Add verbose status to the initial output
    if args.verbose:
        print(
            "Verbose Mode: ENABLED (showing detailed command output and error details)"
        )

    return args, parser


def load_config(config_file_path):
    """Loads LNDg, Paths, and Boltz RPC configuration from ../config.ini."""
    config = configparser.ConfigParser()

    if not os.path.exists(config_file_path):
        err_msg = f"Error: Configuration file not found at {config_file_path}"
        print_color(err_msg, Colors.FAIL)
        script_logger.error(err_msg)
        print_color(
            f"Please ensure the configuration file exists at the specified path: {config_file_path}",
            Colors.FAIL,
        )
        sys.exit(1)

    config.read(config_file_path)
    app_config = {}

    try:
        # LNDg credentials
        app_config["lndg_api_url"] = config["lndg"]["lndg_api_url"]
        app_config["lndg_username"] = config["credentials"]["lndg_username"]
        app_config["lndg_password"] = config["credentials"]["lndg_password"]

        # Executable Paths
        app_config["lncli_path"] = config["paths"]["lncli_path"]
        app_config["pscli_path"] = config["paths"]["pscli_path"]
        app_config["boltzcli_path"] = config["paths"]["boltzcli_path"]

        # Boltzd RPC connection details for boltzcli
        app_config["boltzd_tlscert_path"] = config["paths"]["boltzd_tlscert_path"]
        app_config["boltzd_admin_macaroon_path"] = config["paths"][
            "boltzd_admin_macaroon_path"
        ]

        # LND connection details (optional, for construct_lncli_command)
        if "lnd" in config:
            app_config["lnd_rpcserver"] = config["lnd"].get("rpcserver")
            app_config["lnd_tlscertpath"] = config["lnd"].get("tlscertpath")
            app_config["lnd_macaroonpath"] = config["lnd"].get("macaroonpath")
        else:  # Ensure keys exist even if section is missing, for consistent access later
            app_config["lnd_rpcserver"] = None
            app_config["lnd_tlscertpath"] = None
            app_config["lnd_macaroonpath"] = None

    except KeyError as e:
        err_msg = f"Error: Missing key {e} in config.ini."
        print_color(err_msg, Colors.FAIL)
        script_logger.error(err_msg)
        print_color(
            f"Please ensure your `config.ini` contains all required LNDg credentials, paths, and boltzd RPC details.",
            Colors.FAIL,
        )
        sys.exit(1)
    except configparser.NoSectionError as e:
        err_msg = f"Error: Missing section {e.section} in config.ini."
        print_color(err_msg, Colors.FAIL)
        script_logger.error(err_msg)
        print_color(
            "Please ensure your `config.ini` has [lndg], [credentials], and [paths] sections.",
            Colors.FAIL,
        )
        sys.exit(1)
    return app_config


def run_command(
    command_parts,
    timeout=None,
    debug=False,
    dry_run_output="DRY RUN COMMAND",
    expect_json=False,
    success_codes=None,
    display_str_override=None,
    attempt_graceful_terminate_on_timeout=False,
    verbose=False,  # Add verbose parameter
):
    """
    Executes a system command.
    Returns a tuple: (success: bool, output: str or dict if expect_json else str, error_message: str)
    """
    if success_codes is None:
        success_codes = [0]

    # actual_command_str is always the full command, used for debug prints and dry run info
    actual_command_str = " ".join(command_parts)

    # string_to_print is what's shown on the "Executing:" line
    # It defaults to the actual_command_str unless overridden
    string_to_print = (
        display_str_override if display_str_override is not None else actual_command_str
    )

    print_color(f"Executing: {string_to_print}", Colors.OKCYAN)
    script_logger.debug(f"Executing command: {actual_command_str}")

    if debug:
        # The [DEBUG] {dry_run_output}: line should show the *actual* command
        print_color(f"[DEBUG] {dry_run_output}: {actual_command_str}", Colors.WARNING)
        script_logger.debug(f"[DEBUG] Dry Run: {actual_command_str}")
        if expect_json:
            # Check for pscli lbtc-getaddress
            if "lbtc-getaddress" in command_parts and "pscli" in command_parts[0]:
                return True, {"address": "debug_lq_address_from_dry_run"}, ""
            # Check for boltzcli createreverseswap
            elif (
                "createreverseswap" in command_parts and "boltzcli" in command_parts[0]
            ):
                return (
                    True,
                    {
                        "id": "debug_swap_id_reverse",
                        "invoice": "lnbc100n1pj9z6jusp5cnp9j5f2x0m5z5z5z5z5z5z5z5z5z5z5z5z5z5z5z5z5z5z5z5zqdqqcqzysxqyz5vqsp5h3xkct7t8xsv9c8g6q0v5rk2h3psk32k0k7k0kj7nhsz3g0qqqqqysgqqqqqqlgqqqqqqgq9q9qyysgqhc9sqgmeyr7n6y240g5uqn9g786ftwpk87kr0lgz03f5ljh3djxsnx0wsyv3g6n74s630jtzs8ajv473rws7z88x2u7f8n5f7n2vkt9vq0p5hdxz",  # Dummy LN invoice
                        "blindingKey": "debug_blinding_key",
                        "lockupAddress": "debug_boltz_lockup_address",
                        "expectedAmount": 9950,  # Example: amount after potential fees
                        "timeoutBlockHeight": 123456,
                        "onchainFee": 50,  # Example
                    },
                    "",
                )
            # Check for lncli payinvoice
            elif "payinvoice" in command_parts and "lncli" in command_parts[0]:
                return (
                    True,
                    {
                        "payment_error": "",
                        "payment_preimage": "debug_preimage_from_dry_run",
                        "payment_route": {
                            "total_amt_msat": "10000000",
                            "hops": [],
                        },  # Dummy route
                    },
                    "",
                )
            else:  # Default mock JSON if not specifically handled
                print_color(
                    f"[DEBUG] No specific mock for: {actual_command_str}. Using default mock.",
                    Colors.WARNING,
                )
                return (
                    True,
                    {
                        "message": "Dry run success - default mock",
                        "details": actual_command_str,
                    },
                    "",
                )
        return True, f"Dry run: {actual_command_str}", ""

    try:
        process = subprocess.Popen(
            command_parts, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        stdout, stderr = process.communicate(timeout=timeout)
        return_code = process.returncode

        if return_code in success_codes:
            script_logger.debug(
                f"Command successful (exit code {return_code}): {actual_command_str}"
            )
            if expect_json:
                try:
                    return True, json.loads(stdout), ""
                except json.JSONDecodeError as e:
                    err_msg = f"Failed to decode JSON from command output: {e}\nOutput: {stdout}"
                    script_logger.error(f"{err_msg} for command: {actual_command_str}")
                    return (
                        False,
                        stdout,
                        err_msg,
                    )
            return True, stdout.strip(), ""
        else:
            # Only show detailed output if verbose is enabled
            if verbose:
                error_msg = f"Command failed with exit code {return_code}.\nStderr: {stderr.strip()}\nStdout: {stdout.strip()}"
                print_color(error_msg, Colors.FAIL)
            else:
                # Show minimal error info
                error_msg = f"Command failed with exit code {return_code}."
                if stderr.strip():
                    error_msg += f" Stderr: {stderr.strip()}"
                print_color(error_msg, Colors.FAIL)

            script_logger.error(
                f"Command failed (exit code {return_code}): {actual_command_str}"
            )
            return False, stdout.strip(), stderr.strip()

    except FileNotFoundError:
        error_msg = f"Error: Command not found: {command_parts[0]}. Please check the path in your config.ini."
        print_color(error_msg, Colors.FAIL)
        script_logger.error(error_msg)
        return False, "", error_msg
    except subprocess.TimeoutExpired:
        error_msg = f"Command timed out after {timeout} seconds: {actual_command_str}"
        print_color(error_msg, Colors.FAIL)
        script_logger.warning(error_msg)
        if process.poll() is None:  # Check if process is still running
            if attempt_graceful_terminate_on_timeout:
                print_color(
                    "  Attempting graceful termination (SIGINT)...", Colors.WARNING
                )
                process.send_signal(signal.SIGINT)
                try:
                    # Give lncli a bit more time to react to SIGINT and potentially
                    # communicate cancellation to LND, especially with --cancelable
                    process.wait(timeout=5)
                    print_color(
                        "  Process terminated gracefully after SIGINT.", Colors.OKCYAN
                    )
                except subprocess.TimeoutExpired:
                    print_color(
                        "  Process did not terminate via SIGINT within timeout, resorting to SIGKILL...",
                        Colors.WARNING,
                    )
                    if process.poll() is None:  # Check again before SIGKILL
                        process.kill()
                        print_color("  Process terminated via SIGKILL.", Colors.WARNING)
            else:
                process.kill()  # Original SIGKILL behavior if not attempting graceful

            # Get any final output after attempting to terminate/kill
            try:
                stdout_after_kill, stderr_after_kill = process.communicate(timeout=5)
                if verbose:
                    error_msg += f"\nProcess terminated. Stdout after: {stdout_after_kill.strip()}. Stderr after: {stderr_after_kill.strip()}"
                else:
                    error_msg += "\nProcess terminated."
            except subprocess.TimeoutExpired:
                error_msg += "\nProcess terminated. Failed to get additional output after termination."
            except Exception as e:
                if verbose:
                    error_msg += (
                        f"\nProcess terminated. Error getting additional output: {e}"
                    )
                else:
                    error_msg += "\nProcess terminated."
        script_logger.error(f"Command timeout details: {error_msg}")
        return False, "", error_msg
    except Exception as e:
        error_msg = f"An unexpected error occurred while running command: {actual_command_str}\nError: {e}"
        print_color(error_msg, Colors.FAIL)
        script_logger.exception(
            f"Unexpected error running command: {actual_command_str}"
        )
        return False, "", error_msg


def get_lbtc_address(pscli_path, debug, args):  # Add args parameter
    """Fetches a new L-BTC address using pscli."""
    print_color("\nStep 1: Fetching new L-BTC address...", Colors.HEADER)
    command = [pscli_path, "lbtc-getaddress"]
    success, output, error = run_command(
        command,
        debug=debug,
        expect_json=True,
        dry_run_output="L-BTC address command",
        verbose=args.verbose if hasattr(args, "verbose") else False,
    )

    if success and isinstance(output, dict) and "address" in output:
        address = output["address"]
        print_color(f"Successfully fetched L-BTC address: {address}", Colors.OKGREEN)
        script_logger.info(f"Fetched L-BTC address: {address}")
        return address
    else:
        print_color("Failed to get L-BTC address.", Colors.FAIL)
        script_logger.error(
            f"Failed to get L-BTC address. Error: {error}, Output: {output}"
        )
        if error:
            print_color(f"Error details: {error}", Colors.FAIL)
        if isinstance(output, str) and output:
            print_color(f"Raw output: {output}", Colors.FAIL)
        elif not isinstance(output, dict):
            print_color(
                f"Unexpected output type: {type(output)}, value: {output}", Colors.FAIL
            )
        return None


def get_swap_candidate_channels(
    lndg_api_url,
    lndg_username,
    lndg_password,
    capacity_threshold,
    local_fee_limit_ppm,
    debug,
):
    """
    Retrieves and filters channel information from LNDg API.
    Returns a list of dictionaries, each representing a candidate channel,
    sorted by local_balance in descending order.
    Each dictionary: {'id': str, 'balance': int, 'alias': str, 'fee_rate': int}
    """
    print_color(
        f"\nStep 2: Finding swap candidate channels (Min Indiv. Local Bal > {capacity_threshold} sats, Local Fee <= {local_fee_limit_ppm} ppm)...",
        Colors.HEADER,
    )
    api_url = lndg_api_url + "/api/channels?limit=5000&is_open=true&is_active=true"
    candidate_channels_info = []

    if debug:
        print_color(
            "[DEBUG] Skipping LNDg API call in debug mode. Using mock channel data.",
            Colors.WARNING,
        )
        script_logger.debug("Using mock channel data for LNDg.")
        # Returning richer mock data
        return [
            {
                "id": "mock_chan_id_1",
                "balance": 3000000,
                "alias": "MockChannel1",
                "fee_rate": 0,
            },
            {
                "id": "mock_chan_id_2",
                "balance": 2500000,
                "alias": "MockChannel2",
                "fee_rate": 1,
            },
            {
                "id": "mock_chan_id_3",
                "balance": 2000000,
                "alias": "MockChannel3",
                "fee_rate": 2,
            },
            {
                "id": "mock_chan_id_4",
                "balance": 1500000,
                "alias": "MockChannel4",
                "fee_rate": 3,
            },
            {
                "id": "mock_chan_id_5",
                "balance": 1000000,
                "alias": "MockChannel5",
                "fee_rate": 4,
            },
            {
                "id": "mock_chan_id_6",
                "balance": 500000,
                "alias": "MockChannel6",
                "fee_rate": 5,
            },
        ]

    try:
        response = requests.get(
            api_url, auth=(lndg_username, lndg_password), timeout=30
        )
        response.raise_for_status()
        data = response.json()

        if "results" in data:
            results = data["results"]
            # Sort by local_balance descending first
            sorted_results = sorted(
                results,
                key=lambda x: x.get("local_balance", 0),
                reverse=True,
            )
            for channel in sorted_results:
                local_fee_rate = channel.get("local_fee_rate", float("inf"))
                local_balance = channel.get("local_balance", 0)
                chan_id = channel.get("chan_id", "")
                alias = channel.get("alias", "N/A")

                # Filter based on individual channel properties
                if (
                    local_balance
                    > capacity_threshold  # Min local balance for a single channel to be considered
                    and local_fee_rate <= local_fee_limit_ppm
                    and chan_id
                ):
                    candidate_channels_info.append(
                        {
                            "id": chan_id,
                            "balance": local_balance,
                            "alias": alias,
                            "fee_rate": local_fee_rate,
                        }
                    )
                    print_color(
                        f"  Found candidate: {alias} ({chan_id}), Local Bal: {local_balance}, Fee: {local_fee_rate}ppm",
                        Colors.OKGREEN,
                    )
            if not candidate_channels_info:
                print_color(
                    "No suitable swap candidate channels found based on individual criteria.",
                    Colors.WARNING,
                )
                script_logger.warning(
                    "No suitable swap candidate channels found via LNDg."
                )
            else:
                print_color(
                    f"Found {len(candidate_channels_info)} candidate channels (sorted by local balance).",
                    Colors.OKGREEN,
                )
                script_logger.info(
                    f"Found {len(candidate_channels_info)} LNDg candidate channels."
                )
        else:
            print_color("LNDg API response missing 'results'.", Colors.FAIL)
            script_logger.error(
                f"LNDg API response missing 'results'. Response: {data}"
            )
            print_color(f"Response: {data}", Colors.FAIL)
    except requests.exceptions.Timeout:
        err_msg = f"Timeout connecting to LNDg API at {api_url}"
        print_color(err_msg, Colors.FAIL)
        script_logger.error(err_msg)
    except requests.exceptions.HTTPError as e:
        err_msg = f"HTTP error connecting to LNDg API: {e}"
        print_color(err_msg, Colors.FAIL)
        if e.response is not None:
            print_color(f"Response content: {e.response.text}", Colors.FAIL)
            script_logger.error(f"{err_msg}. Response: {e.response.text}")
        else:
            script_logger.error(err_msg)
    except requests.exceptions.RequestException as e:
        err_msg = f"Error connecting to LNDg API: {e}"
        print_color(err_msg, Colors.FAIL)
        script_logger.error(err_msg)
    except json.JSONDecodeError:
        err_msg = f"Error decoding JSON from LNDg API response: {response.text if 'response' in locals() else 'N/A'}"
        print_color(err_msg, Colors.FAIL)
        script_logger.error(err_msg)
    except Exception as e:
        err_msg = f"An unexpected error occurred while fetching channels: {e}"
        print_color(err_msg, Colors.FAIL)
        script_logger.exception(err_msg)
    return candidate_channels_info


def create_boltz_swap(
    boltzcli_path,
    boltzd_tlscert_path,
    boltzd_admin_macaroon_path,
    swap_amount_sats,
    lbtc_address,
    description,
    debug,
    args,  # Add args parameter
):
    """
    Initiates a reverse swap (LN -> L-BTC) using `boltzcli createreverseswap`.
    Includes TLS cert and macaroon for `boltzd` authentication.
    Returns a tuple (swap_id, lightning_invoice, full_boltz_response_dict) or (None, None, None) on failure.
    """
    print_color(
        f"\nStep 3: Creating Boltz reverse swap for {swap_amount_sats} sats to {lbtc_address}...",
        Colors.HEADER,
    )
    currency_pair = "L-BTC"

    command = [
        boltzcli_path,
        "--tlscert",
        boltzd_tlscert_path,
        "--macaroon",
        boltzd_admin_macaroon_path,
        "createreverseswap",
        "--json",
        "--external-pay",
    ]
    if description:
        command.extend(["--description", description])

    command.extend([currency_pair, str(swap_amount_sats), lbtc_address])

    success, output, error = run_command(
        command,
        debug=debug,
        expect_json=True,
        dry_run_output="Boltz `createreverseswap` command",
        verbose=args.verbose if hasattr(args, "verbose") else False,
    )

    if success and isinstance(output, dict):
        swap_id = output.get("id")
        invoice = output.get("invoice")
        lockup_address = output.get("lockupAddress")
        expected_onchain_amount = output.get("expectedAmount")
        timeout_block = output.get("timeoutBlockHeight")

        if not invoice:
            err_msg = "Boltz response missing 'invoice'. Cannot proceed."
            print_color(err_msg, Colors.FAIL)
            script_logger.error(
                f"{err_msg} Full Boltz response: {json.dumps(output, indent=2)}"
            )
            print_color(f"Full response: {json.dumps(output, indent=2)}", Colors.FAIL)
            return None, None, output
        if not swap_id:
            warn_msg = "Warning: Boltz response missing 'id'. Using 'unknown_swap_id'."
            print_color(warn_msg, Colors.WARNING)
            script_logger.warning(warn_msg)
            swap_id = "unknown_swap_id"

        print_color(f"Boltz reverse swap created successfully!", Colors.OKGREEN)
        script_logger.info(
            f"Boltz reverse swap created. ID: {swap_id}, Amount: {swap_amount_sats}, L-BTC Dest: {lbtc_address}, Invoice: {invoice[:40]}..."
        )
        print_color(f"  Swap ID: {swap_id}", Colors.OKGREEN)
        print_color(
            f"  Lightning Invoice (to pay Boltz): {invoice[:40]}...{invoice[-40:]}",
            Colors.OKGREEN,
        )
        if lockup_address:
            print_color(
                f"  Boltz Lockup Address (L-BTC): {lockup_address}", Colors.OKGREEN
            )
        if expected_onchain_amount is not None:
            print_color(
                f"  Expected L-BTC from Boltz (onchain): {expected_onchain_amount} sats",
                Colors.OKGREEN,
            )
        if timeout_block:
            print_color(f"  Swap Timeout Block Height: {timeout_block}", Colors.OKGREEN)
        return swap_id, invoice, output
    else:
        err_msg = "Failed to create Boltz reverse swap."
        print_color(err_msg, Colors.FAIL)
        script_logger.error(
            f"{err_msg} Error: {error}, Output: {json.dumps(output, indent=2) if isinstance(output, dict) else output}"
        )
        if error:
            print_color(f"Error details: {error}", Colors.FAIL)
        if isinstance(output, str) and output:
            print_color(f"Command output (str): {output}", Colors.FAIL)
        elif isinstance(output, dict):
            print_color(
                f"Command output (JSON): {json.dumps(output, indent=2)}", Colors.FAIL
            )
        return None, None, None


# Constants for retry logic
# MAX_IN_TRANSITION_RETRIES was here, but the new logic is more event-driven.
IN_TRANSITION_RETRY_INITIAL_DELAY_SECONDS = 30
SUBPROCESS_TIMEOUT_BUFFER_SECONDS = (
    30  # Buffer for subprocess.communicate over lncli's own timeout
)
POST_LNCLI_TIMEOUT_CLEANUP_DELAY_SECONDS = (
    30  # Additional delay after script times out lncli payinvoice
)
# Delay between DIFFERENT batches is hardcoded further down as 30 seconds.


def pay_lightning_invoice(
    config,
    args,
    invoice_str,
    candidate_channels_info,  # Now a list of dicts
    debug,
):
    """
    Pays the Lightning invoice using lncli with retries on dynamically formed channel batches.
    Batches are formed to have a total liquidity of at least 1.5x swap amount.
    Includes logic to retry the same batch on "payment is in transition".
    Returns True on success, False on failure.
    """
    print_color(
        f"\nStep 4: Attempting to pay Lightning invoice: {invoice_str[:40]}...{invoice_str[-40:]}",
        Colors.HEADER,
    )
    max_parts_display = (
        args.max_parts if args.max_parts is not None else "lncli default"
    )
    print_color(
        f"Timeout per attempt (lncli --timeout): {args.payment_timeout}, Max Parts: {max_parts_display}",
        Colors.OKCYAN,
    )
    if args.lncli_cltv_limit > 0:
        print_color(
            f"Overall CLTV Limit for payments/queries: {args.lncli_cltv_limit} blocks",
            Colors.OKCYAN,
        )

    decoded_invoice = decode_payreq(config, args, invoice_str)
    if not decoded_invoice:
        print_color("Failed to decode Boltz invoice, cannot proceed.", Colors.FAIL)
        script_logger.error(f"Failed to decode Boltz invoice: {invoice_str[:40]}...")
        return False, None  # Return payment_preimage as None

    invoice_destination_pubkey = decoded_invoice.get("destination")
    payment_hash = decoded_invoice.get("payment_hash")
    payment_preimage = None  # Initialize

    if not invoice_destination_pubkey or not payment_hash:
        print_color(
            "Decoded invoice missing destination pubkey or payment hash.", Colors.FAIL
        )
        script_logger.error(
            f"Decoded invoice {invoice_str[:40]}... missing destination or payment_hash. Decoded: {decoded_invoice}"
        )
        return False, None
    print_color(
        f"Invoice destination for QueryRoutes: {invoice_destination_pubkey}",
        Colors.OKCYAN,
    )
    print_color(f"Payment hash for tracking: {payment_hash}", Colors.OKCYAN)
    script_logger.info(
        f"Attempting payment for hash: {payment_hash}, destination: {invoice_destination_pubkey}"
    )

    if not candidate_channels_info:
        print_color("No candidate channels for payment. Aborting.", Colors.FAIL)
        script_logger.warning("No candidate channels for LN payment.")
        return False, None

    swap_amount_sats = args.amount
    min_chunk = max(1, swap_amount_sats // max(1, len(candidate_channels_info)))
    target_liquidity = int(swap_amount_sats * 1.10)

    channel_idx = 0  # Tracks overall progress through candidate_channels_info
    used_channel_ids = (
        set()
    )  # Keep track of all channels ever successfully probed for any batch

    MAX_CHANNELS_IN_BATCH = 10
    ENRICHMENT_ADD_LIMIT = 5

    # Outer loop for trying new "primary" batches
    while True:
        selected_channels = []
        current_batch_accumulated_liquidity = 0

        print_color(
            f"\n--- Building New Payment Batch (starting from overall candidate index {channel_idx}) ---",
            Colors.OKBLUE,
        )

        # --- Build/Rebuild a primary batch ---
        initial_batch_building_channel_idx = channel_idx
        temp_newly_selected_channels_for_this_batch = []

        while (
            initial_batch_building_channel_idx < len(candidate_channels_info)
            and len(temp_newly_selected_channels_for_this_batch) < MAX_CHANNELS_IN_BATCH
            and current_batch_accumulated_liquidity < target_liquidity
        ):

            channel_candidate = candidate_channels_info[
                initial_batch_building_channel_idx
            ]
            chan_id_candidate = channel_candidate["id"]

            if chan_id_candidate in used_channel_ids:
                initial_batch_building_channel_idx += 1
                continue

            lndg_local_balance = channel_candidate["balance"]
            initial_probe_sats = max(1, int(lndg_local_balance * 0.9))
            probe_amounts = [
                initial_probe_sats,
                max(1, initial_probe_sats // 2),
                max(1, initial_probe_sats // 4),
            ]

            probed_successfully_for_initial_batch = False
            channel_permanently_skipped_no_path = False

            for amt in probe_amounts:
                if amt < min_chunk:
                    continue

                lncli_path = config.get("lncli_path", "/usr/local/bin/lncli")
                lnd_connection_params = [
                    "--rpcserver=" + (config.get("lnd_rpcserver") or "localhost:10009"),
                    "--tlscertpath="
                    + os.path.expanduser(
                        config.get("lnd_tlscertpath") or "~/.lnd/tls.cert"
                    ),
                    "--macaroonpath="
                    + os.path.expanduser(
                        config.get("lnd_macaroonpath")
                        or "~/.lnd/data/chain/bitcoin/mainnet/admin.macaroon"
                    ),
                ]
                query_command_parts = [
                    lncli_path,
                    *lnd_connection_params,
                    "queryroutes",
                    "--dest",
                    invoice_destination_pubkey,
                    "--amt",
                    str(amt),
                    "--outgoing_chan_id",
                    str(chan_id_candidate),
                ]
                if args.ppm is not None:
                    fee_limit_sats = math.floor(args.amount * args.ppm / 1_000_000)
                    query_command_parts.extend(["--fee_limit", str(fee_limit_sats)])
                else:
                    query_command_parts.extend(["--fee_limit", "0"])
                if args.lncli_cltv_limit > 0:
                    query_command_parts.extend(
                        ["--cltv_limit", str(args.lncli_cltv_limit)]
                    )

                query_timeout_val = 30
                try:
                    if args.queryroutes_timeout.lower().endswith("s"):
                        query_timeout_val = int(args.queryroutes_timeout[:-1])
                    elif args.queryroutes_timeout.lower().endswith("m"):
                        query_timeout_val = int(args.queryroutes_timeout[:-1]) * 60
                    query_timeout_val += 10
                except ValueError:
                    pass

                qr_success, qr_output, qr_error_detail = run_command(
                    query_command_parts,
                    timeout=query_timeout_val,
                    debug=args.debug,
                    expect_json=True,
                    dry_run_output=f"lncli queryroutes (batch build) via {channel_candidate['alias']}({chan_id_candidate}) for {amt} sats",
                )
                if (
                    qr_success
                    and isinstance(qr_output, dict)
                    and qr_output.get("routes")
                ):
                    print_color(
                        f"  Added to batch: {channel_candidate['alias']} ({chan_id_candidate}) with {amt} sats.",
                        Colors.OKGREEN,
                    )
                    temp_newly_selected_channels_for_this_batch.append(
                        {
                            "id": chan_id_candidate,
                            "amount": amt,
                            "alias": channel_candidate["alias"],
                        }
                    )
                    current_batch_accumulated_liquidity += amt
                    # used_channel_ids.add(chan_id_candidate) # Add to used_channel_ids ONLY when successfully probed and added
                    probed_successfully_for_initial_batch = True
                    break
                elif (
                    not qr_success
                    and "unable to find a path to destination"
                    in str(qr_error_detail).lower()
                ):
                    print_color(
                        f"  Channel {channel_candidate['alias']} ({chan_id_candidate}) permanently skipped: no path to destination.",
                        Colors.FAIL,
                    )
                    used_channel_ids.add(
                        chan_id_candidate
                    )  # Mark globally unusable due to no path
                    channel_permanently_skipped_no_path = True
                    break  # Stop probing this channel for any amount

            if probed_successfully_for_initial_batch:
                used_channel_ids.add(
                    chan_id_candidate
                )  # Now mark as used because it's in a batch

            initial_batch_building_channel_idx += 1
            if (
                (
                    probed_successfully_for_initial_batch
                    and len(temp_newly_selected_channels_for_this_batch)
                    >= MAX_CHANNELS_IN_BATCH
                )
                or channel_permanently_skipped_no_path
                and not probed_successfully_for_initial_batch
            ):  # If skipped, effectively done with this slot
                # if channel_permanently_skipped_no_path, we just move to next candidate_channel_info index
                # if batch is full, then break
                if (
                    len(temp_newly_selected_channels_for_this_batch)
                    >= MAX_CHANNELS_IN_BATCH
                ):
                    break

        selected_channels.extend(temp_newly_selected_channels_for_this_batch)
        channel_idx = initial_batch_building_channel_idx

        if not selected_channels:
            print_color(
                "No more candidate channels available to form any payment batch.",
                Colors.FAIL,
            )
            script_logger.warning(
                "No more candidate channels to form a payment batch for LN payment."
            )
            return False, None

        print_color(
            f"Formed batch with {len(selected_channels)} channels, total probed liquidity: {current_batch_accumulated_liquidity} sats.",
            Colors.OKCYAN,
        )
        script_logger.info(
            f"Formed payment batch with {len(selected_channels)} channels, liquidity: {current_batch_accumulated_liquidity} sats."
        )

        # --- Inner loop for payment attempts and enrichment of the current primary batch ---
        while True:
            if (
                not selected_channels
            ):  # Should not happen if outer loop logic is correct
                print_color(
                    "Error: Inner loop started with no selected channels.", Colors.FAIL
                )
                script_logger.error(
                    "Pay_lightning_invoice: Inner loop started with no selected channels."
                )
                break  # Break inner to re-evaluate in outer

            outgoing_chan_ids = [ch["id"] for ch in selected_channels]
            actual_command_list, display_command_str = construct_lncli_command(
                config, args, invoice_str, outgoing_chan_ids
            )

            # --- Prepay Probe Step ---
            probe_success, probe_error = send_prepay_probe(
                config,
                args,
                invoice_destination_pubkey,
                swap_amount_sats,
                outgoing_chan_ids,
            )
            if not probe_success:
                print_color(
                    f"Prepay probe failed for this batch: {probe_error}", Colors.WARNING
                )
                script_logger.warning(
                    f"Prepay probe failed for this batch: {probe_error}"
                )
                # Skip this batch, try next
                break  # Break inner loop, outer loop will try next batch
            else:
                print_color(
                    "Prepay probe succeeded (route is viable). Proceeding with real payment.",
                    Colors.OKGREEN,
                )
                # Proceed to real payment as before

            lncli_timeout_val = 300  # Default
            try:
                if args.payment_timeout.lower().endswith("s"):
                    lncli_timeout_val = int(args.payment_timeout[:-1])
                elif args.payment_timeout.lower().endswith("m"):
                    lncli_timeout_val = int(args.payment_timeout[:-1]) * 60
                elif args.payment_timeout.lower().endswith("h"):
                    lncli_timeout_val = int(args.payment_timeout[:-1]) * 3600
                else:
                    lncli_timeout_val = int(args.payment_timeout)
            except ValueError:
                print_color(
                    f"Invalid payment timeout format: {args.payment_timeout}. Using default {lncli_timeout_val}s.",
                    Colors.WARNING,
                )
                script_logger.warning(
                    f"Invalid payment timeout format: {args.payment_timeout}. Using default {lncli_timeout_val}s."
                )

            script_subprocess_timeout = (
                lncli_timeout_val + SUBPROCESS_TIMEOUT_BUFFER_SECONDS
            )

            print_color(
                f"\nAttempting payment with {len(outgoing_chan_ids)} channels (batch liquidity {current_batch_accumulated_liquidity} sats): {', '.join(outgoing_chan_ids)}",
                Colors.OKBLUE,
            )
            script_logger.info(
                f"Attempting LN payment via channels: {', '.join(outgoing_chan_ids)}"
            )

            payinvoice_success, payinvoice_output_json, payinvoice_error_stderr = (
                run_command(
                    actual_command_list,
                    timeout=script_subprocess_timeout,
                    debug=args.debug,
                    expect_json=True,
                    dry_run_output="lncli payinvoice",
                    success_codes=[0],
                    display_str_override=display_command_str,
                    attempt_graceful_terminate_on_timeout=True,
                    verbose=args.verbose,  # Add this line
                )
            )

            payment_resolved_by_payinvoice = False
            payment_needs_tracking = False

            if payinvoice_success and isinstance(payinvoice_output_json, dict):
                payment_error = payinvoice_output_json.get("payment_error", "")
                temp_preimage = payinvoice_output_json.get("payment_preimage", "")

                if not payment_error and temp_preimage:
                    print_color(
                        f"lncli payinvoice reported IMMEDIATE SUCCESS. Preimage: {temp_preimage[:10]}...",
                        Colors.OKGREEN,
                        bold=True,
                    )
                    script_logger.info(
                        f"LN Payment SUCCEEDED (via payinvoice). Hash: {payment_hash}, Preimage: {temp_preimage}"
                    )
                    payment_preimage = temp_preimage
                    return (
                        True,
                        payment_preimage,
                    )  # Overall success for pay_lightning_invoice
                elif (
                    "payment is in transition" in payment_error.lower()
                    or "htlc propagation" in payment_error.lower()
                    or (
                        payment_error
                        and "Unknown" in payment_error
                        and not temp_preimage
                    )
                ):  # LND can return Unknown on long-running ones
                    print_color(
                        f"lncli payinvoice reports payment in transition or status ambiguous: '{payment_error}'. Will track.",
                        Colors.WARNING,
                    )
                    script_logger.warning(
                        f"LN Payment in transition (via payinvoice output: {payment_error}). Hash: {payment_hash}. Proceeding to track."
                    )
                    payment_needs_tracking = True
                else:  # Some other error from payinvoice or no preimage
                    print_color(
                        f"lncli payinvoice reported failure: {payment_error if payment_error else 'No preimage and no specific error'}",
                        Colors.FAIL,
                    )
                    script_logger.warning(
                        f"LN Payment FAILED (via payinvoice). Hash: {payment_hash}. Error: '{payment_error}', Output: {payinvoice_output_json}"
                    )
                    payment_resolved_by_payinvoice = True  # Failed
            elif "Command timed out after" in str(payinvoice_error_stderr):
                print_color(
                    "lncli payinvoice command timed out in script. Payment status unknown. Will track.",
                    Colors.WARNING,
                )
                script_logger.warning(
                    f"lncli payinvoice command timed out in script for hash {payment_hash}. Proceeding to track."
                )
                payment_needs_tracking = True
                if args.debug:  # Simulate payinvoice timeout for debug tracking path
                    print_color(
                        "[DEBUG] Simulating payinvoice timeout, forcing trackpayment path.",
                        Colors.WARNING,
                    )
            else:  # payinvoice_success was False, or output wasn't expected JSON
                print_color(
                    f"lncli payinvoice command failed or produced unexpected output. Stderr: {payinvoice_error_stderr}",
                    Colors.FAIL,
                )
                script_logger.warning(
                    f"LN Payment FAILED (payinvoice command failed). Hash: {payment_hash}. Stderr: {payinvoice_error_stderr}, Output: {payinvoice_output_json}"
                )
                payment_resolved_by_payinvoice = True  # Failed

            if (
                payment_resolved_by_payinvoice
            ):  # This means it definitively failed based on payinvoice output
                # Proceed to enrichment / next batch logic directly
                print_color(
                    "Payment attempt FAILED based on direct payinvoice output. Evaluating enrichment.",
                    Colors.WARNING,
                )
                # Fall through to enrichment logic below this block

            if payment_needs_tracking:
                print_color(
                    "\n--- Payment Status Ambiguous or In-Flight, Initiating Tracking ---",
                    Colors.HEADER,
                )
                # Use a tracking timeout based on the payment timeout plus a buffer
                tracking_timeout_for_attempt = lncli_timeout_val + 60
                payment_succeeded_via_tracking, tracked_preimage = (
                    track_payment_until_settled(
                        config, args, payment_hash, tracking_timeout_for_attempt, debug
                    )
                )

                if payment_succeeded_via_tracking:
                    print_color(
                        "Payment confirmed SUCCEEDED by trackpayment.",
                        Colors.OKGREEN,
                        bold=True,
                    )
                    script_logger.info(
                        f"LN Payment SUCCEEDED (via trackpayment). Hash: {payment_hash}, Preimage: {tracked_preimage}"
                    )
                    payment_preimage = tracked_preimage
                    return True, payment_preimage  # Overall success
                else:
                    print_color(
                        "Payment did NOT SUCCEED via trackpayment (or tracking timed out). Evaluating enrichment.",
                        Colors.WARNING,
                    )
                    script_logger.warning(
                        f"LN Payment FAILED or timed out (via trackpayment). Hash: {payment_hash}."
                    )
                    # Fall through to enrichment logic

            # --- Enrichment or move to next batch ---
            # This block is reached if:
            # 1. payinvoice failed definitively.
            # 2. payinvoice needed tracking, and tracking failed or timed out.

            newly_added_channels_in_enrich_pass = 0
            if len(selected_channels) < MAX_CHANNELS_IN_BATCH and channel_idx < len(
                candidate_channels_info
            ):
                print_color(
                    f"Attempting to enrich current batch (size {len(selected_channels)}, max {MAX_CHANNELS_IN_BATCH})...",
                    Colors.OKBLUE,
                )

                enrich_loop_idx = channel_idx
                channels_actually_added_this_enrich = 0

                while (
                    enrich_loop_idx < len(candidate_channels_info)
                    and len(selected_channels) < MAX_CHANNELS_IN_BATCH
                    and channels_actually_added_this_enrich < ENRICHMENT_ADD_LIMIT
                ):

                    channel_to_enrich = candidate_channels_info[enrich_loop_idx]
                    chan_id_to_enrich = channel_to_enrich["id"]

                    if chan_id_to_enrich in used_channel_ids:
                        enrich_loop_idx += 1
                        continue

                    lndg_bal_enrich = channel_to_enrich["balance"]
                    probe_sats_enrich = max(1, int(lndg_bal_enrich * 0.9))
                    enrich_probe_amts = [
                        probe_sats_enrich,
                        max(1, probe_sats_enrich // 2),
                        max(1, probe_sats_enrich // 4),
                    ]

                    probed_enrich_successfully = False
                    channel_permanently_skipped_no_path_enrich = False

                    for amt_enrich in enrich_probe_amts:
                        if amt_enrich < min_chunk:
                            continue

                        lncli_path = config.get("lncli_path", "/usr/local/bin/lncli")
                        lnd_connection_params = [
                            "--rpcserver="
                            + (config.get("lnd_rpcserver") or "localhost:10009"),
                            "--tlscertpath="
                            + os.path.expanduser(
                                config.get("lnd_tlscertpath") or "~/.lnd/tls.cert"
                            ),
                            "--macaroonpath="
                            + os.path.expanduser(
                                config.get("lnd_macaroonpath")
                                or "~/.lnd/data/chain/bitcoin/mainnet/admin.macaroon"
                            ),
                        ]
                        qr_cmd_enrich = [
                            lncli_path,
                            *lnd_connection_params,
                            "queryroutes",
                            "--dest",
                            invoice_destination_pubkey,
                            "--amt",
                            str(amt_enrich),
                            "--outgoing_chan_id",
                            str(chan_id_to_enrich),
                        ]
                        if args.ppm is not None:
                            fee_limit_sats = math.floor(
                                args.amount * args.ppm / 1_000_000
                            )
                            qr_cmd_enrich.extend(["--fee_limit", str(fee_limit_sats)])
                        else:
                            qr_cmd_enrich.extend(["--fee_limit", "0"])
                        if args.lncli_cltv_limit > 0:
                            qr_cmd_enrich.extend(
                                ["--cltv_limit", str(args.lncli_cltv_limit)]
                            )

                        qr_timeout_val_enrich = 30
                        try:
                            if args.queryroutes_timeout.lower().endswith("s"):
                                qr_timeout_val_enrich = int(
                                    args.queryroutes_timeout[:-1]
                                )
                            elif args.queryroutes_timeout.lower().endswith("m"):
                                qr_timeout_val_enrich = (
                                    int(args.queryroutes_timeout[:-1]) * 60
                                )
                            qr_timeout_val_enrich += 10
                        except ValueError:
                            pass

                        qr_succ_enrich, qr_out_enrich, qr_err_detail_enrich = (
                            run_command(
                                qr_cmd_enrich,
                                timeout=qr_timeout_val_enrich,
                                debug=args.debug,
                                expect_json=True,
                                dry_run_output=f"lncli queryroutes (enrich) via {channel_to_enrich['alias']}({chan_id_to_enrich}) for {amt_enrich} sats",
                            )
                        )
                        if (
                            qr_succ_enrich
                            and isinstance(qr_out_enrich, dict)
                            and qr_out_enrich.get("routes")
                        ):
                            print_color(
                                f"  Enriched batch with: {channel_to_enrich['alias']} ({chan_id_to_enrich}) for {amt_enrich} sats.",
                                Colors.OKGREEN,
                            )
                            selected_channels.append(
                                {
                                    "id": chan_id_to_enrich,
                                    "amount": amt_enrich,
                                    "alias": channel_to_enrich["alias"],
                                }
                            )
                            current_batch_accumulated_liquidity += amt_enrich
                            # used_channel_ids.add(chan_id_to_enrich) # Add to used_channel_ids only when successfully added
                            channels_actually_added_this_enrich += 1
                            probed_enrich_successfully = True
                            break
                        elif (
                            not qr_succ_enrich
                            and "unable to find a path to destination"
                            in str(qr_err_detail_enrich).lower()
                        ):
                            print_color(
                                f"  Channel {channel_to_enrich['alias']} ({chan_id_to_enrich}) permanently skipped during enrichment: no path.",
                                Colors.FAIL,
                            )
                            used_channel_ids.add(
                                chan_id_to_enrich
                            )  # Mark globally unusable
                            channel_permanently_skipped_no_path_enrich = True
                            break  # Stop probing this channel for enrichment

                    if probed_enrich_successfully:
                        used_channel_ids.add(
                            chan_id_to_enrich
                        )  # Now mark as used because it's in the enriched batch

                    enrich_loop_idx += 1
                    if (
                        probed_enrich_successfully
                        and channels_actually_added_this_enrich >= ENRICHMENT_ADD_LIMIT
                    ):
                        break
                    if (
                        channel_permanently_skipped_no_path_enrich
                        and not probed_enrich_successfully
                    ):
                        # if skipped, effectively done with this candidate for enrichment pass
                        # The outer enrich_loop_idx will increment and try next candidate.
                        pass

                channel_idx = enrich_loop_idx
                newly_added_channels_in_enrich_pass = (
                    channels_actually_added_this_enrich
                )

            # Decision Logic after payment attempt (direct or tracked) and optional enrichment
            if newly_added_channels_in_enrich_pass > 0:
                print_color(
                    f"Batch was enriched with {newly_added_channels_in_enrich_pass} new channel(s). Retrying payment with this enriched batch shortly.",
                    Colors.OKCYAN,
                )
                script_logger.info(
                    f"Batch enriched with {newly_added_channels_in_enrich_pass} channels. Retrying payment."
                )
                time.sleep(2)  # Short delay before retry
                continue  # Continue inner payment loop with enriched batch
            else:
                print_color(
                    "Batch could not be enriched further after failure / no new channels added. This payment route is exhausted.",
                    Colors.FAIL,
                )
                script_logger.warning(
                    "Batch could not be enriched. Route exhausted for this set of primary channels."
                )
                print_color(
                    "Breaking from inner payment loop to try a new primary batch if candidates remain.",
                    Colors.FAIL,
                )
                break  # Break inner loop, to outer loop to rebuild a new primary batch

        # Inner loop broken. If it was due to success, function would have returned.
        # Otherwise, outer loop will try to build a new primary batch if candidates remain.
        # If all candidates exhausted, the check at the start of the outer loop will handle it.

    # Should be unreachable if logic is correct, as inner success returns, and outer exhaustion returns.
    print_color("All payment strategies exhausted.", Colors.FAIL)
    script_logger.error(
        f"All payment strategies exhausted for invoice {invoice_str[:40]}..., hash {payment_hash}"
    )
    return False, None


def track_payment_until_settled(config, args, payment_hash, timeout_seconds, debug):
    """
    Tracks an ongoing LND payment using lncli trackpayment until it settles (SUCCEEDED or FAILED)
    or the timeout is reached.
    Returns a tuple (succeeded: bool, preimage: str or None).
    """
    print_color(
        f"\nTracking payment {payment_hash[:10]}... using lncli trackpayment for up to {timeout_seconds} seconds...",
        Colors.OKBLUE,
    )
    script_logger.info(f"Tracking payment {payment_hash} for up to {timeout_seconds}s.")

    lncli_path = config.get("lncli_path", "/usr/local/bin/lncli")
    lnd_rpcserver = config.get("lnd_rpcserver") or "localhost:10009"
    lnd_tlscertpath = config.get("lnd_tlscertpath") or os.path.expanduser(
        "~/.lnd/tls.cert"
    )
    lnd_macaroonpath = config.get("lnd_macaroonpath") or os.path.expanduser(
        "~/.lnd/data/chain/bitcoin/mainnet/admin.macaroon"
    )

    lnd_connection_params = [
        "--rpcserver=" + lnd_rpcserver,
        "--tlscertpath=" + lnd_tlscertpath,
        "--macaroonpath=" + lnd_macaroonpath,
    ]

    command_parts = (
        [lncli_path] + lnd_connection_params + ["trackpayment", "--json", payment_hash]
    )

    if debug:
        print_color(
            f"[DEBUG] lncli trackpayment command: {' '.join(command_parts)}",
            Colors.WARNING,
        )
        script_logger.debug(
            f"[DEBUG] Skipping trackpayment for {payment_hash} (debug mode)."
        )
        # Simulate quick success or failure in debug mode
        if "sim_success_hash" in payment_hash:  # Example debug condition
            print_color("[DEBUG] Simulated trackpayment success.", Colors.OKGREEN)
            return True, "debug_simulated_preimage_trackpayment"
        else:
            print_color("[DEBUG] Simulated trackpayment failure.", Colors.FAIL)
            return False, None

    start_time = time.time()
    last_status = None
    last_failure_reason = None
    payment_preimage = None

    try:
        process = subprocess.Popen(
            command_parts,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # Line-buffered output
        )

        # Read stdout line by line until process exits or timeout
        for line in iter(process.stdout.readline, ""):
            if time.time() - start_time > timeout_seconds:
                print_color(
                    f"Trackpayment timed out after {timeout_seconds} seconds for hash {payment_hash[:10]}... Attempting to terminate.",
                    Colors.WARNING,
                )
                script_logger.warning(
                    f"Trackpayment timed out for {payment_hash} after {timeout_seconds}s."
                )
                if process.poll() is None:  # Still running
                    print_color(
                        "  Attempting graceful termination (SIGINT)...", Colors.WARNING
                    )
                    process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=5)  # Wait for graceful exit
                        print_color(
                            "  Process terminated gracefully after SIGINT.",
                            Colors.OKCYAN,
                        )
                    except subprocess.TimeoutExpired:
                        print_color(
                            "  Process did not terminate via SIGINT within timeout, resorting to SIGKILL...",
                            Colors.WARNING,
                        )
                        if process.poll() is None:  # Check again before SIGKILL
                            process.kill()
                            print_color(
                                "  Process terminated via SIGKILL.", Colors.WARNING
                            )
                break  # Exit the loop after attempting termination

            try:
                if args.verbose:
                    print_color(
                        f"  Trackpayment raw line: '{line.strip()}'", Colors.OKBLUE
                    )

                payment_update = json.loads(line)

                if args.verbose:
                    print_color(
                        f"  Parsed payment_update: {json.dumps(payment_update)}",
                        Colors.OKBLUE,
                    )

                status = payment_update.get("status")
                failure_reason = payment_update.get("failure_reason")

                if (
                    args.verbose and not status and line.strip()
                ):  # Log if status key is missing but line wasn't empty
                    print_color(
                        f"  WARNING: 'status' key missing in trackpayment update: {json.dumps(payment_update)}",
                        Colors.WARNING,
                    )
                    script_logger.debug(
                        f"Trackpayment update for {payment_hash} missing 'status' key: {payment_update}"
                    )

                if status:
                    last_status = status
                    if failure_reason and failure_reason != "FAILURE_REASON_NONE":
                        last_failure_reason = failure_reason

                    if args.verbose:
                        print_color(
                            f"  Payment status update for {payment_hash[:10]}...: {status}",
                            Colors.OKCYAN,
                        )
                        if last_failure_reason:
                            print_color(
                                f"  Failure reason: {last_failure_reason}", Colors.FAIL
                            )

                    script_logger.debug(
                        f"Trackpayment update for {payment_hash}: Status={status}, FailureReason={last_failure_reason}"
                    )

                    if status == "SUCCEEDED":
                        payment_preimage = payment_update.get("preimage")
                        print_color(
                            f"Payment {payment_hash[:10]}... SUCCEEDED during tracking. Preimage: {payment_preimage[:10] if payment_preimage else 'N/A'}...",
                            Colors.OKGREEN,
                        )
                        script_logger.info(
                            f"Trackpayment: Payment {payment_hash} SUCCEEDED. Preimage: {payment_preimage}"
                        )
                        process.terminate()
                        return True, payment_preimage
                    elif status == "FAILED":
                        print_color(
                            f"Payment {payment_hash[:10]}... FAILED during tracking. Reason: {last_failure_reason}",
                            Colors.FAIL,
                        )
                        script_logger.warning(
                            f"Trackpayment: Payment {payment_hash} FAILED. Reason: {last_failure_reason}"
                        )
                        process.terminate()
                        return False, None
            except json.JSONDecodeError:
                if (
                    args.verbose and line.strip()
                ):  # Only print if verbose and line is not just whitespace
                    print_color(
                        f"  Warning: Non-JSON or malformed line from trackpayment: '{line.strip()}'",
                        Colors.WARNING,
                    )
                    script_logger.debug(
                        f"Trackpayment non-JSON line for {payment_hash}: '{line.strip()}'"
                    )
                pass
            except Exception as e:
                print_color(
                    f"Error processing trackpayment output for {payment_hash[:10]}...: {e}, line: {line.strip()}",
                    Colors.FAIL,
                )
                script_logger.exception(
                    f"Error processing trackpayment output for {payment_hash}"
                )

        # After loop, if process is still running (e.g., due to timeout but break was hit)
        if process.poll() is None:
            process.terminate()
            time.sleep(1)  # Give it a moment
            if process.poll() is None:
                process.kill()

        # Check any remaining output after termination (stderr or final stdout)
        # This is a non-blocking communicate call with a short timeout
        stdout_after, stderr_after = "", ""
        try:
            stdout_after, stderr_after = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            script_logger.warning(
                f"Timeout getting final output from trackpayment process for {payment_hash}"
            )
        except Exception as e_comm:
            script_logger.warning(
                f"Error getting final output from trackpayment process for {payment_hash}: {e_comm}"
            )

        if stderr_after.strip():
            print_color(
                f"Trackpayment for {payment_hash[:10]}... stderr: {stderr_after.strip()}",
                Colors.FAIL,
            )
            script_logger.error(
                f"Trackpayment stderr for {payment_hash}: {stderr_after.strip()}"
            )

    except FileNotFoundError:
        err_msg = f"Error: lncli not found at {lncli_path}. Check config.ini."
        print_color(err_msg, Colors.FAIL)
        script_logger.error(err_msg)
        return False, None  # Critical error, assume failure
    except Exception as e:
        err_msg = f"An unexpected error occurred during trackpayment for {payment_hash[:10]}...: {e}"
        print_color(err_msg, Colors.FAIL)
        script_logger.exception(
            f"Unexpected error during trackpayment for {payment_hash}"
        )
        return False, None  # Assume failure

    # If we reached here, payment did not SUCCEED explicitly during tracking or timed out.
    # Check the last known status if any.
    if last_status == "SUCCEEDED":  # Should have returned earlier
        script_logger.info(
            f"Trackpayment for {payment_hash} concluded with last_status SUCCEEDED (post-loop check). Preimage: {payment_preimage}"
        )
        return True, payment_preimage

    print_color(
        f"Payment tracking for {payment_hash[:10]}... finished without clear success. Last status: {last_status}, Last failure reason: {last_failure_reason if last_failure_reason else 'N/A'}",
        Colors.WARNING,
    )
    script_logger.warning(
        f"Trackpayment for {payment_hash} finished inconclusive. LastStatus: {last_status}, LastReason: {last_failure_reason}"
    )
    return False, None


def construct_lncli_command(config, args, invoice, outgoing_chan_ids_batch):
    """
    Constructs the lncli payinvoice command.
    Returns a tuple: (actual_command_list, display_command_string)
    """
    if args.debug:
        print(
            f"[DEBUG construct_lncli_command] Args received: amount={args.amount}, ppm={args.ppm}, timeout={args.payment_timeout}, max_parts={args.max_parts}, debug={args.debug}, verbose={args.verbose}"
        )

    lncli_path = config.get("lncli_path", "/usr/local/bin/lncli")

    lnd_rpcserver = config.get("lnd_rpcserver") or "localhost:10009"
    lnd_tlscertpath = config.get("lnd_tlscertpath") or os.path.expanduser(
        "~/.lnd/tls.cert"
    )
    lnd_macaroonpath = config.get("lnd_macaroonpath") or os.path.expanduser(
        "~/.lnd/data/chain/bitcoin/mainnet/admin.macaroon"
    )

    lnd_connection_params = [
        "--rpcserver=" + lnd_rpcserver,
        "--tlscertpath=" + os.path.expanduser(lnd_tlscertpath),
        "--macaroonpath=" + os.path.expanduser(lnd_macaroonpath),
    ]

    core_command_params = [
        "payinvoice",
        "--json",
        "--force",
        "--cancelable",
        f"--timeout={args.payment_timeout}",
    ]

    if args.ppm is not None:
        fee_limit_sats = math.floor(args.amount * args.ppm / 1_000_000)
        core_command_params.extend(["--fee_limit", str(fee_limit_sats)])
        if args.debug:  # Print fee calculation only in debug, not just verbose
            print(
                f"[DEBUG construct_lncli_command] Setting lncli --fee_limit to {fee_limit_sats} sats (calculated from {args.ppm} PPM for {args.amount} sats amount)."
            )
    else:
        core_command_params.extend(["--fee_limit", "0"])
        if args.debug:
            print(
                "[DEBUG construct_lncli_command] Setting lncli --fee_limit to 0 sats (as --ppm was not provided)."
            )

    if args.max_parts is not None:
        core_command_params.extend(["--max_parts", str(args.max_parts)])
        if args.debug:
            print(
                f"[DEBUG construct_lncli_command] Setting lncli --max_parts to {args.max_parts}."
            )
    # If args.max_parts is None, lncli uses its default

    outgoing_channel_params = []
    for chan_id in outgoing_chan_ids_batch:
        outgoing_channel_params.extend(["--outgoing_chan_id", str(chan_id)])

    # Actual command for execution is always complete
    actual_command_list = (
        [lncli_path]
        + lnd_connection_params
        + core_command_params
        + outgoing_channel_params
        + [invoice]
    )

    # Determine the command string for display
    if args.verbose or args.debug:  # Show full command if verbose or debug
        display_command_string = " ".join(actual_command_list)
    else:
        # Abbreviated version for display: lncli_path + core_params + outgoing_params + invoice
        abbreviated_parts = (
            [lncli_path] + core_command_params + outgoing_channel_params + [invoice]
        )
        display_command_string = " ".join(abbreviated_parts)
        # Optional: add a suffix like " [...LND params hidden]" if you want to be explicit
        # display_command_string += " [...LND connection details hidden]"

    # This debug print is for verifying the construction process itself.
    # It shows the *actual* command that will be formed.
    # The "Executing:" line printed by run_command will use the display_command_string.
    if args.debug:
        print(
            f"[DEBUG construct_lncli_command] Actual command for execution: {' '.join(actual_command_list)}"
        )
        if (
            not args.verbose
        ):  # If not verbose, the display string is different, so show it for clarity
            print(
                f"[DEBUG construct_lncli_command] Display command for 'Executing:' line: {display_command_string}"
            )

    return actual_command_list, display_command_string


def decode_payreq(config, args, invoice_str):
    """Decodes a BOLT11 payment request using lncli decodepayreq."""
    print_bold_step(
        f"Decoding payment request: {invoice_str[:40]}...", color_code=Colors.OKCYAN
    )
    lncli_path = config.get("lncli_path", "/usr/local/bin/lncli")
    lnd_connection_params = [
        "--rpcserver=" + (config.get("lnd_rpcserver") or "localhost:10009"),
        "--tlscertpath="
        + os.path.expanduser(config.get("lnd_tlscertpath") or "~/.lnd/tls.cert"),
        "--macaroonpath="
        + os.path.expanduser(
            config.get("lnd_macaroonpath")
            or "~/.lnd/data/chain/bitcoin/mainnet/admin.macaroon"
        ),
    ]
    command_parts = [lncli_path] + lnd_connection_params + ["decodepayreq", invoice_str]

    # Use a short timeout as this should be a quick operation
    success, output, error_stderr = run_command(
        command_parts,
        timeout=30,  # 30-second timeout for decodepayreq
        debug=args.debug,
        expect_json=True,
        dry_run_output="lncli decodepayreq command",
        verbose=args.verbose,  # Add this line
    )

    if args.debug and success:  # Simulate successful decode in debug
        # Try to match the amount from the invoice if possible, or use args.amount
        # For simplicity in mock, using args.amount
        mock_decoded_data = {
            "destination": "debug_destination_pubkey_from_decode",
            "payment_hash": "debug_payment_hash_from_decode_"
            + str(args.amount),  # Make it unique for debug
            "num_satoshis": str(args.amount),  # Ensure it's a string like lncli output
            "description": "debug_description",
            "cltv_expiry": "40",  # Example CLTV expiry
            "expiry": "3600",
            "payment_addr": "debug_payment_addr_from_decode",
        }
        print_color(
            f"[DEBUG] Simulated decodepayreq success: {mock_decoded_data}",
            Colors.OKGREEN,
        )
        return mock_decoded_data

    if success and isinstance(output, dict):
        # Validate essential fields
        if (
            not output.get("destination")
            or not output.get("num_satoshis")
            or not output.get("payment_hash")
        ):
            err_msg = f"Decoded invoice missing 'destination', 'num_satoshis', or 'payment_hash'. Response: {json.dumps(output)}"
            print_color(err_msg, Colors.FAIL)
            script_logger.error(f"decode_payreq: {err_msg}")
            return None

        # Verify amount if possible (num_satoshis can be 0 for "any amount" invoices, but Boltz will specify)
        decoded_amount = int(output.get("num_satoshis", 0))
        if decoded_amount != args.amount:
            warn_msg = f"Warning: Decoded invoice amount ({decoded_amount} sats) differs from requested swap amount ({args.amount} sats)."
            print_color(warn_msg, Colors.WARNING)
            script_logger.warning(f"decode_payreq: {warn_msg}")
            # Decide if this is a critical error or just a warning. For Boltz, it should match.
            # If this check is too strict for some invoices, it can be relaxed or made conditional.

        print_color("Payment request decoded successfully.", Colors.OKGREEN)
        script_logger.info(
            f"Payment request {invoice_str[:40]}... decoded. Hash: {output.get('payment_hash')}, Amount: {output.get('num_satoshis')}"
        )
        if args.verbose:
            print_color(f"  Destination: {output.get('destination')}", Colors.OKCYAN)
            print_color(f"  Payment Hash: {output.get('payment_hash')}", Colors.OKCYAN)
            print_color(f"  Amount: {output.get('num_satoshis')} sats", Colors.OKCYAN)
            print_color(
                f"  CLTV Expiry (delta from current block): {output.get('cltv_expiry')}",
                Colors.OKCYAN,
            )
        return output
    else:
        err_msg = "Failed to decode payment request."
        print_color(err_msg, Colors.FAIL)
        script_logger.error(
            f"decode_payreq: {err_msg} Stderr: {error_stderr}, Output: {json.dumps(output) if isinstance(output,dict) else output}"
        )
        if error_stderr:
            print_color(f"  Stderr: {error_stderr.strip()}", Colors.FAIL)
        if (
            isinstance(output, str) and output
        ):  # Output might be string if JSON parsing failed in run_command
            print_color(f"  Stdout: {output.strip()}", Colors.FAIL)
        elif isinstance(
            output, dict
        ):  # run_command succeeded but content was not as expected
            print_color(f"  Output: {json.dumps(output)}", Colors.FAIL)
        return None


def send_prepay_probe(config, args, dest_pubkey, amt, outgoing_chan_ids):
    """
    Sends a prepay probe with a random payment hash, simulating the real payment as closely as possible.
    Returns (True, None) if probe is successful (INCORRECT_PAYMENT_DETAILS or INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS), else (False, error_message).
    """
    lncli_path = config.get("lncli_path", "/usr/local/bin/lncli")
    lnd_rpcserver = config.get("lnd_rpcserver") or "localhost:10009"
    lnd_tlscertpath = config.get("lnd_tlscertpath") or os.path.expanduser(
        "~/.lnd/tls.cert"
    )
    lnd_macaroonpath = config.get("lnd_macaroonpath") or os.path.expanduser(
        "~/.lnd/data/chain/bitcoin/mainnet/admin.macaroon"
    )

    # Generate random 32-byte payment hash
    fake_payment_hash = binascii.hexlify(os.urandom(32)).decode()

    # Calculate timeout values using the same logic as the real payment
    lncli_timeout_val = 300  # Default
    try:
        if args.payment_timeout.lower().endswith("s"):
            lncli_timeout_val = int(args.payment_timeout[:-1])
        elif args.payment_timeout.lower().endswith("m"):
            lncli_timeout_val = int(args.payment_timeout[:-1]) * 60
        elif args.payment_timeout.lower().endswith("h"):
            lncli_timeout_val = int(args.payment_timeout[:-1]) * 3600
        else:
            lncli_timeout_val = int(args.payment_timeout)
    except ValueError:
        print_color(
            f"Invalid payment timeout format: {args.payment_timeout}. Using default {lncli_timeout_val}s for probe.",
            Colors.WARNING,
        )
        script_logger.warning(
            f"Invalid payment timeout format: {args.payment_timeout}. Using default {lncli_timeout_val}s for probe."
        )

    # Convert to string format for lncli (e.g., 300 -> "5m", 60 -> "1m", 30 -> "30s")
    if lncli_timeout_val >= 3600:
        lncli_timeout_str = f"{lncli_timeout_val // 3600}h"
    elif lncli_timeout_val >= 60:
        lncli_timeout_str = f"{lncli_timeout_val // 60}m"
    else:
        lncli_timeout_str = f"{lncli_timeout_val}s"

    command = [
        lncli_path,
        "--rpcserver=" + lnd_rpcserver,
        "--tlscertpath=" + lnd_tlscertpath,
        "--macaroonpath=" + lnd_macaroonpath,
        "sendpayment",
        "--dest",
        dest_pubkey,
        "--amt",
        str(amt),
        "--payment_hash",
        fake_payment_hash,
        "--timeout",
        lncli_timeout_str,
        "--json",
    ]
    for chan_id in outgoing_chan_ids:
        command.extend(["--outgoing_chan_id", str(chan_id)])
    if args.ppm is not None:
        fee_limit_sats = math.floor(args.amount * args.ppm / 1_000_000)
        command.extend(["--fee_limit", str(fee_limit_sats)])
    else:
        command.extend(["--fee_limit", "0"])
    if args.max_parts is not None:
        command.extend(["--max_parts", str(args.max_parts)])
    if args.lncli_cltv_limit > 0:
        command.extend(["--cltv_limit", str(args.lncli_cltv_limit)])

    print_color(
        f"Probing route with fake payment hash (prepay probe) for {amt} sats via channels: {', '.join(str(c) for c in outgoing_chan_ids)} (timeout: {lncli_timeout_str})",
        Colors.WARNING,
    )
    script_logger.info(
        f"Prepay probe: {amt} sats, outgoing_chan_ids: {outgoing_chan_ids}, timeout: {lncli_timeout_str}"
    )

    # Use the same subprocess timeout calculation as the real payment
    script_subprocess_timeout = lncli_timeout_val + SUBPROCESS_TIMEOUT_BUFFER_SECONDS

    success, output, error = run_command(
        command,
        timeout=script_subprocess_timeout,
        debug=args.debug,
        expect_json=True,
        dry_run_output="lncli prepay probe",
        verbose=False,  # Always suppress verbose output for prepay probes
        success_codes=[
            0,
            1,
        ],  # Accept both success (0) and "failure" (1) exit codes for probes
    )

    # Acceptable probe success signals
    def is_probe_success(output):
        # Check failure_reason field first (most reliable)
        failure_reason = output.get("failure_reason", "")
        if any(
            s in failure_reason
            for s in [
                "FAILURE_REASON_INCORRECT_PAYMENT_DETAILS",
                "INCORRECT_PAYMENT_DETAILS",
                "INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS",
            ]
        ):
            return True

        # Check payment_error field
        payment_error = output.get("payment_error", "")
        if any(
            s in payment_error
            for s in [
                "INCORRECT_PAYMENT_DETAILS",
                "INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS",
                "FAILURE_REASON_INCORRECT_PAYMENT_DETAILS",
            ]
        ):
            return True

        # Also check htlcs for failure code
        for htlc in output.get("htlcs", []):
            failure = htlc.get("failure", {})
            code = failure.get("code", "")
            if code in [
                "INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS",
                "INCORRECT_PAYMENT_DETAILS",
            ]:
                return True
        return False

    # Always check the output for probe success signals, regardless of exit code
    if isinstance(output, dict) and is_probe_success(output):
        print_color(
            "Prepay probe reached destination (INCORRECT_PAYMENT_DETAILS or similar). Route is viable.",
            Colors.OKGREEN,
        )
        script_logger.info(
            "Prepay probe: INCORRECT_PAYMENT_DETAILS or similar (route viable)"
        )
        return True, None

    # If we get here, the probe failed for a reason other than incorrect payment details
    error_msg = f"Probe failed: {output.get('failure_reason', output.get('payment_error', error))}"
    print_color(f"Prepay probe failed: {error_msg}", Colors.WARNING)
    script_logger.warning(f"Prepay probe failed: {error_msg}")
    return False, error_msg


def get_channel_info_by_id(config, args, channel_id):
    """
    Gets channel information by channel ID using LNDg API.
    Returns a dict with channel info or None if not found.
    """
    # Use LNDg API to get channel info (more efficient than lncli listchannels)
    api_url = f"{args.lndg_api}/api/channels?limit=5000&is_open=true&is_active=true"

    try:
        response = requests.get(
            api_url, auth=(config["lndg_username"], config["lndg_password"]), timeout=30
        )
        response.raise_for_status()
        data = response.json()

        if "results" in data:
            for channel in data["results"]:
                if channel.get("chan_id") == channel_id:
                    return {
                        "id": channel.get("chan_id"),
                        "balance": channel.get("local_balance", 0),
                        "alias": channel.get("alias", "Unknown"),
                        "fee_rate": channel.get("local_fee_rate", 0),
                        "remote_pubkey": channel.get("remote_pubkey"),
                        "capacity": channel.get("capacity"),
                        "active": channel.get("is_active", False),
                    }

        # If not found in LNDg, return minimal info with just the channel ID
        print_color(
            f"Channel {channel_id} not found in LNDg API. Using minimal channel info.",
            Colors.WARNING,
        )

        return {
            "id": channel_id,
            "balance": 0,  # We'll check this with queryroutes
            "alias": f"Channel_{channel_id}",
            "fee_rate": 0,
            "remote_pubkey": None,
            "capacity": 0,
            "active": True,  # Assume active since we're trying to use it
        }

    except requests.exceptions.RequestException as e:
        print_color(f"Failed to get channel info from LNDg API: {e}", Colors.FAIL)
        print_color(f"Using minimal channel info for {channel_id}", Colors.WARNING)

        return {
            "id": channel_id,
            "balance": 0,  # We'll check this with queryroutes
            "alias": f"Channel_{channel_id}",
            "fee_rate": 0,
            "remote_pubkey": None,
            "capacity": 0,
            "active": True,  # Assume active since we're trying to use it
        }


def pay_with_single_channel(config, args, invoice_str, channel_id, debug):
    """
    Pays the Lightning invoice using a single specified channel.
    Returns True on success, False on failure.
    """
    print_color(
        f"\nStep 4: Attempting to pay Lightning invoice via single channel {channel_id}",
        Colors.HEADER,
    )

    # Get channel info
    channel_info = get_channel_info_by_id(config, args, channel_id)
    if not channel_info:
        print_color(f"Channel {channel_id} not found or not accessible.", Colors.FAIL)
        return False, None

    if not channel_info.get("active", False):
        print_color(
            f"Channel {channel_id} ({channel_info['alias']}) is not active.",
            Colors.FAIL,
        )
        return False, None

    local_balance = channel_info.get("balance", 0)
    if local_balance > 0:  # Only check balance if we have it
        if local_balance < args.amount:
            print_color(
                f"Channel {channel_id} ({channel_info['alias']}) has insufficient balance: "
                f"{local_balance} sats < {args.amount} sats required.",
                Colors.FAIL,
            )
            return False, None
        print_color(
            f"Using channel {channel_id} ({channel_info['alias']}) with local balance: {local_balance} sats",
            Colors.OKGREEN,
        )
    else:
        print_color(
            f"Using channel {channel_id} ({channel_info['alias']}) - balance will be checked during route query",
            Colors.OKGREEN,
        )

    # Decode invoice
    decoded_invoice = decode_payreq(config, args, invoice_str)
    if not decoded_invoice:
        print_color("Failed to decode Boltz invoice, cannot proceed.", Colors.FAIL)
        return False, None

    invoice_destination_pubkey = decoded_invoice.get("destination")
    payment_hash = decoded_invoice.get("payment_hash")

    if not invoice_destination_pubkey or not payment_hash:
        print_color(
            "Decoded invoice missing destination pubkey or payment hash.", Colors.FAIL
        )
        return False, None

    # Test route with queryroutes
    lncli_path = config.get("lncli_path", "/usr/local/bin/lncli")
    lnd_connection_params = [
        "--rpcserver=" + (config.get("lnd_rpcserver") or "localhost:10009"),
        "--tlscertpath="
        + os.path.expanduser(config.get("lnd_tlscertpath") or "~/.lnd/tls.cert"),
        "--macaroonpath="
        + os.path.expanduser(
            config.get("lnd_macaroonpath")
            or "~/.lnd/data/chain/bitcoin/mainnet/admin.macaroon"
        ),
    ]

    query_command = [
        lncli_path,
        *lnd_connection_params,
        "queryroutes",
        "--dest",
        invoice_destination_pubkey,
        "--amt",
        str(args.amount),
        "--outgoing_chan_id",
        str(channel_id),
    ]

    if args.ppm is not None:
        fee_limit_sats = math.floor(args.amount * args.ppm / 1_000_000)
        query_command.extend(["--fee_limit", str(fee_limit_sats)])
    else:
        query_command.extend(["--fee_limit", "0"])

    if args.lncli_cltv_limit > 0:
        query_command.extend(["--cltv_limit", str(args.lncli_cltv_limit)])

    query_timeout_val = 30
    try:
        if args.queryroutes_timeout.lower().endswith("s"):
            query_timeout_val = int(args.queryroutes_timeout[:-1])
        elif args.queryroutes_timeout.lower().endswith("m"):
            query_timeout_val = int(args.queryroutes_timeout[:-1]) * 60
        query_timeout_val += 10
    except ValueError:
        pass

    print_color(
        f"Testing route via channel {channel_id} ({channel_info['alias']})...",
        Colors.OKCYAN,
    )
    qr_success, qr_output, qr_error = run_command(
        query_command,
        timeout=query_timeout_val,
        debug=args.debug,
        expect_json=True,
        dry_run_output=f"lncli queryroutes via {channel_info['alias']}({channel_id})",
        verbose=args.verbose,
    )

    if not qr_success or not isinstance(qr_output, dict) or not qr_output.get("routes"):
        print_color(
            f"No route found via channel {channel_id} ({channel_info['alias']}): {qr_error}",
            Colors.FAIL,
        )
        return False, None

    print_color(
        f"Route found via channel {channel_id} ({channel_info['alias']}). Proceeding with prepay probe...",
        Colors.OKGREEN,
    )

    # Prepay probe
    probe_success, probe_error = send_prepay_probe(
        config,
        args,
        invoice_destination_pubkey,
        args.amount,
        [channel_id],
    )

    if not probe_success:
        print_color(
            f"Prepay probe failed for channel {channel_id} ({channel_info['alias']}): {probe_error}",
            Colors.FAIL,
        )
        return False, None

    print_color(
        f"Prepay probe successful for channel {channel_id} ({channel_info['alias']}). Proceeding with payment...",
        Colors.OKGREEN,
    )

    # Actual payment
    actual_command_list, display_command_str = construct_lncli_command(
        config, args, invoice_str, [channel_id]
    )

    lncli_timeout_val = 300  # Default
    try:
        if args.payment_timeout.lower().endswith("s"):
            lncli_timeout_val = int(args.payment_timeout[:-1])
        elif args.payment_timeout.lower().endswith("m"):
            lncli_timeout_val = int(args.payment_timeout[:-1]) * 60
        elif args.payment_timeout.lower().endswith("h"):
            lncli_timeout_val = int(args.payment_timeout[:-1]) * 3600
        else:
            lncli_timeout_val = int(args.payment_timeout)
    except ValueError:
        print_color(
            f"Invalid payment timeout format: {args.payment_timeout}. Using default {lncli_timeout_val}s.",
            Colors.WARNING,
        )

    script_subprocess_timeout = (
        lncli_timeout_val + 30
    )  # SUBPROCESS_TIMEOUT_BUFFER_SECONDS

    print_color(
        f"Attempting payment via channel {channel_id} ({channel_info['alias']})...",
        Colors.OKBLUE,
    )

    payinvoice_success, payinvoice_output_json, payinvoice_error_stderr = run_command(
        actual_command_list,
        timeout=script_subprocess_timeout,
        debug=args.debug,
        expect_json=True,
        dry_run_output="lncli payinvoice",
        success_codes=[0],
        display_str_override=display_command_str,
        attempt_graceful_terminate_on_timeout=True,
        verbose=args.verbose,
    )

    if payinvoice_success and isinstance(payinvoice_output_json, dict):
        payment_error = payinvoice_output_json.get("payment_error", "")
        payment_preimage = payinvoice_output_json.get("payment_preimage", "")

        if not payment_error and payment_preimage:
            print_color(
                f"Payment SUCCESSFUL via channel {channel_id} ({channel_info['alias']})!",
                Colors.OKGREEN,
                bold=True,
            )
            script_logger.info(
                f"Payment SUCCESS via channel {channel_id} ({channel_info['alias']}). Preimage: {payment_preimage}"
            )
            return True, payment_preimage
        else:
            print_color(
                f"Payment FAILED via channel {channel_id} ({channel_info['alias']}): {payment_error}",
                Colors.FAIL,
            )
            script_logger.warning(
                f"Payment FAILED via channel {channel_id} ({channel_info['alias']}). Error: {payment_error}"
            )
            return False, None
    else:
        print_color(
            f"Payment command failed via channel {channel_id} ({channel_info['alias']}): {payinvoice_error_stderr}",
            Colors.FAIL,
        )
        script_logger.error(
            f"Payment command failed via channel {channel_id} ({channel_info['alias']}). Error: {payinvoice_error_stderr}"
        )
        return False, None


def main():
    """Main execution flow."""
    setup_logging()  # Initialize logging system
    args, parser = parse_arguments()
    config = load_config(args.config)
    script_logger.info(f"Starting boltz_swap-out.py with arguments: {vars(args)}")

    # Override args with config values if args are at their defaults
    default_lndg_api = parser.get_default("lndg_api")
    if args.lndg_api == default_lndg_api and "lndg_api_url" in config:
        print(
            f"[INFO] Overriding default LNDg API '{args.lndg_api}' with config value '{config['lndg_api_url']}'."
        )
        args.lndg_api = config["lndg_api_url"]
        script_logger.info(f"Overridden LNDg API with config value: {args.lndg_api}")

    # Then print effective settings
    print_color(
        "\n--- Effective Settings After Config Override ---", Colors.HEADER, bold=True
    )
    print_color(f"LNDg API: {args.lndg_api}", Colors.OKCYAN)
    print_color(f"Swap Amount (LN): {args.amount} sats", Colors.OKCYAN)

    # Adapt output based on mode
    if args.outgoing_channel_id:
        print_color(
            f"Mode: Single Channel (ID: {args.outgoing_channel_id})", Colors.OKCYAN
        )
        print_color(
            "Note: LNDg API and batching process will be skipped", Colors.OKCYAN
        )
    else:
        print_color(
            f"Min Local Balance (candidates): {args.capacity} sats", Colors.OKCYAN
        )
        print_color(
            f"Max Local Fee (candidates): {args.local_fee_limit} ppm", Colors.OKCYAN
        )

    print_color("\n--- Initial Configuration & Paths ---", Colors.HEADER, bold=True)
    if args.debug:
        print_color(
            "DEBUG MODE ENABLED: No actual transactions.", Colors.WARNING, bold=True
        )

    print_color(f"Paths (from config.ini):", Colors.OKCYAN)
    print_color(f"  lncli: {config['lncli_path']}", Colors.OKCYAN)
    print_color(f"  pscli: {config['pscli_path']}", Colors.OKCYAN)
    print_color(f"  boltzcli: {config['boltzcli_path']}", Colors.OKCYAN)
    print_color(f"  boltzd TLS Cert: {config['boltzd_tlscert_path']}", Colors.OKCYAN)
    print_color(
        f"  boltzd Admin Macaroon: {config['boltzd_admin_macaroon_path']}",
        Colors.OKCYAN,
    )

    if args.description:
        print_color(f"Swap Description: {args.description}", Colors.OKCYAN)

    payment_preimage_final = None  # To store preimage for logging

    try:  # Start of try block for KeyboardInterrupt
        lbtc_address = None
        if args.custom_destination_address:
            lbtc_address = args.custom_destination_address
            print_color(
                f"\nUsing custom L-BTC destination address: {lbtc_address}",
                Colors.WARNING,
            )
            script_logger.info(
                f"Using custom L-BTC destination address: {lbtc_address}"
            )

            if not args.force:
                print_color(
                    "\n--- PLEASE CONFIRM SWAP DETAILS ---", Colors.WARNING, bold=True
                )
                print_color(f"  Swap Amount: {args.amount} sats", Colors.WARNING)
                print_color(
                    f"  Destination L-BTC Address: ", Colors.WARNING, bold=False
                )  # Keep it on one line for address
                print_color(
                    f"    {lbtc_address}", Colors.FAIL, bold=True
                )  # Highlight address in FAIL color
                print_color(
                    f"  Payment Timeout: {args.payment_timeout}", Colors.WARNING
                )
                if args.ppm is not None:
                    calculated_fee_sats = math.floor(args.amount * args.ppm / 1_000_000)
                    print_color(
                        f"  LN Fee Limit (PPM): {args.ppm} (approx. {calculated_fee_sats} sats)",
                        Colors.WARNING,
                    )
                else:
                    print_color(
                        f"  LN Fee Limit (PPM): Not set (uses 0 sats for lncli --fee_limit)",
                        Colors.WARNING,
                    )

                print_color(
                    "\nWARNING: ENSURE THE L-BTC ADDRESS IS CORRECT!",
                    Colors.FAIL,
                    bold=True,
                )
                print_color(
                    "If the address is incorrect, your funds may be IRRECOVERABLY LOST.",
                    Colors.FAIL,
                )
                print_color("Double-check the address carefully.", Colors.FAIL)

                confirm = (
                    input(
                        Colors.WARNING
                        + Colors.BOLD
                        + "Proceed with this address? (yes/no): "
                        + Colors.ENDC
                    )
                    .strip()
                    .lower()
                )
                if confirm != "yes":
                    print_color(
                        "\nSwap aborted by user. No action taken.",
                        Colors.FAIL,
                        bold=True,
                    )
                    script_logger.info(
                        "Swap aborted by user confirmation for custom address."
                    )
                    sys.exit(1)
                print_color(
                    "Confirmation received. Proceeding with custom address.",
                    Colors.OKGREEN,
                )
                script_logger.info("User confirmed custom address. Proceeding.")
            else:
                print_color(
                    "Confirmation for custom address skipped due to --force flag.",
                    Colors.OKBLUE,
                )
                script_logger.info(
                    "Custom address confirmation skipped due to --force."
                )
        else:
            lbtc_address = get_lbtc_address(
                config["pscli_path"], args.debug, args
            )  # Add args

        if not lbtc_address:
            err_msg = "\nExiting: L-BTC address not available or confirmed."
            print_color(err_msg, Colors.FAIL, bold=True)
            script_logger.error(err_msg.strip())
            sys.exit(1)

        # Create Boltz swap first (required for both modes)
        swap_id, lightning_invoice, boltz_response_dict = create_boltz_swap(
            config["boltzcli_path"],
            config["boltzd_tlscert_path"],
            config["boltzd_admin_macaroon_path"],
            args.amount,
            lbtc_address,
            args.description,
            args.debug,
            args,
        )
        if not lightning_invoice:
            err_msg = "\nExiting: Failed to create Boltz swap or get invoice."
            print_color(err_msg, Colors.FAIL, bold=True)
            script_logger.error(
                f"Main: {err_msg.strip()} Amount: {args.amount}, L-BTC Addr: {lbtc_address}"
            )
            sys.exit(1)

        # Check if using single channel mode
        if args.outgoing_channel_id:
            if not args.outgoing_channel_id.strip():
                print_color(
                    "Error: --outgoing-channel-id specified but no channel ID provided.",
                    Colors.FAIL,
                )
                print_color(
                    "Please provide a channel ID, e.g., --outgoing-channel-id 1234567890123456789",
                    Colors.FAIL,
                )
                sys.exit(1)

            print_color(
                f"Single channel mode: Using channel ID {args.outgoing_channel_id}",
                Colors.OKBLUE,
            )

            payment_successful, payment_preimage_final = pay_with_single_channel(
                config,
                args,
                lightning_invoice,
                args.outgoing_channel_id,
                args.debug,
            )
        else:
            # Original batch processing logic
            candidate_channels_info = get_swap_candidate_channels(
                args.lndg_api,
                config["lndg_username"],
                config["lndg_password"],
                args.capacity,
                args.local_fee_limit,
                args.debug,
            )
            if not candidate_channels_info:
                err_msg = "\nExiting: No suitable swap candidate channels found."
                print_color(err_msg, Colors.FAIL, bold=True)
                script_logger.error(err_msg.strip())
                sys.exit(1)
            print_color(
                f"Total candidate channels for payment: {len(candidate_channels_info)}",
                Colors.OKBLUE,
            )

            payment_successful, payment_preimage_final = pay_lightning_invoice(
                config,
                args,
                lightning_invoice,
                candidate_channels_info,
                args.debug,
            )

        print_color("\n--- Swap Summary ---", Colors.HEADER, bold=True)
        print_color(f"L-BTC Destination Address: {lbtc_address}", Colors.OKCYAN)
        print_color(f"Boltz Swap ID: {swap_id if swap_id else 'N/A'}", Colors.OKCYAN)

        # Add mode-specific summary information
        if args.outgoing_channel_id:
            print_color(
                f"Payment Mode: Single Channel ({args.outgoing_channel_id})",
                Colors.OKCYAN,
            )
        else:
            print_color(f"Payment Mode: Multi-Channel Batch", Colors.OKCYAN)

        log_details = {
            "SwapID": swap_id if swap_id else "N/A",
            "AmountSats": args.amount,
            "LBTCDestAddress": lbtc_address,
            "PaymentPreimage": (
                payment_preimage_final if payment_preimage_final else "N/A"
            ),
        }

        if boltz_response_dict and isinstance(boltz_response_dict, dict):
            b_expected_amount = boltz_response_dict.get("expectedAmount")
            b_invoice_amount = boltz_response_dict.get(
                "invoiceAmount"
            )  # May not be present directly
            b_lockup_addr = boltz_response_dict.get("lockupAddress")

            log_details["BoltzExpectedLBTCSent"] = (
                b_expected_amount if b_expected_amount is not None else "N/A"
            )
            log_details["BoltzLockupAddress"] = (
                b_lockup_addr if b_lockup_addr else "N/A"
            )
            log_details["BoltzInvoiceAmountFromResp"] = (
                b_invoice_amount if b_invoice_amount is not None else "N/A"
            )

            if b_invoice_amount:
                print_color(
                    f"Boltz Expected LN Payment Amount (from response, if available): {b_invoice_amount} sats",
                    Colors.OKCYAN,
                )
            if b_expected_amount is not None:
                print_color(
                    f"Boltz Est. L-BTC Sent (onchain): {b_expected_amount} sats",
                    Colors.OKCYAN,
                )
            if b_lockup_addr:
                print_color(
                    f"Boltz L-BTC Lockup Address: {b_lockup_addr}", Colors.OKCYAN
                )

        if payment_successful:
            success_msg = "Swap Initiated & LN Invoice Paid!"
            print_color(success_msg, Colors.OKGREEN, bold=True)
            print_color("Monitor your Liquid wallet for L-BTC.", Colors.OKGREEN)
            log_message = f"Boltz Swap SUCCESS: {json.dumps(log_details)}"
            script_logger.info(log_message)
            if swap_id and swap_id != "unknown_swap_id":
                print_color(
                    f"Check status: {config['boltzcli_path']} --tlscert {config['boltzd_tlscert_path']} --macaroon {config['boltzd_admin_macaroon_path']} swapinfo {swap_id}",
                    Colors.OKCYAN,
                )
        else:
            failure_msg = "Swap Failed: LN invoice not paid."
            print_color(failure_msg, Colors.FAIL, bold=True)
            print_color(
                "No funds should have left LN wallet if all attempts failed.",
                Colors.WARNING,
            )
            log_message = f"Boltz Swap FAILED: {json.dumps(log_details)}"
            script_logger.error(log_message)
            if swap_id and swap_id != "unknown_swap_id":
                print_color(
                    f"Check status: {config['boltzcli_path']} --tlscert {config['boltzd_tlscert_path']} --macaroon {config['boltzd_admin_macaroon_path']} swapinfo {swap_id}",
                    Colors.WARNING,
                )
        sys.exit(0 if payment_successful else 1)

    except KeyboardInterrupt:
        print_color(
            "\n\nScript aborted by user (CTRL-C). Exiting.", Colors.WARNING, bold=True
        )
        script_logger.warning("Script aborted by user (KeyboardInterrupt).")
        current_swap_id = locals().get("swap_id")
        if current_swap_id and current_swap_id != "unknown_swap_id":
            print_color(
                f"If a Boltz swap was initiated, its ID might be: {current_swap_id}",
                Colors.WARNING,
            )
            print_color(
                f"You may need to check its status manually using boltzcli swapinfo {current_swap_id}",
                Colors.WARNING,
            )
            script_logger.warning(
                f"Swap ID at interruption (if any): {current_swap_id}"
            )
        sys.exit(130)
    except Exception as e:
        print_color(
            f"\nAn unexpected error occurred in main: {e}", Colors.FAIL, bold=True
        )
        import traceback

        tb_str = traceback.format_exc()
        print_color(tb_str, Colors.FAIL)
        script_logger.critical(f"Unhandled critical error in main: {e}\n{tb_str}")
        sys.exit(2)


if __name__ == "__main__":
    main()
