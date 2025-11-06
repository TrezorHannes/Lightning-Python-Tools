#!/usr/bin/env python3

"""
LND UTXO Consolidator Helper
============================

This script helps create an `lncli sendcoins` command to
consolidate UTXOs (Unspent Transaction Outputs).

It guides you interactively through the process:
1.  Calls `lncli listunspent` to find all available UTXOs.
2.  Asks for a selection strategy (Min-to-Max, Max-to-Min, Random).
3.  Asks whether to consolidate all UTXOs or just a subset.
4.  Allows filtering by sats amount (min/max).
5.  Asks for the fee strategy (--conf_target or --sat_per_vbyte).
6.  Asks for the destination Bitcoin address (with double confirmation).
7.  Builds the complete `lncli` command and displays it for review.

**This script does NOT execute the command.**
It only prints it, allowing you to review it and then
manually copy and paste it into your terminal.

All interactive questions can also be pre-filled via
command-line arguments. Run `python3 consolidate.py -h`
to see all options.

Disclaimer:
--------------------------------
This script is open-source and provided "as is" without any
warranty. The authors or contributors assume no liability
for financial losses, data loss, or any damages
arising from the use of this software. Bitcoin transactions
are irreversible.
Verify EVERY command carefully before executing it.
You are solely responsible for your actions.
"""

import sys
import os
import subprocess
import json
import configparser
import argparse
import shlex
import random

# --- Color definitions for terminal output ---
class Colors:
    """Simple color codes for the terminal."""
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'
    GREY = '\033[90m'

def print_color(text, color, bold=False, file=sys.stdout):
    """Prints colored text."""
    bold_code = Colors.BOLD if bold else ""
    print(f"{bold_code}{color}{text}{Colors.ENDC}", file=file)

# --- Helper functions for user input ---

def ask_yes_no(prompt, default=None):
    """Asks a Yes/No question and returns True/False."""
    if default is True:
        options = "(Y/n)"
    elif default is False:
        options = "(y/N)"
    else:
        options = "(y/n)"

    while True:
        inp = input(f"{Colors.YELLOW}[?] {prompt} {options}: {Colors.ENDC}").strip().lower()
        if not inp and default is not None:
            return default
        if inp in ('y', 'yes'):
            return True
        if inp in ('n', 'no'):
            return False
        print_color("Please answer with 'y' (yes) or 'n' (no).", Colors.RED)

def ask_for_number(prompt, default=None):
    """Asks for a number and returns an integer."""
    while True:
        default_str = f"[Default: {default}]" if default is not None else ""
        inp = input(f"{Colors.YELLOW}[?] {prompt} {default_str}: {Colors.ENDC}").strip()
        if not inp and default is not None:
            return default
        if not inp:
            print_color("Input is required.", Colors.RED)
            continue
        try:
            return int(inp)
        except ValueError:
            print_color("Invalid input. Please enter a whole number.", Colors.RED)

def ask_for_string(prompt, default=None):
    """Asks for a string and ensures it is not empty."""
    while True:
        default_str = f"[Default: {default}]" if default is not None else ""
        inp = input(f"{Colors.YELLOW}[?] {prompt} {default_str}: {Colors.ENDC}").strip()
        if not inp and default is not None:
            return default
        if inp:
            return inp
        print_color("Input must not be empty.", Colors.RED)

# --- Main logic ---

