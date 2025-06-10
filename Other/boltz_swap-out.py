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
        help="Enable verbose output, showing full details of executed commands like LND connection parameters.",
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
    print(f"Swap Amount (LN): {args.amount} sats")
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

    return args, parser


def load_config(config_file_path):
    """Loads LNDg, Paths, and Boltz RPC configuration from ../config.ini."""
    config = configparser.ConfigParser()

    if not os.path.exists(config_file_path):
        print_color(
            f"Error: Configuration file not found at {config_file_path}", Colors.FAIL
        )
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
        print_color(f"Error: Missing key {e} in config.ini.", Colors.FAIL)
        print_color(
            f"Please ensure your `config.ini` contains all required LNDg credentials, paths, and boltzd RPC details.",
            Colors.FAIL,
        )
        sys.exit(1)
    except configparser.NoSectionError as e:
        print_color(f"Error: Missing section {e.section} in config.ini.", Colors.FAIL)
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

    if debug:
        # The [DEBUG] {dry_run_output}: line should show the *actual* command
        print_color(f"[DEBUG] {dry_run_output}: {actual_command_str}", Colors.WARNING)
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
            if expect_json:
                try:
                    return True, json.loads(stdout), ""
                except json.JSONDecodeError as e:
                    return (
                        False,
                        stdout,
                        f"Failed to decode JSON from command output: {e}\nOutput: {stdout}",
                    )
            return True, stdout.strip(), ""
        else:
            error_msg = f"Command failed with exit code {return_code}.\nStderr: {stderr.strip()}\nStdout: {stdout.strip()}"
            print_color(error_msg, Colors.FAIL)
            return False, stdout.strip(), stderr.strip()

    except FileNotFoundError:
        error_msg = f"Error: Command not found: {command_parts[0]}. Please check the path in your config.ini."
        print_color(error_msg, Colors.FAIL)
        return False, "", error_msg
    except subprocess.TimeoutExpired:
        error_msg = f"Command timed out after {timeout} seconds: {actual_command_str}"
        print_color(error_msg, Colors.FAIL)
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
                error_msg += f"\nProcess terminated. Stdout after: {stdout_after_kill.strip()}. Stderr after: {stderr_after_kill.strip()}"
            except subprocess.TimeoutExpired:
                error_msg += "\nProcess terminated. Failed to get additional output after termination."
            except Exception as e:
                error_msg += (
                    f"\nProcess terminated. Error getting additional output: {e}"
                )

        return False, "", error_msg
    except Exception as e:
        error_msg = f"An unexpected error occurred while running command: {actual_command_str}\nError: {e}"
        print_color(error_msg, Colors.FAIL)
        return False, "", error_msg


def get_lbtc_address(pscli_path, debug):
    """Fetches a new L-BTC address using pscli."""
    print_color("\nStep 1: Fetching new L-BTC address...", Colors.HEADER)
    command = [pscli_path, "lbtc-getaddress"]
    success, output, error = run_command(
        command, debug=debug, expect_json=True, dry_run_output="L-BTC address command"
    )

    if success and isinstance(output, dict) and "address" in output:
        address = output["address"]
        print_color(f"Successfully fetched L-BTC address: {address}", Colors.OKGREEN)
        return address
    else:
        print_color("Failed to get L-BTC address.", Colors.FAIL)
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
            else:
                print_color(
                    f"Found {len(candidate_channels_info)} candidate channels (sorted by local balance).",
                    Colors.OKGREEN,
                )
        else:
            print_color("LNDg API response missing 'results'.", Colors.FAIL)
            print_color(f"Response: {data}", Colors.FAIL)
    except requests.exceptions.Timeout:
        print_color(f"Timeout connecting to LNDg API at {api_url}", Colors.FAIL)
    except requests.exceptions.HTTPError as e:
        print_color(f"HTTP error connecting to LNDg API: {e}", Colors.FAIL)
        if e.response is not None:
            print_color(f"Response content: {e.response.text}", Colors.FAIL)
    except requests.exceptions.RequestException as e:
        print_color(f"Error connecting to LNDg API: {e}", Colors.FAIL)
    except json.JSONDecodeError:
        print_color(
            f"Error decoding JSON from LNDg API response: {response.text if 'response' in locals() else 'N/A'}",
            Colors.FAIL,
        )
    except Exception as e:
        print_color(
            f"An unexpected error occurred while fetching channels: {e}", Colors.FAIL
        )
    return candidate_channels_info


