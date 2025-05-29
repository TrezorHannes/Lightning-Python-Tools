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
python boltz_swap-out.py --amount 1000000 --capacity 2000000 --fee-limit 5 --debug

Options:
  --amount SATS          (Required) The amount in satoshis to swap from LN to L-BTC.
  --capacity SATS        Minimum local balance on a channel to be a swap candidate.
                         (Default: 3000000)
  --fee-limit PPM        Maximum local fee rate (ppm) for candidate channels.
                         (Default: 10)
  --max-parts INT        Max parts for `lncli payinvoice --max_parts`. (Default: 16)
  --payment-timeout STR  Timeout for `lncli payinvoice` (e.g., "10m", "1h").
                         (Default: "10m")
  --description STR      Optional description for the Boltz swap invoice.
                         (Default: "LNDg-Boltz-Swap-Out")
  --debug                Enable debug mode: prints commands, no actual execution.
                         (Highly recommended for first runs).
  -h, --help             Show this help message and exit.

Workflow:
1.  Fetches a new L-BTC address using `pscli`.
2.  Queries LNDg API to find suitable outgoing channels based on `--capacity`
    and `--fee-limit`.
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


def parse_arguments():
    """Parses command-line arguments."""
    # Prepare epilog text safely
    epilog_text = "Automated LN to L-BTC swaps. Use with caution. Refer to script source for full disclaimer."  # Default fallback
    if __doc__:
        disclaimer_marker = "Disclaimer:"
        disclaimer_start_index = __doc__.find(disclaimer_marker)
        if disclaimer_start_index != -1:
            # Take everything from "Disclaimer:" onwards
            epilog_text = __doc__[disclaimer_start_index:].strip()

    parser = argparse.ArgumentParser(
        description="Automates LN to L-BTC swaps using Boltz Client (`boltzcli`).\n"
        "Executable paths and `boltzd` RPC connection details are read from config.ini.",
        formatter_class=argparse.RawTextHelpFormatter,  # To preserve help text formatting
        epilog=epilog_text,  # Use the safely prepared epilog_text
    )
    parser.add_argument(
        "--amount",
        type=int,
        required=True,
        help="(Required) The amount in satoshis to swap from LN to L-BTC.",
    )
    parser.add_argument(
        "--capacity",
        type=int,
        default=3000000,
        help="Minimum local balance on a channel to be a swap candidate (sats).\n(Default: 3000000)",
    )
    parser.add_argument(
        "--fee-limit",
        type=int,
        default=10,  # Defaulting to a low fee limit
        help="Maximum local fee rate (ppm) for candidate channels.\n(Default: 10)",
    )
    parser.add_argument(
        "--max-parts",
        type=int,
        default=16,
        help="Max parts for `lncli payinvoice --max_parts`.\n(Default: 16)",
    )
    parser.add_argument(
        "--payment-timeout",
        type=str,
        default="10m",
        help='Timeout for `lncli payinvoice` (e.g., "10m", "1h").\n(Default: "10m")',
    )
    parser.add_argument(
        "--description",
        type=str,
        default="LNDg-Boltz-Swap-Out",
        help='Optional description for the Boltz swap invoice.\n(Default: "LNDg-Boltz-Swap-Out")',
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode: prints commands, no actual execution.\n(Highly recommended for first runs).",
    )
    # --help is added automatically by argparse

    if len(sys.argv) == 1:  # If no arguments are passed, print help and exit
        parser.print_help(sys.stderr)
        sys.exit(1)

    return parser.parse_args()