def main():
    """Runs the main script."""
    
    # --- 1. Set up ArgumentParser ---
    # (This also provides -h and the disclaimer)
    disclaimer = """
Disclaimer:
  This script is open-source and provided "as is" without any
  warranty. The authors or contributors assume no liability
  for financial losses, data loss, or any damages
  arising from the use of this software. Bitcoin transactions
  are irreversible.
  Verify EVERY command carefully before executing it.
  You are solely responsible for your actions.
"""
    parser = argparse.ArgumentParser(
        description="LND UTXO Consolidator Helper",
        epilog=disclaimer,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--address', type=str, help="Destination Bitcoin address (skips prompt)")
    parser.add_argument('--all', action='store_true', help="Consolidate all UTXOs (skips prompt)")
    parser.add_argument('--min_amt', type=int, help="Minimum target *sum* in sats (skips prompt)")
    parser.add_argument('--max_amt', type=int, help="Maximum target *sum* in sats (skips prompt)")
    parser.add_argument('--strategy', type=str, choices=['random', 'min-to-max', 'max-to-min'], help="UTXO selection strategy (skips prompt)")
    parser.add_argument('--conf_target', type=int, help="Fee strategy: target blocks (excludes --sat_per_vbyte)")
    parser.add_argument('--sat_per_vbyte', type=int, help="Fee strategy: sats/vbyte (excludes --conf_target)")
    
    args = parser.parse_args()

    # Check for fee strategy conflict
    if args.conf_target and args.sat_per_vbyte:
        print_color("Error: --conf_target and --sat_per_vbyte cannot be used at the same time.", Colors.RED, file=sys.stderr)
        sys.exit(1)

    print_color("--- LND UTXO Consolidator ---", Colors.HEADER, bold=True)

    # --- 2. Load Config.ini ---
    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        config_file_path = os.path.join(current_dir, "..", "config.ini")
        
        if not os.path.exists(config_file_path):
            print_color(f"Config file not found at: {config_file_path}", Colors.RED, file=sys.stderr)
            print_color("Please ensure 'config.ini' exists in the parent directory.", Colors.RED, file=sys.stderr)
            sys.exit(1)

        config = configparser.ConfigParser()
        config.read(config_file_path)
        lncli_path = config.get('paths', 'lncli_path')
        
        if not os.path.exists(lncli_path):
            print_color(f"lncli not found at: {lncli_path}", Colors.RED, file=sys.stderr)
            print_color("Please check 'lncli_path' in your 'config.ini'.", Colors.RED, file=sys.stderr)
            sys.exit(1)
            
        print_color(f"lncli path loaded: {lncli_path}", Colors.GREY)

    except (configparser.NoSectionError, configparser.NoOptionError) as e:
        print_color(f"Error reading config.ini: {e}", Colors.RED, file=sys.stderr)
        print_color("Ensure [paths] section and 'lncli_path' option exist in config.ini.", Colors.RED, file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print_color(f"An unexpected error occurred: {e}", Colors.RED, file=sys.stderr)
        sys.exit(1)


    # --- 3. Fetch UTXOs ---
    print_color("\n--- 1. Fetching UTXOs ---", Colors.CYAN, bold=True)
    command = [lncli_path, "listunspent", "--min_confs", "3"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, encoding='utf-8')
        data = json.loads(result.stdout)
        utxos = data.get("utxos", [])
        
        if not utxos:
            print_color("No UTXOs found. Is LND running and unlocked?", Colors.YELLOW)
            sys.exit(0)
            
        print_color(f"Found {len(utxos)} UTXOs.", Colors.GREEN)
        
    except subprocess.CalledProcessError as e:
        print_color(f"Error calling 'lncli listunspent':", Colors.RED, file=sys.stderr)
        print_color(e.stderr, Colors.GREY, file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError:
        print_color("Error parsing JSON output from lncli.", Colors.RED, file=sys.stderr)
        sys.exit(1)

    # --- 4. Select and Filter UTXOs ---
    print_color("\n--- 2. Select and Filter UTXOs ---", Colors.CYAN, bold=True)
    selected_utxos = []
    filter_desc = ""
    strategy = args.strategy
    strategy_desc = "N/A" # Default

    use_all = args.all or ask_yes_no("Do you want to use ALL UTXOs?")

    if use_all:
        selected_utxos = utxos
        filter_desc = "All UTXOs"
        strategy_desc = "N/A (All UTXOs selected)"
    else:
        # --- 4a. Set UTXO Selection Strategy (since we are not using all) ---
        if not strategy:
            while not strategy:
                print(f"{Colors.YELLOW}[?] Choose UTXO selection strategy:")
                print("  (a) Min to Max (Best for consolidation)")
                print("  (b) Max to Min (Most cost-effective)")
                print("  (c) Random (No specific order)")
                choice = input(f"Your choice (a/b/c): {Colors.ENDC}").strip().lower()
                
                if choice == 'a':
                    strategy = 'min-to-max'
                elif choice == 'b':
                    strategy = 'max-to-min'
                elif choice == 'c':
                    strategy = 'random'
                else:
                    print_color("Invalid choice. Please enter 'a', 'b', or 'c'.", Colors.RED)

        if strategy == 'min-to-max':
            utxos.sort(key=lambda u: int(u.get("amount_sat", 0)))
            strategy_desc = "Min to Max (Consolidation)"
        elif strategy == 'max-to-min':
            utxos.sort(key=lambda u: int(u.get("amount_sat", 0)), reverse=True)
            strategy_desc = "Max to Min (Cost-effective)"
        elif strategy == 'random':
            random.shuffle(utxos)
            strategy_desc = "Random"
        
        print_color(f"Using strategy: {strategy_desc}", Colors.GREY)

        # --- 4b. Select UTXOs based on target sum ---
        print("Set target sum for consolidation:")
        min_target_sum = args.min_amt if args.min_amt is not None else ask_for_number("Minimum *target sum* (0 for no minimum)", default=0)
        max_target_sum = args.max_amt if args.max_amt is not None else ask_for_number("Maximum *target sum* (0 for no maximum)", default=0)
        filter_desc = f"Min Sum: {min_target_sum:,} sats, Max Sum: {'Unlimited' if max_target_sum == 0 else f'{max_target_sum:,} sats'}"

        if max_target_sum == 0:
            max_target_sum = float('inf') # Set to infinity if 0 was entered

        selected_utxos = []
        current_sum = 0
        
        for utxo in utxos:
            selected_utxos.append(utxo)
            current_sum += int(utxo.get("amount_sat", 0))
            
            if current_sum >= min_target_sum:
                # We've met or exceeded the minimum target sum
                break
        
        # Now, validate the result
        if current_sum < min_target_sum:
            print_color(f"Error: Could not reach the minimum target sum of {min_target_sum:,} sats.", Colors.RED)
            all_utxos_sum = sum(int(u.get("amount_sat", 0)) for u in utxos)
            print_color(f"The strategy '{strategy_desc}' only found {all_utxos_sum:,} sats in total from all available UTXOs.", Colors.RED)
            sys.exit(1)
            
        if current_sum > max_target_sum:
            print_color(f"Error: The smallest combination using strategy '{strategy_desc}' exceeded the maximum target.", Colors.RED)
            print_color(f"Target: {min_target_sum:,} - {max_target_sum:,} sats. Result: {current_sum:,} sats.", Colors.RED)
            print_color("Try a different strategy or adjust your max target.", Colors.YELLOW)
            sys.exit(1)

    if not selected_utxos:
        print_color("No UTXOs matched your criteria. Exiting.", Colors.YELLOW)
        sys.exit(0)

    total_sats = sum(int(u.get("amount_sat", 0)) for u in selected_utxos)
    print_color(f"Selected {len(selected_utxos)} UTXOs totaling {total_sats:,} sats.", Colors.GREEN)


    # --- 6. Determine Fee Strategy ---
    print_color("\n--- 3. Setting Fee Strategy ---", Colors.CYAN, bold=True)
    fee_flag = ""

    if args.conf_target:
        fee_flag = f"--conf_target {args.conf_target}"
        print_color(f"Using fee strategy: {fee_flag}", Colors.GREY)
    elif args.sat_per_vbyte:
        fee_flag = f"--sat_per_vbyte {args.sat_per_vbyte}"
        print_color(f"Using fee strategy: {fee_flag}", Colors.GREY)
    else:
        while not fee_flag:
            choice = input(f"{Colors.YELLOW}[?] Choose fee strategy: (a) --conf_target, (b) --sat_per_vbyte: {Colors.ENDC}").strip().lower()
            if choice == 'a':
                target = ask_for_number("Enter confirmation target (number of blocks)")
                fee_flag = f"--conf_target {target}"
            elif choice == 'b':
                rate = ask_for_number("Enter fee rate (sats/vbyte)")
                fee_flag = f"--sat_per_vbyte {rate}"
            else:
                print_color("Invalid choice. Please enter 'a' or 'b'.", Colors.RED)


    # --- 7. Get Destination Address ---
    print_color("\n--- 4. Setting Destination Address ---", Colors.CYAN, bold=True)
    print_color("WARNING: Transactions are irreversible. Double-check your address.", Colors.RED, bold=True)
    
    address = args.address
    if address:
        print_color(f"Using address from argument: {address}", Colors.GREY)
    else:
        while True:
            addr1 = ask_for_string("Enter the destination Bitcoin address")
            addr2 = ask_for_string("Please RE-ENTER the address to confirm")
            if addr1 == addr2:
                address = addr1
                break
            print_color("Addresses do not match. Please try again.", Colors.RED)


    # --- 8. Formulate the Command ---
    print_color("\n--- 5. Generating Command ---", Colors.CYAN, bold=True)
    
    # Base command
    command_parts = [
        lncli_path,
        "sendcoins",
        "--sweepall",
        f"--addr {shlex.quote(address)}",
        fee_flag
    ]
    
    # Add UTXOs only if not using all (when using all, --sweepall handles it automatically)
    if not use_all:
        for utxo in selected_utxos:
            outpoint = utxo.get("outpoint")
            if outpoint:
                command_parts.append(f"--utxo {shlex.quote(outpoint)}")

    # Join with backslashes for readability
    full_command = " \\\n    ".join(command_parts)


    # --- 9. Review Your Selections ---
    print_color("\n--- 6. Review Your Selections ---", Colors.CYAN, bold=True)
    print_color("--------------------------------------------------------------------", Colors.HEADER)
    print_color(" Your Consolidation Plan:", Colors.HEADER, bold=True)
    print_color("--------------------------------------------------------------------", Colors.HEADER)
    print_color(f" Strategy:       {strategy_desc}", Colors.BLUE)
    print_color(f" Filter:         {filter_desc}", Colors.BLUE)
    print_color(f" Fee Strategy:   {fee_flag}", Colors.BLUE)
    print_color(f" Destination:    {address}", Colors.BLUE)
    print_color(f" UTXOs to send:  {len(selected_utxos)}", Colors.BLUE)
    print_color(f" Total Amount:   {total_sats:,} sats", Colors.BLUE)
    print_color("--------------------------------------------------------------------", Colors.HEADER)


    # --- 10. Final Output ---
    print_color("\n==================== FINAL COMMAND (FOR REVIEW) ====================", Colors.HEADER, bold=True)
    print_color(full_command, Colors.GREEN)
    print_color("====================================================================", Colors.HEADER, bold=True)
    print_color("\nThis script does NOT execute the command.", Colors.YELLOW, bold=True)
    print_color("Please review the command above. If it is correct, copy and paste it into your terminal to run it.", Colors.YELLOW)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print_color("\n\nOperation cancelled by user (Ctrl+C). Exiting.", Colors.RED)
        sys.exit(1)