def create_boltz_swap(
    boltzcli_path,
    boltzd_tlscert_path,
    boltzd_admin_macaroon_path,
    swap_amount_sats,
    lbtc_address,
    description,
    debug,
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
    )

    if success and isinstance(output, dict):
        swap_id = output.get("id")
        invoice = output.get("invoice")
        lockup_address = output.get("lockupAddress")
        expected_onchain_amount = output.get("expectedAmount")
        timeout_block = output.get("timeoutBlockHeight")

        if not invoice:
            print_color(
                "Boltz response missing 'invoice'. Cannot proceed.", Colors.FAIL
            )
            print_color(f"Full response: {json.dumps(output, indent=2)}", Colors.FAIL)
            return None, None, output
        if not swap_id:
            print_color(
                "Warning: Boltz response missing 'id'. Using 'unknown_swap_id'.",
                Colors.WARNING,
            )
            swap_id = "unknown_swap_id"

        print_color(f"Boltz reverse swap created successfully!", Colors.OKGREEN)
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
        print_color("Failed to create Boltz reverse swap.", Colors.FAIL)
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
MAX_IN_TRANSITION_RETRIES = 4
# Initial delay, will be doubled for same batch. If you send large payments, you may want to increase this.
# Be careful with this, as it will increase the total time to pay the invoice.
IN_TRANSITION_RETRY_INITIAL_DELAY_SECONDS = 30
SUBPROCESS_TIMEOUT_BUFFER_SECONDS = (
    30  # Buffer for subprocess.communicate over lncli's own timeout
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

    if not candidate_channels_info:
        print_color("No candidate channels for payment. Aborting.", Colors.FAIL)
        return False

    swap_amount_sats = args.amount
    # Target liquidity for each batch: 1.5x the swap amount
    target_batch_liquidity = swap_amount_sats * 1.5

    # --- New Batch Formation Logic ---
    all_formed_batches_info = (
        []
    )  # Stores lists of channel_info dicts, each list is a batch
    channel_idx_tracker = 0
    while channel_idx_tracker < len(candidate_channels_info):
        current_batch_channel_list = []
        current_batch_cumulative_liquidity = 0

        # Greedily build a batch until target liquidity is met or channels run out
        temp_idx = channel_idx_tracker
        while temp_idx < len(candidate_channels_info):
            channel = candidate_channels_info[temp_idx]
            current_batch_channel_list.append(channel)
            current_batch_cumulative_liquidity += channel["balance"]
            temp_idx += 1
            if current_batch_cumulative_liquidity >= target_batch_liquidity:
                break  # Batch has enough liquidity

        if current_batch_channel_list:  # If any channels were added to this batch
            all_formed_batches_info.append(current_batch_channel_list)
        channel_idx_tracker = temp_idx  # Update main tracker to where this batch ended

    # Handle "Avoid Small Last Batch" rule:
    # If there's more than one batch, and the last batch's total liquidity is less than the swap amount,
    # merge it into the second-to-last batch.
    if len(all_formed_batches_info) > 1:
        last_batch_list = all_formed_batches_info[-1]
        liquidity_of_last_batch = sum(ch["balance"] for ch in last_batch_list)
        if liquidity_of_last_batch < swap_amount_sats:
            print_color(
                f"Last formed batch has {liquidity_of_last_batch} sats, less than swap amount {swap_amount_sats}. Merging into previous batch.",
                Colors.WARNING,
            )
            all_formed_batches_info[-2].extend(
                last_batch_list
            )  # Add channels from last to second-last
            all_formed_batches_info.pop()  # Remove the (now merged) last batch

    # Final list of batches to attempt, each element is a list of channel_info dicts
    final_payment_batches_info = [
        batch for batch in all_formed_batches_info if batch
    ]  # Filter out any empty batches

    if not final_payment_batches_info:
        total_candidate_liquidity = sum(ch["balance"] for ch in candidate_channels_info)
        print_color(
            "Could not form any viable payment batches with the available candidate channels.",
            Colors.FAIL,
        )
        print_color(
            f"  Swap amount: {swap_amount_sats} sats. Target batch liquidity: {target_batch_liquidity} sats.",
            Colors.FAIL,
        )
        print_color(
            f"  Total liquidity from {len(candidate_channels_info)} candidates: {total_candidate_liquidity} sats.",
            Colors.FAIL,
        )
        if candidate_channels_info:
            print_color(
                f"  Largest candidate channel has {candidate_channels_info[0]['balance']} sats.",
                Colors.FAIL,
            )
        return False

    print_color(
        f"Formed {len(final_payment_batches_info)} payment batches with target liquidity ~{target_batch_liquidity} sats each (or remaining).",
        Colors.OKCYAN,
    )
    # --- End New Batch Formation Logic ---

    # Calculate base timeout_seconds for lncli's own execution
    lncli_timeout_seconds = 0
    if args.payment_timeout.lower().endswith("s"):
        lncli_timeout_seconds = int(args.payment_timeout[:-1])
    elif args.payment_timeout.lower().endswith("m"):
        lncli_timeout_seconds = int(args.payment_timeout[:-1]) * 60
    elif args.payment_timeout.lower().endswith("h"):
        lncli_timeout_seconds = int(args.payment_timeout[:-1]) * 3600
    else:
        try:
            lncli_timeout_seconds = int(args.payment_timeout)
        except ValueError:
            print_color(
                f"Invalid payment timeout '{args.payment_timeout}'. Defaulting to 300s for lncli.",
                Colors.WARNING,
            )
            lncli_timeout_seconds = 300  # Defaulting to 5 minutes for lncli itself

    # This should be based on lncli's own timeout plus a buffer, and remains constant for this batch's attempts.
    # This script_subprocess_timeout_for_this_batch is for the payinvoice command itself.
    payinvoice_script_subprocess_timeout = (
        lncli_timeout_seconds + SUBPROCESS_TIMEOUT_BUFFER_SECONDS
    )
    # Timeout for queryroutes subprocess will be derived from args.queryroutes_timeout

    overall_payment_attempt_batch_count = 0  # Counts distinct batches tried

    for batch_idx, current_batch_info_list in enumerate(final_payment_batches_info):
        if (
            args.max_payment_attempts is not None
            and overall_payment_attempt_batch_count >= args.max_payment_attempts
        ):
            print_color(
                f"Reached max payment batches to try ({args.max_payment_attempts}). Stopping.",
                Colors.WARNING,
            )
            break
        overall_payment_attempt_batch_count += 1

        current_batch_ids = [ch["id"] for ch in current_batch_info_list]
        current_batch_liquidity_sum = sum(
            ch["balance"] for ch in current_batch_info_list
        )

        aliases_in_batch = [ch["alias"] for ch in current_batch_info_list]
        if len(aliases_in_batch) > 5:  # Keep printout manageable
            aliases_str = (
                ", ".join(aliases_in_batch[:3])
                + f"... ({len(aliases_in_batch) - 3} more)"
            )
        else:
            aliases_str = ", ".join(aliases_in_batch)

        print_color(
            f"\nAttempting Batch {overall_payment_attempt_batch_count}/{len(final_payment_batches_info)} (Total Liquidity: {current_batch_liquidity_sum} sats)",
            Colors.OKBLUE,
        )
        print_color(
            f"  Channels in this batch ({len(current_batch_ids)}): {aliases_str}",
            Colors.OKCYAN,
        )
        if args.verbose or args.debug:
            print_color(f"  Channel IDs: {', '.join(current_batch_ids)}", Colors.OKCYAN)

        # Inner loop for "in transition" retries for the CURRENT batch
        while True:
            # --- QueryRoutes for each channel in the batch BEFORE trying to pay ---
            # This 'while True' loop is for payinvoice retries (e.g. "in transition")
            # The queryroutes logic will be tried for each channel *within* this batch attempt.

            payment_attempted_for_this_batch_iteration = False
            successful_payinvoice_for_this_batch = False

            for (
                channel_info
            ) in (
                current_batch_info_list
            ):  # Iterate through channels in the current batch
                chan_id_to_try = channel_info["id"]
                chan_alias_to_try = channel_info["alias"]
                print_color(
                    f"  Attempting QueryRoutes for channel: {chan_alias_to_try} ({chan_id_to_try})",
                    Colors.OKCYAN,
                )

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

                query_command_parts = (
                    [lncli_path]
                    + lnd_connection_params
                    + [
                        "queryroutes",
                        "--dest",
                        invoice_str,
                        "--amt",
                        str(args.amount),
                        "--outgoing_chan_id",
                        str(chan_id_to_try),
                        "--timeout",
                        args.queryroutes_timeout,  # lncli's internal timeout for queryroutes
                    ]
                )
                if args.ppm is not None:
                    fee_limit_sats = math.floor(args.amount * args.ppm / 1_000_000)
                    query_command_parts.extend(["--fee_limit", str(fee_limit_sats)])
                else:  # Default to 0 fee_limit if --ppm not specified
                    query_command_parts.extend(["--fee_limit", "0"])

                if args.lncli_cltv_limit > 0:
                    query_command_parts.extend(
                        ["--cltv_limit", str(args.lncli_cltv_limit)]
                    )

                # Calculate subprocess timeout for queryroutes
                queryroutes_subprocess_timeout_seconds = 30  # Default
                try:
                    if args.queryroutes_timeout.lower().endswith("s"):
                        queryroutes_subprocess_timeout_seconds = int(
                            args.queryroutes_timeout[:-1]
                        )
                    elif args.queryroutes_timeout.lower().endswith("m"):
                        queryroutes_subprocess_timeout_seconds = (
                            int(args.queryroutes_timeout[:-1]) * 60
                        )
                    queryroutes_subprocess_timeout_seconds += (
                        10  # Buffer for subprocess
                    )
                except ValueError:
                    print_color(
                        f"Warning: Invalid --queryroutes-timeout format '{args.queryroutes_timeout}'. Using default for subprocess.",
                        Colors.WARNING,
                    )

                qr_success, qr_output, qr_error = run_command(
                    query_command_parts,
                    timeout=queryroutes_subprocess_timeout_seconds,
                    debug=args.debug,
                    expect_json=True,
                    dry_run_output=f"lncli queryroutes via {chan_id_to_try}",
                )

                if (
                    args.debug and qr_success
                ):  # Simulate successful queryroutes in debug
                    # Explicitly prepare mock data components
                    mock_total_fees_msat = "100"
                    # Simplify mock_total_time_lock to avoid Pylance issue with decoded_invoice in this specific block
                    # Use a plausible fixed value for the mock.
                    # The runtime logic for non-debug paths is unaffected.
                    mock_total_time_lock = (
                        70  # Example: Plausible fixed total_time_lock for mock
                    )

                    mock_qr_data = {
                        "routes": [
                            {
                                "total_fees_msat": mock_total_fees_msat,
                                "total_time_lock": mock_total_time_lock,
                                "hops": [
                                    {
                                        "chan_id": chan_id_to_try,
                                        "pub_key": "mock_next_hop_pubkey",
                                    }
                                ],
                            }
                        ],
                        "success_prob": 0.9,
                    }
                    print_color(
                        f"[DEBUG] Simulated queryroutes success for {chan_id_to_try}: {mock_qr_data}",
                        Colors.OKGREEN,
                    )
                    qr_output = mock_qr_data  # ensure qr_output is the mock data
                    # Fall through to payinvoice attempt in debug

                if (
                    qr_success
                    and isinstance(qr_output, dict)
                    and qr_output.get("routes")
                    and len(qr_output["routes"]) > 0
                ):
                    print_color(
                        f"  QueryRoutes successful for channel {chan_alias_to_try} ({chan_id_to_try}). Found {len(qr_output['routes'])} route(s).",
                        Colors.OKGREEN,
                    )
                    if args.verbose:
                        print_color(
                            f"    Route details: {json.dumps(qr_output['routes'][0], indent=2)}",
                            Colors.OKCYAN,
                        )

                    # --- Attempt lncli payinvoice using this specific channel ---
                    # We use only the chan_id that QueryRoutes succeeded for this specific attempt.
                    # The `construct_lncli_command` will build the command for this single channel.
                    # We reset the "in_transition_retry_count_for_this_batch" as this is a new pay attempt
                    # for potentially a different channel within the batch, after a successful query.

                    # Reset retry count for this specific payinvoice attempt
                    in_transition_retry_count_for_this_pay_attempt = 0
                    current_in_transition_retry_delay_seconds_for_pay = (
                        IN_TRANSITION_RETRY_INITIAL_DELAY_SECONDS
                    )

                    while (
                        True
                    ):  # Inner loop for payinvoice retries for *this specific channel*
                        payment_attempted_for_this_batch_iteration = (
                            True  # Mark that we tried to pay
                        )
                        actual_command_list, display_command_str = (
                            construct_lncli_command(
                                config,
                                args,
                                invoice_str,
                                [
                                    chan_id_to_try
                                ],  # Pass only the successfully queried channel
                            )
                        )

                        if in_transition_retry_count_for_this_pay_attempt > 0:
                            print_color(
                                f"    Retrying payinvoice via {chan_alias_to_try} (in transition attempt {in_transition_retry_count_for_this_pay_attempt}/{MAX_IN_TRANSITION_RETRIES}) ",
                                Colors.OKCYAN,
                            )
                        else:
                            print_color(
                                f"    Attempting payinvoice via channel: {chan_alias_to_try} ({chan_id_to_try})",
                                Colors.OKBLUE,
                            )

                        command_success, output, error_stderr = run_command(
                            actual_command_list,
                            timeout=payinvoice_script_subprocess_timeout,
                            debug=args.debug,
                            expect_json=True,
                            dry_run_output=f"lncli payinvoice via {chan_id_to_try}",
                            success_codes=[0],
                            display_str_override=display_command_str,
                            attempt_graceful_terminate_on_timeout=True,
                        )

                        if command_success:
                            if args.debug:
                                print_color(
                                    f"[DEBUG] Payinvoice via {chan_id_to_try} successful (simulated).",
                                    Colors.OKGREEN,
                                )
                                return True  # Overall success

                            if isinstance(output, dict):
                                payment_error_field = output.get("payment_error")
                                payment_preimage = output.get("payment_preimage")
                                top_level_status = output.get("status")
                                failure_reason = output.get("failure_reason")
                                is_successful_payment = (
                                    payment_preimage
                                    and payment_preimage != ""
                                    and (
                                        top_level_status == "SUCCEEDED"
                                        or not top_level_status
                                    )
                                    and (
                                        failure_reason == "FAILURE_REASON_NONE"
                                        or not failure_reason
                                    )
                                    and (
                                        not payment_error_field
                                        or payment_error_field == ""
                                    )
                                )
                                if is_successful_payment:
                                    print_color(
                                        f"Invoice payment successful via channel {chan_alias_to_try}! Preimage: {payment_preimage}",
                                        Colors.OKGREEN,
                                    )
                                    # (Existing logic to print route details)
                                    return True  # Overall payment success
                                else:
                                    print_color(
                                        f"    Payinvoice via {chan_alias_to_try} completed (exit 0) but content indicates failure.",
                                        Colors.WARNING,
                                    )
                                    if args.verbose:
                                        print_color(
                                            f"      JSON: {json.dumps(output, indent=2)}",
                                            Colors.WARNING,
                                        )
                                    break  # Break from this payinvoice attempt's retry loop, try next channel in batch
                            else:  # lncli exited 0 but output was not JSON
                                print_color(
                                    f"    Payinvoice via {chan_alias_to_try} completed (exit 0) but output not JSON.",
                                    Colors.WARNING,
                                )
                                break  # Break from this payinvoice attempt's retry loop
                        else:  # payinvoice command failed (non-zero exit)
                            err_str_combined = str(error_stderr) + str(output)
                            if "invoice is already paid" in err_str_combined.lower():
                                print_color(
                                    "Invoice was already paid. Success.", Colors.OKGREEN
                                )
                                return True  # Overall success

                            is_in_transition = (
                                "payment is in transition" in err_str_combined.lower()
                            )
                            if is_in_transition:
                                in_transition_retry_count_for_this_pay_attempt += 1
                                if (
                                    in_transition_retry_count_for_this_pay_attempt
                                    > MAX_IN_TRANSITION_RETRIES
                                ):
                                    print_color(
                                        f"    Max 'in transition' retries for payinvoice via {chan_alias_to_try} reached.",
                                        Colors.FAIL,
                                    )
                                    break  # Break from this payinvoice attempt's retry loop
                                print_color(
                                    f"    'Payment in transition' for payinvoice via {chan_alias_to_try}. Retrying (attempt {in_transition_retry_count_for_this_pay_attempt +1}). Waiting {current_in_transition_retry_delay_seconds_for_pay}s...",
                                    Colors.WARNING,
                                )
                                time.sleep(
                                    current_in_transition_retry_delay_seconds_for_pay
                                )
                                current_in_transition_retry_delay_seconds_for_pay *= 2
                                continue  # Retry payinvoice for this channel
                            else:  # Non-transitional error for payinvoice
                                print_color(
                                    f"    Payinvoice via {chan_alias_to_try} failed (non-transitional).",
                                    Colors.FAIL,
                                )
                                if args.verbose:
                                    if error_stderr:
                                        print_color(
                                            f"      Stderr: {error_stderr.strip()}",
                                            Colors.FAIL,
                                        )
                                    if isinstance(output, str) and output.strip():
                                        print_color(
                                            f"      Stdout: {output.strip()}",
                                            Colors.FAIL,
                                        )
                                break  # Break from this payinvoice attempt's retry loop, try next channel in batch
                    # End of while True for this channel's payinvoice retries
                    # If we broke out of the payinvoice retry loop for this channel without returning True, it means this channel ultimately failed.
                    # The outer for loop will then try the next channel in the batch with queryroutes.

                else:  # QueryRoutes failed for this channel_id
                    print_color(
                        f"  QueryRoutes failed for channel {chan_alias_to_try} ({chan_id_to_try}).",
                        Colors.WARNING,
                    )
                    if args.verbose or args.debug:
                        if qr_error:
                            print_color(
                                f"    Stderr: {qr_error.strip()}", Colors.WARNING
                            )
                        if isinstance(qr_output, str) and qr_output.strip():
                            print_color(
                                f"    Stdout: {qr_output.strip()}", Colors.WARNING
                            )
                        elif isinstance(qr_output, dict):
                            print_color(
                                f"    Full Response: {json.dumps(qr_output, indent=2)}",
                                Colors.WARNING,
                            )
                    # Continue to the next channel in the batch for QueryRoutes

            # --- End of for loop iterating through channels in current_batch_info_list ---
            # If we've looped through all channels in this batch and haven't returned True (overall success),
            # then this batch attempt is considered failed.

            if (
                successful_payinvoice_for_this_batch
            ):  # This flag would be set if any payinvoice in the batch loop succeeded
                return True  # Should have been caught earlier, but as a safeguard

            # If no payment was even attempted (e.g. all queryroutes failed for this batch)
            # or all attempted payments in this batch failed.
            print_color(
                f"All channels in Batch {overall_payment_attempt_batch_count} failed QueryRoutes or subsequent PayInvoice.",
                Colors.FAIL,
            )
            break  # Break from the outer while True (which was for "in_transition_retry_count_for_this_batch")
            # because we've exhausted all channels in this batch.

        # --- After inner while loop (this batch's attempts are done) ---
        # If payment was successful inside the loop, we'd have returned True.
        # So if we reach here, this batch {overall_payment_attempt_batch_count} didn't result in a confirmed payment.

        # Check if there are more batches to try AND we haven't hit the overall max attempts
        if batch_idx + 1 < len(final_payment_batches_info) and (
            args.max_payment_attempts is None
            or overall_payment_attempt_batch_count < args.max_payment_attempts
        ):
            print_color(
                f"Retrying with next batch in 30 seconds...", Colors.WARNING
            )  # Using the existing 30s delay
            time.sleep(30)
        # No "else" needed: if it's the last configured attempt or last batch, outer loop terminates.

    print_color(
        "All payment attempts with available channel batches failed to confirm success.",
        Colors.FAIL,
    )
    return False


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
    )

    if args.debug and success:  # Simulate successful decode in debug
        # Try to match the amount from the invoice if possible, or use args.amount
        # For simplicity in mock, using args.amount
        mock_decoded_data = {
            "destination": "debug_destination_pubkey_from_decode",
            "payment_hash": "debug_payment_hash_from_decode",
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
        if not output.get("destination") or not output.get("num_satoshis"):
            print_color(
                f"Decoded invoice missing 'destination' or 'num_satoshis'. Response: {json.dumps(output)}",
                Colors.FAIL,
            )
            return None

        # Verify amount if possible (num_satoshis can be 0 for "any amount" invoices, but Boltz will specify)
        decoded_amount = int(output.get("num_satoshis", 0))
        if decoded_amount != args.amount:
            print_color(
                f"Warning: Decoded invoice amount ({decoded_amount} sats) differs from requested swap amount ({args.amount} sats).",
                Colors.WARNING,
            )
            # Decide if this is a critical error or just a warning. For Boltz, it should match.
            # If this check is too strict for some invoices, it can be relaxed or made conditional.

        print_color("Payment request decoded successfully.", Colors.OKGREEN)
        if args.verbose:
            print_color(f"  Destination: {output.get('destination')}", Colors.OKCYAN)
            print_color(f"  Amount: {output.get('num_satoshis')} sats", Colors.OKCYAN)
            print_color(
                f"  CLTV Expiry (delta from current block): {output.get('cltv_expiry')}",
                Colors.OKCYAN,
            )
        return output
    else:
        print_color("Failed to decode payment request.", Colors.FAIL)
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


def main():
    """Main execution flow."""
    args, parser = parse_arguments()
    config = load_config(args.config)

    # Override args with config values if args are at their defaults
    default_lndg_api = parser.get_default("lndg_api")
    if args.lndg_api == default_lndg_api and "lndg_api_url" in config:
        print(
            f"[INFO] Overriding default LNDg API '{args.lndg_api}' with config value '{config['lndg_api_url']}'."
        )
        args.lndg_api = config["lndg_api_url"]

    # Then print effective settings
    print_color(
        "\n--- Effective Settings After Config Override ---", Colors.HEADER, bold=True
    )
    print_color(f"LNDg API: {args.lndg_api}", Colors.OKCYAN)
    print_color(f"Swap Amount (LN): {args.amount} sats", Colors.OKCYAN)
    print_color(f"Min Local Balance (candidates): {args.capacity} sats", Colors.OKCYAN)
    print_color(
        f"Max Local Fee (candidates): {args.local_fee_limit} ppm", Colors.OKCYAN
    )
    # Print other relevant args if they can also be overridden by config

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

    try:  # Start of try block for KeyboardInterrupt
        lbtc_address = None
        if args.custom_destination_address:
            lbtc_address = args.custom_destination_address
            print_color(
                f"\nUsing custom L-BTC destination address: {lbtc_address}",
                Colors.WARNING,
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
                    sys.exit(1)
                print_color(
                    "Confirmation received. Proceeding with custom address.",
                    Colors.OKGREEN,
                )
            else:
                print_color(
                    "Confirmation for custom address skipped due to --force flag.",
                    Colors.OKBLUE,
                )
        else:
            lbtc_address = get_lbtc_address(config["pscli_path"], args.debug)

        if not lbtc_address:
            print_color(
                "\nExiting: L-BTC address not available or confirmed.",
                Colors.FAIL,
                bold=True,
            )
            sys.exit(1)

        candidate_channels_info = get_swap_candidate_channels(
            args.lndg_api,  # Use the potentially overridden args.lndg_api
            config["lndg_username"],
            config["lndg_password"],
            args.capacity,
            args.local_fee_limit,
            args.debug,
        )
        if not candidate_channels_info:
            print_color(
                "\nExiting: No suitable swap candidate channels found.",
                Colors.FAIL,
                bold=True,
            )
            sys.exit(1)
        print_color(
            f"Total candidate channels for payment: {len(candidate_channels_info)}",
            Colors.OKBLUE,
        )

        swap_id, lightning_invoice, boltz_response_dict = create_boltz_swap(
            config["boltzcli_path"],
            config["boltzd_tlscert_path"],
            config["boltzd_admin_macaroon_path"],
            args.amount,
            lbtc_address,
            args.description,
            args.debug,
        )
        if not lightning_invoice:
            print_color(
                "\nExiting: Failed to create Boltz swap or get invoice.",
                Colors.FAIL,
                bold=True,
            )
            sys.exit(1)

        decoded_invoice = decode_payreq(config, args, lightning_invoice)
        if not decoded_invoice:
            print_color(
                "Failed to decode Boltz invoice, cannot proceed with payment.",
                Colors.FAIL,
            )
            return False

        payment_successful = pay_lightning_invoice(
            config,
            args,
            lightning_invoice,
            candidate_channels_info,
            args.debug,
        )

        print_color("\n--- Swap Summary ---", Colors.HEADER, bold=True)
        print_color(f"L-BTC Destination Address: {lbtc_address}", Colors.OKCYAN)
        print_color(f"Boltz Swap ID: {swap_id if swap_id else 'N/A'}", Colors.OKCYAN)

        if boltz_response_dict and isinstance(boltz_response_dict, dict):
            b_expected_amount = boltz_response_dict.get("expectedAmount")
            # Assuming invoiceAmount is the LN invoice amount Boltz expects to be paid
            b_invoice_amount = None
            if "invoice" in boltz_response_dict:
                # Attempt to decode the invoice to get its amount (more robust)
                # This is a placeholder for actual invoice decoding if needed,
                # for now, we rely on what Boltz might provide directly.
                # For simplicity, let's assume boltz_response_dict might have an "invoiceAmount" key
                # or we could parse it from the invoice string if necessary.
                b_invoice_amount = boltz_response_dict.get(
                    "invoiceAmount"
                )  # Check if Boltz provides this

            b_lockup_addr = boltz_response_dict.get("lockupAddress")

            if b_invoice_amount:  # This is the LN invoice amount
                print_color(
                    f"Boltz Expected LN Payment Amount: {b_invoice_amount} sats",  # Clarified label
                    Colors.OKCYAN,
                )
            if b_expected_amount:  # This is what Boltz should send on-chain (L-BTC)
                print_color(
                    f"Boltz Est. L-BTC Sent (onchain): {b_expected_amount} sats",
                    Colors.OKCYAN,
                )
            if b_lockup_addr:
                print_color(
                    f"Boltz L-BTC Lockup Address: {b_lockup_addr}", Colors.OKCYAN
                )

        if payment_successful:
            print_color("Swap Initiated & LN Invoice Paid!", Colors.OKGREEN, bold=True)
            print_color("Monitor your Liquid wallet for L-BTC.", Colors.OKGREEN)
            if swap_id and swap_id != "unknown_swap_id":
                print_color(
                    f"Check status: {config['boltzcli_path']} --tlscert {config['boltzd_tlscert_path']} --macaroon {config['boltzd_admin_macaroon_path']} swapinfo {swap_id}",
                    Colors.OKCYAN,
                )
        else:
            print_color("Swap Failed: LN invoice not paid.", Colors.FAIL, bold=True)
            print_color(
                "No funds should have left LN wallet if all attempts failed.",
                Colors.WARNING,
            )
            if swap_id and swap_id != "unknown_swap_id":
                print_color(
                    f"Check status: {config['boltzcli_path']} --tlscert {config['boltzd_tlscert_path']} --macaroon {config['boltzd_admin_macaroon_path']} swapinfo {swap_id}",
                    Colors.WARNING,
                )
        sys.exit(0 if payment_successful else 1)  # Exit with 0 on success, 1 on failure

    except KeyboardInterrupt:
        print_color(
            "\n\nScript aborted by user (CTRL-C). Exiting.", Colors.WARNING, bold=True
        )
        # Attempt to provide Boltz swap ID if available, for manual checking
        # Check if swap_id was defined before interruption
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
        sys.exit(130)  # Standard exit code for CTRL+C
    except Exception as e:
        print_color(f"\nAn unexpected error occurred: {e}", Colors.FAIL, bold=True)
        import traceback

        print_color(
            traceback.format_exc(), Colors.FAIL
        )  # Print full traceback for unexpected errors
        sys.exit(2)  # General error exit code


if __name__ == "__main__":
    main()
