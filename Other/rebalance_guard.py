#!/usr/bin/env python3
"""
Rebalance Guard Script

Audits all active channels in LNDg to protect against unprofitable rebalancing feedback loops:
1. Channels offering inbound discounts (local_inbound_fee_rate < 0) are locked (ar_out_target = 100%)
   to prevent LNDg from using them as outbound rebalance donors while they are refilling.
2. Balanced channels (local_inbound_fee_rate >= 0 and local_balance_ratio >= threshold) that were
   previously locked are restored to their standard ar_out_target (e.g. 75%).

Usage:
    python3 rebalance_guard.py [--dry-run] [--debug]
"""

import os
import sys
import argparse
import logging
import json
import configparser
import requests
from prettytable import PrettyTable

# Get the path to the parent directory
parent_dir = os.path.dirname(os.path.abspath(__file__))
config_file_path = os.path.join(parent_dir, "..", "config.ini")
fee_config_file_path = os.path.join(parent_dir, "..", "feeConfig.json")
log_file_path = os.path.join(parent_dir, "..", "logs", "rebalance-guard.log")

# Setup logging
logging.basicConfig(
    filename=log_file_path,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


baseline_file_path = os.path.join(parent_dir, "..", "data", "rebalance_targets_baseline.json")


def load_config():
    config = configparser.ConfigParser()
    config.read(config_file_path)
    return config


def load_fee_config():
    if os.path.exists(fee_config_file_path):
        with open(fee_config_file_path, "r") as f:
            return json.load(f)
    return {}


def load_baseline_targets():
    """Load persistent channel baseline targets map."""
    if os.path.exists(baseline_file_path):
        try:
            with open(baseline_file_path, "r") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Error reading baseline targets file: {e}")
    return {}


def save_baseline_targets(baseline_map):
    """Save persistent channel baseline targets map."""
    try:
        os.makedirs(os.path.dirname(baseline_file_path), exist_ok=True)
        with open(baseline_file_path, "w") as f:
            json.dump(baseline_map, f, indent=4)
    except Exception as e:
        logging.error(f"Error saving baseline targets file: {e}")


def fetch_all_open_channels(config):
    """Fetch all open channels from LNDg API."""
    lndg_api_url = config["lndg"]["lndg_api_url"]
    username = config["credentials"]["lndg_username"]
    password = config["credentials"]["lndg_password"]
    api_url = f"{lndg_api_url}/api/channels/?limit=1500"

    response = requests.get(api_url, auth=(username, password), timeout=15)
    response.raise_for_status()
    data = response.json()
    results = data.get("results", [])
    return [c for c in results if c.get("is_open", False)]


def evaluate_channel_action(
    channel,
    lock_target=100,
    restore_target=75,
    restore_liquidity_threshold=75.0,
    lock_threshold=95,
    baseline_map=None
):
    """
    Evaluates whether a channel needs ar_out_target update.

    Returns:
        tuple: (action: str, new_target: int|None)
               action in ["LOCK", "RESTORE", "NOOP"]
    """
    chan_id = str(channel.get("chan_id", ""))
    inbound_fee = channel.get("local_inbound_fee_rate") or 0
    current_out_target = channel.get("ar_out_target")
    if current_out_target is None:
        current_out_target = 100

    capacity = channel.get("capacity", 0)
    local_balance = channel.get("local_balance", 0)
    local_ratio = (local_balance / capacity * 100.0) if capacity > 0 else 0.0

    # Rule 1: Inbound discount active -> lock out from rebalancing donor candidacy
    if inbound_fee < 0:
        if current_out_target < lock_threshold:
            # Capture current target as baseline before locking if not already captured
            if baseline_map is not None and chan_id and chan_id not in baseline_map:
                baseline_map[chan_id] = current_out_target
            return "LOCK", lock_target

    # Rule 2: Inbound discount removed & liquidity healthy -> restore baseline target
    elif inbound_fee >= 0:
        if current_out_target >= lock_threshold and local_ratio >= restore_liquidity_threshold:
            channel_restore_target = restore_target
            if baseline_map and chan_id in baseline_map:
                channel_restore_target = baseline_map[chan_id]

            if current_out_target != channel_restore_target:
                return "RESTORE", channel_restore_target

    return "NOOP", None


def audit_channel_rebalance_targets(
    channels,
    lock_target=100,
    restore_target=75,
    restore_liquidity_threshold=75.0,
    lock_threshold=95,
    baseline_map=None
):
    """
    Audits a list of open channels and generates update plans.

    Returns:
        list of dicts containing audit actions.
    """
    plans = []
    if baseline_map is None:
        baseline_map = {}

    for c in channels:
        action, new_target = evaluate_channel_action(
            c,
            lock_target=lock_target,
            restore_target=restore_target,
            restore_liquidity_threshold=restore_liquidity_threshold,
            lock_threshold=lock_threshold,
            baseline_map=baseline_map
        )
        if action != "NOOP":
            capacity = c.get("capacity", 0)
            local_balance = c.get("local_balance", 0)
            local_ratio = (local_balance / capacity * 100.0) if capacity > 0 else 0.0
            plans.append({
                "chan_id": str(c.get("chan_id")),
                "alias": c.get("alias", ""),
                "action": action,
                "old_target": c.get("ar_out_target", 100),
                "new_target": new_target,
                "inbound_fee": c.get("local_inbound_fee_rate", 0) or 0,
                "outbound_fee": c.get("local_fee_rate", 0) or 0,
                "local_ratio": local_ratio,
                "auto_fees": c.get("auto_fees", False),
            })
    return plans


def update_lndg_channel_target(chan_id, new_target, config, dry_run=False):
    """Update ar_out_target on LNDg via REST API."""
    if dry_run:
        logging.info(f"[DRY RUN] Would update channel {chan_id} ar_out_target to {new_target}")
        return True

    lndg_api_url = config["lndg"]["lndg_api_url"]
    username = config["credentials"]["lndg_username"]
    password = config["credentials"]["lndg_password"]
    url = f"{lndg_api_url}/api/channels/{chan_id}/"
    payload = {"chan_id": chan_id, "ar_out_target": new_target}

    try:
        response = requests.put(url, json=payload, auth=(username, password), timeout=10)
        response.raise_for_status()
        logging.info(f"Successfully updated channel {chan_id} ar_out_target to {new_target}")
        return True
    except Exception as e:
        logging.error(f"Failed to update channel {chan_id} target: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Audit and guard LNDg rebalancing targets.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without modifying LNDg")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logging")
    parser.add_argument("--lock-target", type=int, default=100, help="Target percentage when locked (default: 100)")
    parser.add_argument("--restore-target", type=int, default=75, help="Target percentage when restored (default: 75)")
    parser.add_argument("--restore-threshold", type=float, default=75.0, help="Min local liquidity % to restore target (default: 75.0)")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    config = load_config()
    fee_config = load_fee_config()
    inbound_protection = fee_config.get("inbound_protection", {})

    lock_target = inbound_protection.get("lock_target", args.lock_target)
    restore_target = inbound_protection.get("default_restored_ar_out_target", args.restore_target)
    restore_threshold = inbound_protection.get("restore_liquidity_threshold", args.restore_threshold)

    print("=" * 80)
    print(" 🛡️  LNDg Rebalance Guard")
    print(f" Mode: {'DRY RUN' if args.dry_run else 'LIVE EXECUTION'}")
    print(f" Lock Target: {lock_target}% | Restore Target: {restore_target}% | Restore Threshold: {restore_threshold}%")
    print("=" * 80)

    try:
        channels = fetch_all_open_channels(config)
        print(f"Fetched {len(channels)} open channels from LNDg.")
    except Exception as e:
        print(f"❌ Error fetching channels from LNDg: {e}")
        sys.exit(1)

    baseline_map = load_baseline_targets()

    plans = audit_channel_rebalance_targets(
        channels,
        lock_target=lock_target,
        restore_target=restore_target,
        restore_liquidity_threshold=restore_threshold,
        baseline_map=baseline_map
    )

    if not plans:
        print("✅ All channels are healthy! No target adjustments required.")
        return

    table = PrettyTable()
    table.field_names = [
        "Action",
        "Chan ID",
        "Alias",
        "Local %",
        "Out Fee",
        "In Fee",
        "Current oTarget",
        "New oTarget",
        "Managed By"
    ]

    for p in plans:
        managed = "LNDg af.py" if p["auto_fees"] else "fee_adjuster"
        action_str = f"🔒 {p['action']}" if p["action"] == "LOCK" else f"🔓 {p['action']}"
        table.add_row([
            action_str,
            p["chan_id"][:12] + "...",
            p["alias"][:18],
            f"{p['local_ratio']:.1f}%",
            f"{p['outbound_fee']} ppm",
            f"{p['inbound_fee']} ppm",
            f"{p['old_target']}%",
            f"{p['new_target']}%",
            managed
        ])

    print(table)
    print(f"\nTotal adjustments to apply: {len(plans)} channels")

    success_count = 0
    for p in plans:
        success = update_lndg_channel_target(
            p["chan_id"],
            p["new_target"],
            config,
            dry_run=args.dry_run
        )
        if success:
            success_count += 1

    if not args.dry_run:
        save_baseline_targets(baseline_map)
        print(f"✅ Successfully updated {success_count}/{len(plans)} channel targets in LNDg.")
    else:
        print(f"🔍 Dry run complete. {len(plans)} potential updates identified.")


if __name__ == "__main__":
    main()