def load_config():
    """Loads LNDg, Paths, and Boltz RPC configuration from ../config.ini."""
    parent_dir = os.path.dirname(os.path.abspath(__file__))
    config_file_path = os.path.join(parent_dir, "..", "config.ini")
    config = configparser.ConfigParser()

    if not os.path.exists(config_file_path):
        print_color(
            f"Error: Configuration file not found at {config_file_path}", Colors.FAIL
        )
        print_color(
            "Please ensure `config.ini` exists in the parent directory and is correctly named.",
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
):
    """
    Executes a system command.
    Returns a tuple: (success: bool, output: str or dict if expect_json else str, error_message: str)
    """
    if success_codes is None:
        success_codes = [0]
    command_str = " ".join(command_parts)
    print_color(f"Executing: {command_str}", Colors.OKCYAN)

    if debug:
        print_color(f"[DEBUG] {dry_run_output}: {command_str}", Colors.WARNING)
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
                    f"[DEBUG] No specific mock for: {command_str}. Using default mock.",
                    Colors.WARNING,
                )
                return (
                    True,
                    {
                        "message": "Dry run success - default mock",
                        "details": command_str,
                    },
                    "",
                )
        return True, f"Dry run: {command_str}", ""

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
        error_msg = f"Command timed out after {timeout} seconds: {command_str}"
        print_color(error_msg, Colors.FAIL)
        if process.poll() is None:
            process.kill()
            stdout_after_kill, stderr_after_kill = process.communicate()
            error_msg += f"\nKilled process. Stdout after kill: {stdout_after_kill.strip()}. Stderr after kill: {stderr_after_kill.strip()}"
        return False, "", error_msg
    except Exception as e:
        error_msg = f"An unexpected error occurred while running command: {command_str}\nError: {e}"
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
    lndg_api_url, lndg_username, lndg_password, capacity_threshold, fee_limit_ppm, debug
):
    """
    Retrieves and filters channel IDs from LNDg API.
    Returns a list of channel IDs.
    """
    print_color(
        f"\nStep 2: Finding swap candidate channels (Local Bal > {capacity_threshold} sats, Fee <= {fee_limit_ppm} ppm)...",
        Colors.HEADER,
    )
    api_url = lndg_api_url + "/api/channels?limit=5000&is_open=true&is_active=true"
    candidate_channels = []

    if debug:
        print_color(
            "[DEBUG] Skipping LNDg API call in debug mode. Using mock channel IDs.",
            Colors.WARNING,
        )
        return [
            "mock_chan_id_1",
            "mock_chan_id_2",
            "mock_chan_id_3",
            "mock_chan_id_4",
            "mock_chan_id_5",
            "mock_chan_id_6",
        ]

    try:
        response = requests.get(
            api_url, auth=(lndg_username, lndg_password), timeout=30
        )
        response.raise_for_status()
        data = response.json()

        if "results" in data:
            results = data["results"]
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
                if (
                    local_balance > capacity_threshold
                    and local_fee_rate <= fee_limit_ppm
                    and chan_id
                ):
                    candidate_channels.append(chan_id)
                    print_color(
                        f"  Found candidate: {alias} ({chan_id}), Local Bal: {local_balance}, Fee: {local_fee_rate}ppm",
                        Colors.OKGREEN,
                    )
            if not candidate_channels:
                print_color(
                    "No suitable swap candidate channels found.", Colors.WARNING
                )
            else:
                print_color(
                    f"Found {len(candidate_channels)} candidate channels.",
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
    return candidate_channels


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


def pay_lightning_invoice(
    lncli_path,
    invoice_str,
    candidate_chan_ids,
    max_parts,
    payment_timeout_str,
    debug,
):
    """
    Pays the Lightning invoice using lncli with retries on different channel batches.
    Returns True on success, False on failure.
    """
    print_color(
        f"\nStep 4: Attempting to pay Lightning invoice: {invoice_str[:40]}...{invoice_str[-40:]}",
        Colors.HEADER,
    )
    print_color(
        f"Timeout per attempt: {payment_timeout_str}, Max Parts: {max_parts}",
        Colors.OKCYAN,
    )

    if not candidate_chan_ids:
        print_color("No candidate channels for payment. Aborting.", Colors.FAIL)
        return False

    timeout_seconds = 0
    if payment_timeout_str.lower().endswith("s"):
        timeout_seconds = int(payment_timeout_str[:-1])
    elif payment_timeout_str.lower().endswith("m"):
        timeout_seconds = int(payment_timeout_str[:-1]) * 60
    elif payment_timeout_str.lower().endswith("h"):
        timeout_seconds = int(payment_timeout_str[:-1]) * 3600
    else:
        try:
            timeout_seconds = int(payment_timeout_str)
        except ValueError:
            print_color(
                f"Invalid payment timeout '{payment_timeout_str}'. Defaulting to 600s.",
                Colors.WARNING,
            )
            timeout_seconds = 600

    batch_size = 3
    num_batches = (len(candidate_chan_ids) + batch_size - 1) // batch_size

    for i in range(0, len(candidate_chan_ids), batch_size):
        current_batch_ids = candidate_chan_ids[i : i + batch_size]
        print_color(
            f"\nAttempt {i//batch_size + 1}/{num_batches} with channels: {', '.join(current_batch_ids)}",
            Colors.OKBLUE,
        )

        command = [
            lncli_path,
            "payinvoice",
            "--force",
            "--json",  # Ensure JSON output for parsing
            # "--inflight_updates", # Removing for cleaner final JSON output
            "--pay_req",
            invoice_str,
            "--fee_limit_percent",
            "1",
            "--timeout",
            payment_timeout_str,
            "--max_parts",
            str(max_parts),
        ]
        for chan_id in current_batch_ids:
            command.extend(["--outgoing_chan_id", chan_id])

        subprocess_actual_timeout = timeout_seconds + 60

        # Ensure run_command is called with success_codes that include expected error codes for "already paid"
        # However, for the *first* attempt, a non-zero exit code means failure.
        # For subsequent attempts, "already paid" is an error but means prior success.
        # The current loop structure relies on the *first* success returning True.

        success_exit_codes = [0]  # Default: only exit code 0 is success for run_command
        # If this is not the first attempt, "already paid" (exit code 1 typically for lncli)
        # could be an indicator of prior success. But this makes run_command's success tricky.
        # It's better to rely on parsing the content of the *first* successful attempt.

        command_success, output, error_stderr = run_command(
            command,
            timeout=subprocess_actual_timeout,
            debug=debug,
            expect_json=True,
            dry_run_output="lncli payinvoice command",
            success_codes=success_exit_codes,  # Only 0 is true success for the payment itself
        )

        if command_success:  # lncli exited with 0
            if debug:  # Debug mock for payinvoice should simulate success
                print_color(
                    f"[DEBUG] Payment successful (simulated). Preimage: {output.get('payment_preimage', 'N/A')}",
                    Colors.OKGREEN,
                )
                return True  # Exit after first (mocked) successful batch

            if isinstance(output, dict):
                payment_error_field = output.get("payment_error")
                payment_preimage = output.get("payment_preimage")
                top_level_status = output.get(
                    "status"
                )  # e.g., "SUCCEEDED", "FAILED", "IN_FLIGHT"
                failure_reason = output.get(
                    "failure_reason"
                )  # e.g., "FAILURE_REASON_NONE" on success

                # Primary success indicators:
                # 1. Valid payment_preimage is present.
                # 2. Top-level status is "SUCCEEDED".
                # 3. No payment_error field, or it's empty/null.
                # 4. failure_reason is "FAILURE_REASON_NONE".

                is_successful_payment = (
                    payment_preimage
                    and payment_preimage != ""
                    and (
                        top_level_status == "SUCCEEDED" or not top_level_status
                    )  # SUCCEEDED is explicit, or no status field if older lncli
                    and (failure_reason == "FAILURE_REASON_NONE" or not failure_reason)
                    and (not payment_error_field or payment_error_field == "")
                )

                if is_successful_payment:
                    print_color(
                        f"Invoice payment successful! Preimage: {payment_preimage}",
                        Colors.OKGREEN,
                    )
                    if (
                        "payment_route" in output and output["payment_route"]
                    ):  # Check if route is not None or empty
                        print_color(
                            f"  Route details: {json.dumps(output['payment_route'], indent=2)}",
                            Colors.OKCYAN,
                        )
                    else:
                        # Check htlcs array for the successful one if top-level route is missing
                        successful_htlc_route = None
                        if "htlcs" in output and isinstance(output["htlcs"], list):
                            for htlc in output["htlcs"]:
                                if htlc.get("status") == "SUCCEEDED" and htlc.get(
                                    "route"
                                ):
                                    successful_htlc_route = htlc.get("route")
                                    break
                        if successful_htlc_route:
                            print_color(
                                f"  Successful HTLC Route details: {json.dumps(successful_htlc_route, indent=2)}",
                                Colors.OKCYAN,
                            )
                    return True  # Crucial: exit function on first confirmed success
                else:
                    # Payment attempt seemed to succeed based on exit code 0, but content analysis says no.
                    print_color(
                        f"Payment attempt completed (exit 0) but content indicates failure or ambiguity.",
                        Colors.WARNING,
                    )
                    print_color(
                        f"  Status: {top_level_status}, Preimage: {payment_preimage}, Error: {payment_error_field}, FailureReason: {failure_reason}",
                        Colors.WARNING,
                    )
                    print_color(
                        f"  Full JSON output: {json.dumps(output, indent=2)}",
                        Colors.WARNING,
                    )
                    # Continue to next batch as this attempt wasn't definitively successful by content
            else:  # lncli exited 0 but output was not JSON
                print_color(
                    f"Payment attempt completed (exit 0) but output was not valid JSON: {output}",
                    Colors.WARNING,
                )
                # Continue to next batch

        else:  # command_success is False, meaning lncli exited with non-zero code
            print_color(f"Payment attempt command failed.", Colors.FAIL)
            if error_stderr:
                print_color(f"  Stderr: {error_stderr.strip()}", Colors.FAIL)

            # Check if the error is "invoice is already paid"
            # output from run_command is stdout in this case. error_stderr is stderr.
            err_str_combined = str(error_stderr) + str(
                output
            )  # Check both stdout and stderr for the message
            if "invoice is already paid" in err_str_combined.lower() or (
                "code = AlreadyExists desc = invoice is already paid"
                in err_str_combined
            ):
                print_color(
                    "Invoice was already paid (likely by a previous attempt in this script run). Considering this a success.",
                    Colors.OKGREEN,
                )
                return True  # Exit, considering it success because it's paid

            if isinstance(output, str) and output.strip():
                print_color(f"  Stdout: {output.strip()}", Colors.FAIL)
            elif isinstance(output, dict):
                print_color(
                    f"  Output (JSON): {json.dumps(output, indent=2)}", Colors.FAIL
                )

        # If we reach here, the current batch was not definitively successful and we should try the next one
        if i + batch_size < len(candidate_chan_ids):
            print_color("Retrying with next batch in 5 seconds...", Colors.WARNING)
            time.sleep(5)
        # No "else" needed here, if it's the last batch and failed, loop terminates and function returns False below

    print_color(
        "All payment attempts with available channel batches failed to confirm success.",
        Colors.FAIL,
    )
    return False


def main():
    """Main execution flow."""
    args = parse_arguments()
    app_config = load_config()

    print_color("--- Boltz LN to L-BTC Swap Initiator ---", Colors.HEADER, bold=True)
    if args.debug:
        print_color(
            "DEBUG MODE ENABLED: No actual transactions.", Colors.WARNING, bold=True
        )

    print_color(f"Swap Amount (LN): {args.amount} sats", Colors.OKCYAN)
    print_color(f"Min Local Balance (candidates): {args.capacity} sats", Colors.OKCYAN)
    print_color(f"Max Local Fee (candidates): {args.fee_limit} ppm", Colors.OKCYAN)
    print_color(f"LNDg API: {app_config['lndg_api_url']}", Colors.OKCYAN)
    print_color(f"Paths (from config.ini):", Colors.OKCYAN)
    print_color(f"  lncli: {app_config['lncli_path']}", Colors.OKCYAN)
    print_color(f"  pscli: {app_config['pscli_path']}", Colors.OKCYAN)
    print_color(f"  boltzcli: {app_config['boltzcli_path']}", Colors.OKCYAN)
    print_color(
        f"  boltzd TLS Cert: {app_config['boltzd_tlscert_path']}", Colors.OKCYAN
    )
    print_color(
        f"  boltzd Admin Macaroon: {app_config['boltzd_admin_macaroon_path']}",
        Colors.OKCYAN,
    )

    if args.description:
        print_color(f"Swap Description: {args.description}", Colors.OKCYAN)

    lbtc_address = get_lbtc_address(app_config["pscli_path"], args.debug)
    if not lbtc_address:
        print_color("\nExiting: Failed to get L-BTC address.", Colors.FAIL, bold=True)
        sys.exit(1)

    candidate_channels = get_swap_candidate_channels(
        app_config["lndg_api_url"],
        app_config["lndg_username"],
        app_config["lndg_password"],
        args.capacity,
        args.fee_limit,
        args.debug,
    )
    if not candidate_channels:
        print_color(
            "\nExiting: No suitable swap candidate channels found.",
            Colors.FAIL,
            bold=True,
        )
        sys.exit(1)
    print_color(
        f"Total candidate channels for payment: {len(candidate_channels)}",
        Colors.OKBLUE,
    )

    swap_id, lightning_invoice, boltz_response_dict = create_boltz_swap(
        app_config["boltzcli_path"],
        app_config["boltzd_tlscert_path"],
        app_config["boltzd_admin_macaroon_path"],
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

    payment_successful = pay_lightning_invoice(
        app_config["lncli_path"],
        lightning_invoice,
        candidate_channels,
        args.max_parts,
        args.payment_timeout,
        args.debug,
    )

    print_color("\n--- Swap Summary ---", Colors.HEADER, bold=True)
    print_color(f"L-BTC Destination Address: {lbtc_address}", Colors.OKCYAN)
    print_color(f"Boltz Swap ID: {swap_id if swap_id else 'N/A'}", Colors.OKCYAN)

    if boltz_response_dict and isinstance(boltz_response_dict, dict):
        b_expected_amount = boltz_response_dict.get("expectedAmount")
        b_invoice_amount = boltz_response_dict.get("invoiceAmount")
        b_lockup_addr = boltz_response_dict.get("lockupAddress")

        if b_invoice_amount:
            print_color(
                f"Boltz Stated LN Invoice Amount: {b_invoice_amount} sats",
                Colors.OKCYAN,
            )
        if b_expected_amount:
            print_color(
                f"Boltz Est. L-BTC Sent (onchain): {b_expected_amount} sats",
                Colors.OKCYAN,
            )
        if b_lockup_addr:
            print_color(f"Boltz L-BTC Lockup Address: {b_lockup_addr}", Colors.OKCYAN)

    if payment_successful:
        print_color("Swap Initiated & LN Invoice Paid!", Colors.OKGREEN, bold=True)
        print_color("Monitor your Liquid wallet for L-BTC.", Colors.OKGREEN)
        if swap_id and swap_id != "unknown_swap_id":
            print_color(
                f"Check status: {app_config['boltzcli_path']} --tlscert {app_config['boltzd_tlscert_path']} --macaroon {app_config['boltzd_admin_macaroon_path']} swapinfo {swap_id}",
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
                f"Check status: {app_config['boltzcli_path']} --tlscert {app_config['boltzd_tlscert_path']} --macaroon {app_config['boltzd_admin_macaroon_path']} swapinfo {swap_id}",
                Colors.WARNING,
            )


if __name__ == "__main__":
    main()
