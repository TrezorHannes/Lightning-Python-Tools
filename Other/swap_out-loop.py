#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
swap_out-loop.py: Economically optimized Loop Out liquidity rebalancing via Lightning Labs Loop.

Features:
1. Discovers channel candidates with high local liquidity and low outbound fees via LNDg API.
2. Supports hybrid sizing: explicit --amt or dynamic per-channel equilibrium calculations.
3. Two-stage route validation: lncli queryroutes (graph fee evaluation) followed by
   prepay probing (lncli sendpayment with a fake hash) to prove live downstream liquidity.
4. Total economic cost scoring: Loop server fee + on-chain sweep fee + off-chain routing fee +
   foregone local routing revenue (opportunity cost ppm).
5. Interactive arrow-key CLI terminal menu (termios/tty) for intuitive candidate selection.
6. Execution via litloop / loop with minimum 6-block conf_target for economical sweeping.
7. Foreground litloop monitor streaming with safe Ctrl+C detachment.
8. Persistent accounting record store in SQLite (data/loop_out_history.db) with auto-synced CSV export.
"""

import os
import re
import sys
import json
import time
import datetime
import math
import binascii
import subprocess
import argparse
import configparser
import sqlite3
import csv
import logging
from typing import List, Dict, Any, Tuple, Optional, Union
import requests
from prettytable import PrettyTable

# Global Loop Constants
LOOP_PUBKEY_DEFAULT = "021c97a90a411ff2b10dc2a8e32de2f29d2fa49d41bfbb52bd416e460db0747d0d"
MIN_LOOP_OUT_SATS = 250_000
MAX_LOOP_OUT_SATS = 240_000_000
DEFAULT_CONF_TARGET = 9
MIN_CONF_TARGET = 6

# ANSI Color Codes
class Colors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    HIGHLIGHT = "\033[1;30;46m"  # Bold black on cyan background for menu cursor


def print_color(text: str, color_code: str = "", bold: bool = False):
    """Prints colorized text to stdout."""
    if bold:
        print(f"{color_code}{Colors.BOLD}{text}{Colors.ENDC}")
    else:
        print(f"{color_code}{text}{Colors.ENDC}")


def setup_logger(project_root: str) -> logging.Logger:
    """Sets up rotating file logger."""
    logger = logging.getLogger("swap_out_loop")
    logger.setLevel(logging.INFO)
    logs_dir = os.path.join(project_root, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_file = os.path.join(logs_dir, "swap_out-loop.log")

    if not logger.handlers:
        fh = logging.FileHandler(log_file)
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    return logger


def load_config() -> Tuple[configparser.ConfigParser, str]:
    """Loads config.ini from parent directory."""
    parent_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(parent_dir)
    config_file_path = os.path.join(project_root, "config.ini")

    config = configparser.ConfigParser()
    if os.path.exists(config_file_path):
        config.read(config_file_path)
    return config, project_root


def run_command(
    command_args: List[str],
    timeout: int = 60,
    expect_json: bool = False,
    dry_run: bool = False,
    dry_run_output: str = "",
) -> Tuple[bool, Any, Optional[str]]:
    """Runs a subprocess command securely with error handling."""
    if dry_run:
        if expect_json:
            return True, (dry_run_output if isinstance(dry_run_output, dict) else {}), None
        return True, dry_run_output, None

    try:
        process = subprocess.run(
            command_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
        stdout = process.stdout.strip()
        stderr = process.stderr.strip()

        if process.returncode != 0:
            err_msg = stderr or stdout or f"Command failed with code {process.returncode}"
            if expect_json and stdout:
                try:
                    data = json.loads(stdout)
                    return False, data, err_msg
                except json.JSONDecodeError:
                    pass
            return False, stdout, err_msg

        if expect_json:
            try:
                data = json.loads(stdout)
                return True, data, None
            except json.JSONDecodeError as jde:
                return False, stdout, f"JSON parse error: {jde}"

        return True, stdout, None

    except subprocess.TimeoutExpired:
        return False, None, f"Command timed out after {timeout}s: {' '.join(command_args)}"
    except Exception as e:
        return False, None, f"Execution error: {e}"


def get_config_val(config: Any, section: str, option: str, fallback: Any = "") -> Any:
    """Helper to safely read from ConfigParser or dict."""
    if isinstance(config, configparser.ConfigParser):
        return config.get(section, option, fallback=fallback)
    elif isinstance(config, dict):
        sec = config.get(section, {})
        if isinstance(sec, dict):
            return sec.get(option, fallback)
        return config.get(option, fallback)
    return fallback


def get_lnd_connection_params(config: Any) -> List[str]:
    """Extracts LND connection parameters from config for lncli."""
    params = []
    rpc = get_config_val(config, "lnd", "rpcserver", "").strip()
    tls = get_config_val(config, "lnd", "tlscertpath", "").strip()
    mac = get_config_val(config, "lnd", "macaroonpath", "").strip()
    if rpc:
        params.append(f"--rpcserver={rpc}")
    if tls:
        params.append(f"--tlscertpath={os.path.expanduser(tls)}")
    if mac:
        params.append(f"--macaroonpath={os.path.expanduser(mac)}")
    return params


def resolve_loop_command(config: configparser.ConfigParser) -> List[str]:
    """Resolves loop / litloop command line structure."""
    loop_cmd = get_config_val(config, "loop", "loop_command", "litloop").strip()

    # If litloop is installed or aliased
    if loop_cmd == "litloop":
        # Check if litloop binary exists in path
        if subprocess.run(["which", "litloop"], stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode == 0:
            return ["litloop"]
        # Check if loop is in PATH
        if subprocess.run(["which", "loop"], stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode == 0:
            # Fallback to loop binary with litd connection settings
            tlscert = os.path.expanduser("~/.lit/tls.cert")
            if os.path.exists(tlscert):
                return ["loop", "--rpcserver=localhost:8443", f"--tlscertpath={tlscert}"]
            return ["loop"]
        return ["litloop"]

    # If custom command specified
    return loop_cmd.split()


def fetch_channels_lndg(config: configparser.ConfigParser) -> List[Dict[str, Any]]:
    """Fetches open active channels from LNDg API."""
    if not config.has_section("lndg") or not config.has_section("credentials"):
        return []

    lndg_url = config.get("lndg", "lndg_api_url", fallback="http://localhost:8889").rstrip("/")
    username = config.get("credentials", "lndg_username", fallback="")
    password = config.get("credentials", "lndg_password", fallback="")

    api_url = f"{lndg_url}/api/channels?limit=1000&is_open=true&is_active=true"
    try:
        resp = requests.get(api_url, auth=(username, password), timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("results", [])
    except Exception as e:
        print_color(f"Warning: Failed to fetch channels from LNDg API: {e}", Colors.WARNING)
    return []


def filter_and_size_candidates(
    channels: List[Dict[str, Any]],
    target_amt: Optional[int] = None,
    min_capacity: int = 3_000_000,
    max_fee_rate: int = 100,
    min_local_ratio: float = 60.0,
    target_local_ratio: float = 50.0,
    blacklist: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Filters channels with high local liquidity & low outbound fee rates.
    Sizes proposed swap amount dynamically or against explicit target_amt.
    """
    if blacklist is None:
        blacklist = []

    candidates = []
    for ch in channels:
        if not ch.get("is_active", True) or not ch.get("is_open", True):
            continue

        pubkey = ch.get("remote_pubkey", "")
        if pubkey in blacklist:
            continue

        capacity = int(ch.get("capacity", 0))
        local_balance = int(ch.get("local_balance", 0))
        local_fee_rate = int(ch.get("local_fee_rate", 0))

        if capacity < min_capacity:
            continue
        if local_fee_rate > max_fee_rate:
            continue

        local_ratio = (local_balance / capacity) * 100 if capacity > 0 else 0
        if local_ratio < min_local_ratio:
            continue

        # Dynamic or fixed sizing
        reserve = max(100_000, int(capacity * 0.05))
        drainable_surplus = max(0, local_balance - reserve)

        if target_amt is not None and target_amt > 0:
            if drainable_surplus < MIN_LOOP_OUT_SATS:
                continue
            proposed_amt = min(target_amt, drainable_surplus)
        else:
            # Rebalance channel down to target equilibrium ratio (e.g. 50%)
            target_local_balance = int(capacity * (target_local_ratio / 100.0))
            proposed_amt = local_balance - target_local_balance

        # Clamp proposed amount to Loop limits
        if proposed_amt < MIN_LOOP_OUT_SATS:
            continue
        if proposed_amt > MAX_LOOP_OUT_SATS:
            proposed_amt = MAX_LOOP_OUT_SATS

        candidate = {
            "chan_id": str(ch.get("chan_id", "")),
            "alias": ch.get("alias", f"Node_{pubkey[:8]}"),
            "remote_pubkey": pubkey,
            "capacity": capacity,
            "local_balance": local_balance,
            "local_ratio": local_ratio,
            "local_fee_rate": local_fee_rate,
            "proposed_amt": proposed_amt,
            "drainable_surplus": drainable_surplus,
        }
        candidates.append(candidate)

    # Sort primarily by local_ratio descending, then local_fee_rate ascending
    candidates.sort(key=lambda x: (x["local_ratio"], -x["local_fee_rate"]), reverse=True)
    return candidates


def parse_loop_quote_output(output_text: str, amt: int) -> Dict[str, int]:
    """Parses output from `litloop quote out`."""
    quote = {
        "send_offchain": amt,
        "receive_onchain": amt,
        "estimated_onchain_fee": 0,
        "service_fee": 0,
        "total_loop_fee": 0,
    }

    for line in output_text.splitlines():
        line_clean = line.strip()
        if "Send off-chain:" in line_clean:
            val = line_clean.split(":")[1].replace("sat", "").strip()
            if val.isdigit():
                quote["send_offchain"] = int(val)
        elif "Receive on-chain:" in line_clean:
            val = line_clean.split(":")[1].replace("sat", "").strip()
            if val.isdigit():
                quote["receive_onchain"] = int(val)
        elif "Estimated on-chain fee:" in line_clean:
            val = line_clean.split(":")[1].replace("sat", "").strip()
            if val.isdigit():
                quote["estimated_onchain_fee"] = int(val)
        elif "Loop service fee:" in line_clean:
            val = line_clean.split(":")[1].replace("sat", "").strip()
            if val.isdigit():
                quote["service_fee"] = int(val)
        elif "Estimated total fee:" in line_clean:
            val = line_clean.split(":")[1].replace("sat", "").strip()
            if val.isdigit():
                quote["total_loop_fee"] = int(val)

    # If total_loop_fee was parsed but components were not (standard vs verbose output)
    if quote["total_loop_fee"] > 0 and quote["service_fee"] == 0 and quote["estimated_onchain_fee"] == 0:
        quote["service_fee"] = max(0, quote["total_loop_fee"] - 200)
        quote["estimated_onchain_fee"] = min(quote["total_loop_fee"], 200)
    elif quote["total_loop_fee"] == 0:
        quote["total_loop_fee"] = quote["service_fee"] + quote["estimated_onchain_fee"]

    return quote


def get_loop_quote(
    loop_cmd_parts: List[str],
    amt: int,
    conf_target: int = 9,
    dry_run: bool = False,
) -> Dict[str, int]:
    """Queries litloop quote out for cost estimates."""
    if dry_run:
        # Realistic simulation: 0.1% loop server fee + ~200 sat sweep fee
        service_fee = int(amt * 0.001)
        onchain_fee = 200
        return {
            "send_offchain": amt,
            "receive_onchain": amt - service_fee - onchain_fee,
            "estimated_onchain_fee": onchain_fee,
            "service_fee": service_fee,
            "total_loop_fee": service_fee + onchain_fee,
        }

    cmd = list(loop_cmd_parts) + ["quote", "out", "-v", "--conf_target", str(conf_target), str(amt)]
    success, output, _ = run_command(cmd, timeout=20)
    if success and isinstance(output, str):
        return parse_loop_quote_output(output, amt)

    # Fallback to standard quote if -v unsupported
    cmd_std = list(loop_cmd_parts) + ["quote", "out", "--conf_target", str(conf_target), str(amt)]
    succ_std, out_std, _ = run_command(cmd_std, timeout=20)
    if succ_std and isinstance(out_std, str):
        return parse_loop_quote_output(out_std, amt)

    # Default fallback estimate if offline
    service_fee = int(amt * 0.001)
    return {
        "send_offchain": amt,
        "receive_onchain": amt - service_fee - 200,
        "estimated_onchain_fee": 200,
        "service_fee": service_fee,
        "total_loop_fee": service_fee + 200,
    }


def query_route_to_loop(
    config: configparser.ConfigParser,
    dest_pubkey: str,
    amt: int,
    outgoing_chan_id: str,
    dry_run: bool = False,
) -> Tuple[bool, int, int]:
    """
    Stage 1: Checks route viability and off-chain routing fees via lncli queryroutes.
    Returns (success, routing_fee_sats, hops_count).
    """
    lncli_path = get_config_val(config, "paths", "lncli_path", "lncli")
    lnd_params = get_lnd_connection_params(config)

    cmd = [
        lncli_path,
        *lnd_params,
        "queryroutes",
        "--dest",
        dest_pubkey,
        "--amt",
        str(amt),
        "--outgoing_chan_id",
        str(outgoing_chan_id),
    ]

    success, output, _ = run_command(
        cmd,
        timeout=25,
        expect_json=True,
        dry_run=dry_run,
        dry_run_output={"routes": [{"total_fees": "40", "hops": [{"chan_id": outgoing_chan_id}]}]},
    )

    if success and isinstance(output, dict) and output.get("routes"):
        best_route = output["routes"][0]
        routing_fee = int(best_route.get("total_fees", 0))
        hops_count = len(best_route.get("hops", []))
        return True, routing_fee, hops_count

    return False, 0, 0


def probe_direct_route_to_loop(
    config: Any,
    remote_pubkey: str,
    dest_pubkey: str,
    amt: int,
    timeout: int = 15,
) -> Tuple[bool, int, int, Optional[str]]:
    """
    Attempts to probe direct 2-hop route: Local -> Peer -> Loop.
    Uses lncli buildroute and lncli sendtoroute with a fake payment hash.
    Returns (success, verified_fee, hops_count, error_msg).
    """
    if not remote_pubkey:
        return False, 0, 0, "No remote pubkey provided"

    lncli_path = get_config_val(config, "paths", "lncli_path", "lncli")
    lnd_params = get_lnd_connection_params(config)

    # 1. Build direct route
    build_cmd = [
        lncli_path,
        *lnd_params,
        "buildroute",
        "--amt",
        str(amt),
        "--hops",
        f"{remote_pubkey},{dest_pubkey}",
    ]
    succ_build, out_build, err_build = run_command(build_cmd, timeout=timeout, expect_json=True)
    if not succ_build or not isinstance(out_build, dict) or "route" not in out_build:
        return False, 0, 0, err_build or "Direct channel to Loop not available or buildroute failed"

    route = out_build["route"]
    direct_fees = int(route.get("total_fees", 0))
    hops_count = len(route.get("hops", []))

    # 2. Probe direct route using sendtoroute with fake payment hash
    fake_payment_hash = binascii.hexlify(os.urandom(32)).decode()
    probe_cmd = [
        lncli_path,
        *lnd_params,
        "sendtoroute",
        f"--payment_hash={fake_payment_hash}",
        "-",
    ]
    try:
        process = subprocess.run(
            probe_cmd,
            input=json.dumps(out_build),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        if process.stdout:
            out_probe = json.loads(process.stdout)
            failure_code = out_probe.get("failure", {}).get("code", "")
            if failure_code in ["INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS", "INCORRECT_PAYMENT_DETAILS"]:
                return True, direct_fees, hops_count, None
            for htlc in out_probe.get("htlcs", []):
                code = htlc.get("failure", {}).get("code", "")
                if code in ["INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS", "INCORRECT_PAYMENT_DETAILS"]:
                    return True, direct_fees, hops_count, None
            err = out_probe.get("failure", {}).get("code") or "Direct route lacked liquidity"
            return False, 0, 0, err
    except Exception as e:
        return False, 0, 0, str(e)

    return False, 0, 0, "Failed to verify direct route"


def send_prepay_probe(
    config: Any,
    dest_pubkey: str,
    amt: int,
    outgoing_chan_id: str,
    remote_pubkey: str = "",
    timeout: int = 20,
    skip_probe: bool = False,
) -> Tuple[bool, int, int, Optional[str]]:
    """
    Stage 2: Live route probe using a random 32-byte fake hash.
    Proves downstream liquidity without risking funds.
    Returns (success, verified_routing_fee, hops_count, error_detail).
    """
    if skip_probe:
        return True, 0, 0, None

    lncli_path = get_config_val(config, "paths", "lncli_path", "lncli")
    lnd_params = get_lnd_connection_params(config)
    fake_payment_hash = binascii.hexlify(os.urandom(32)).decode()

    cmd = [
        lncli_path,
        *lnd_params,
        "sendpayment",
        "--dest",
        dest_pubkey,
        "--amt",
        str(amt),
        "--payment_hash",
        fake_payment_hash,
        "--outgoing_chan_id",
        str(outgoing_chan_id),
        "--timeout",
        f"{timeout}s",
        "--json",
    ]

    _, output, _ = run_command(cmd, timeout=timeout + 10, expect_json=True)

    if isinstance(output, dict):
        # 1. Check if any attempt reached Loop destination
        htlcs = output.get("htlcs", [])
        for htlc in htlcs:
            failure = htlc.get("failure", {})
            code = failure.get("code", "")
            if code in ["INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS", "INCORRECT_PAYMENT_DETAILS"]:
                route = htlc.get("route", {})
                verified_fee = int(route.get("total_fees", 0))
                hops_count = len(route.get("hops", []))
                return True, verified_fee, hops_count, None

        # Check top-level failure reason
        failure_reason = output.get("failure_reason", "")
        payment_error = output.get("payment_error", "")
        acceptable_signals = [
            "INCORRECT_PAYMENT_DETAILS",
            "INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS",
            "FAILURE_REASON_INCORRECT_PAYMENT_DETAILS",
        ]
        if any(sig in failure_reason for sig in acceptable_signals) or any(
            sig in payment_error for sig in acceptable_signals
        ):
            if htlcs:
                last_route = htlcs[-1].get("route", {})
                verified_fee = int(last_route.get("total_fees", 0))
                hops_count = len(last_route.get("hops", []))
                return True, verified_fee, hops_count, None
            return True, 0, 0, None

    # 2. Multi-hop probe failed. If peer has a direct channel to Loop, probe the direct channel!
    if remote_pubkey:
        succ_dir, fee_dir, hops_dir, err_dir = probe_direct_route_to_loop(
            config, remote_pubkey, dest_pubkey, amt, timeout=timeout
        )
        if succ_dir:
            return True, fee_dir, hops_dir, None

    err_detail = "Insufficient liquidity on route to Loop"
    if isinstance(output, dict):
        err_detail = output.get("failure_reason") or output.get("payment_error") or err_detail
    return False, 0, 0, err_detail


def calculate_economic_cost(
    amt: int,
    service_fee: int,
    onchain_fee: int,
    routing_fee: int,
    local_fee_rate: int,
) -> Dict[str, Any]:
    """Computes full economic breakdown including opportunity cost."""
    # Opportunity cost: lost potential routing fee from draining local liquidity
    opportunity_cost = round((amt * local_fee_rate) / 1_000_000)
    total_cost = service_fee + onchain_fee + routing_fee + opportunity_cost
    effective_ppm = round((total_cost / amt) * 1_000_000) if amt > 0 else 0

    return {
        "server_fee": service_fee,
        "onchain_fee": onchain_fee,
        "routing_fee": routing_fee,
        "opportunity_cost": opportunity_cost,
        "total_cost": total_cost,
        "effective_ppm": effective_ppm,
    }


def get_loop_db_path(config: Any) -> Optional[str]:
    """
    Returns the configured or discovered path to Loop's SQLite database (Source of Truth).
    Checks [loop] -> loop_db_path and [paths] -> loop_db_path in config.ini,
    falling back to default ~/.loop/mainnet/loop_sqlite.db.
    """
    db_path = ""
    if hasattr(config, "get"):
        if config.has_section("loop"):
            db_path = config.get("loop", "loop_db_path", fallback="")
        if not db_path and config.has_section("paths"):
            db_path = config.get("paths", "loop_db_path", fallback="")

    if db_path:
        expanded = os.path.expanduser(db_path.strip())
        if os.path.exists(expanded):
            return expanded
        return expanded

    default_path = os.path.expanduser("~/.loop/mainnet/loop_sqlite.db")
    if os.path.exists(default_path):
        return default_path

    return None


LOOP_STATE_MAP = {
    0: "INITIATED",
    1: "HTLC_PUBLISHED",
    2: "SUCCESS",
    3: "FAILED",
    4: "FAILED",
    5: "INVOICE_SETTLED",
    6: "SUCCESS",
    7: "FAILED",
    8: "HTLC_PUBLISHED",
    9: "PREIMAGE_REVEALED",
}


def fetch_loop_history_from_db(db_path: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Reads Loop Out swaps directly from Loop's SQLite database (Source of Truth)."""
    if not os.path.exists(db_path):
        return []

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sweeps';")
    has_sweeps = cur.fetchone() is not None

    if has_sweeps:
        query = """
        SELECT 
            hex(s.swap_hash) AS swap_id,
            s.initiation_time,
            s.amount_requested AS amount,
            s.label,
            lo.outgoing_chan_set,
            lo.dest_address,
            su.update_state,
            COALESCE(su.server_cost, 0) AS server_cost,
            COALESCE(su.onchain_cost, 0) AS onchain_cost,
            COALESCE(su.offchain_cost, 0) AS offchain_cost,
            sw.completed AS sweep_completed,
            sw.amt AS sweep_amt,
            sb.batch_tx_id
        FROM loopout_swaps lo
        JOIN swaps s ON lo.swap_hash = s.swap_hash
        LEFT JOIN (
            SELECT swap_hash, update_state, server_cost, onchain_cost, offchain_cost,
                   ROW_NUMBER() OVER (PARTITION BY swap_hash ORDER BY update_timestamp DESC) as rn
            FROM swap_updates
        ) su ON lo.swap_hash = su.swap_hash AND su.rn = 1
        LEFT JOIN sweeps sw ON lo.swap_hash = sw.swap_hash
        LEFT JOIN sweep_batches sb ON sw.batch_id = sb.id
        ORDER BY s.initiation_time DESC
        LIMIT ?;
        """
    else:
        query = """
        SELECT 
            hex(s.swap_hash) AS swap_id,
            s.initiation_time,
            s.amount_requested AS amount,
            s.label,
            lo.outgoing_chan_set,
            lo.dest_address,
            su.update_state,
            COALESCE(su.server_cost, 0) AS server_cost,
            COALESCE(su.onchain_cost, 0) AS onchain_cost,
            COALESCE(su.offchain_cost, 0) AS offchain_cost,
            NULL AS sweep_completed,
            NULL AS sweep_amt,
            NULL AS batch_tx_id
        FROM loopout_swaps lo
        JOIN swaps s ON lo.swap_hash = s.swap_hash
        LEFT JOIN (
            SELECT swap_hash, update_state, server_cost, onchain_cost, offchain_cost,
                   ROW_NUMBER() OVER (PARTITION BY swap_hash ORDER BY update_timestamp DESC) as rn
            FROM swap_updates
        ) su ON lo.swap_hash = su.swap_hash AND su.rn = 1
        ORDER BY s.initiation_time DESC
        LIMIT ?;
        """
    cur.execute(query, (limit,))
    rows = []
    for r in cur.fetchall():
        server_fee = int(r["server_cost"])
        onchain_fee = int(r["onchain_cost"])
        routing_fee = int(r["offchain_cost"])
        amt = int(r["amount"])

        sweep_completed = r["sweep_completed"]
        batch_tx_id = r["batch_tx_id"]
        sweep_amt = int(r["sweep_amt"]) if r["sweep_amt"] is not None else 0

        # If onchain fee is 0 in swap_updates but a sweep batch tx exists,
        # estimate/calculate the pending onchain fee from the sweep output
        if onchain_fee == 0 and batch_tx_id and sweep_amt > 0 and amt >= sweep_amt:
            onchain_fee = amt - sweep_amt

        total_cost = server_fee + onchain_fee + routing_fee
        ppm = int((total_cost * 1_000_000) / amt) if amt > 0 else 0

        state_code = r["update_state"]
        if state_code == 1 and batch_tx_id and (sweep_completed == 0 or sweep_completed is False):
            state_str = "PREIMAGE_REVEALED"
        else:
            state_str = LOOP_STATE_MAP.get(state_code, f"STATE_{state_code}" if state_code is not None else "INITIATED")

        raw_time = str(r["initiation_time"])
        formatted_time = raw_time.split(".")[0].replace(" +0000 UTC", "")

        rows.append({
            "swap_id": r["swap_id"].lower(),
            "initiation_time": formatted_time,
            "amount": amt,
            "label": r["label"] or "",
            "outgoing_chan_set": r["outgoing_chan_set"] or "",
            "server_fee": server_fee,
            "onchain_fee": onchain_fee,
            "routing_fee": routing_fee,
            "total_cost": total_cost,
            "effective_ppm": ppm,
            "status": state_str,
        })
    conn.close()
    return rows


def fetch_loop_history_from_cli(config: Any, limit: int = 50) -> List[Dict[str, Any]]:
    """Queries loop listswaps via RPC CLI as fallback."""
    loop_cmd = resolve_loop_command(config)
    cmd = list(loop_cmd) + ["listswaps"]
    succ, out, _ = run_command(cmd, timeout=30, expect_json=True)
    if not succ or not isinstance(out, dict) or "swaps" not in out:
        return []

    raw_swaps = [s for s in out.get("swaps", []) if s.get("type") == "LOOP_OUT"]
    raw_swaps.sort(key=lambda s: int(s.get("initiation_time", 0)), reverse=True)

    rows = []
    for s in raw_swaps[:limit]:
        amt = int(s.get("amt", 0))
        server_fee = int(s.get("cost_server", 0))
        onchain_fee = int(s.get("cost_onchain", 0))
        routing_fee = int(s.get("cost_offchain", 0))
        total_cost = server_fee + onchain_fee + routing_fee
        ppm = int((total_cost * 1_000_000) / amt) if amt > 0 else 0

        ns_time = int(s.get("initiation_time", 0))
        if ns_time > 0:
            formatted_time = datetime.datetime.fromtimestamp(ns_time / 1e9, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        else:
            formatted_time = "UNKNOWN"

        chan_set = s.get("outgoing_chan_set", [])
        chan_str = ",".join(str(c) for c in chan_set) if isinstance(chan_set, list) else str(chan_set)

        rows.append({
            "swap_id": s.get("id", "").lower(),
            "initiation_time": formatted_time,
            "amount": amt,
            "label": s.get("label", ""),
            "outgoing_chan_set": chan_str,
            "server_fee": server_fee,
            "onchain_fee": onchain_fee,
            "routing_fee": routing_fee,
            "total_cost": total_cost,
            "effective_ppm": ppm,
            "status": s.get("state", "UNKNOWN"),
        })
    return rows


def fetch_loop_history(config: Any, limit: int = 50) -> Tuple[List[Dict[str, Any]], str]:
    """
    Fetches Loop Out history. First tries direct SQLite read from the configured
    or discovered loop_db_path (Source of Truth). If unavailable, falls back to RPC CLI (listswaps).
    Returns (swaps_list, source_description).
    """
    db_path = get_loop_db_path(config)
    if db_path and os.path.exists(db_path):
        try:
            swaps = fetch_loop_history_from_db(db_path, limit=limit)
            return swaps, f"Loop SQLite DB SOT ({db_path})"
        except Exception:
            pass

    swaps = fetch_loop_history_from_cli(config, limit=limit)
    return swaps, "Loop Daemon RPC (listswaps)"


def export_history_to_csv(swaps: List[Dict[str, Any]], csv_path: str) -> None:
    """Exports swap history records to CSV file."""
    if not swaps:
        return
    os.makedirs(os.path.dirname(os.path.abspath(csv_path)), exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(swaps[0].keys())
        for s in swaps:
            writer.writerow(list(s.values()))


def read_single_keypress() -> str:
    """Reads a single keypress from standard input in raw mode."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":  # Escape sequence
            ch2 = sys.stdin.read(1)
            if ch2 == "[":
                ch3 = sys.stdin.read(1)
                return f"\x1b[{ch3}"
            return "\x1b"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def print_candidates_table(candidates: List[Dict[str, Any]]) -> None:
    """Prints a formatted summary table of candidates with fee and cost breakdown."""
    table = PrettyTable()
    table.field_names = [
        "#",
        "Alias",
        "Channel ID",
        "Local %",
        "Swap Size",
        "Loop Fee",
        "Sweep Fee",
        "Route Fee",
        "Opp Cost",
        "Total Cost",
        "Net PPM",
    ]
    table.align = "r"
    table.align["Alias"] = "l"
    table.align["#"] = "c"
    for idx, c in enumerate(candidates, 1):
        table.add_row([
            f"[{idx}]",
            c["alias"][:20],
            c["chan_id"],
            f"{c['local_ratio']:.1f}%",
            f"{c['proposed_amt']:,}",
            f"{c['server_fee']:,}",
            f"{c['onchain_fee']:,}",
            f"{c['routing_fee']:,}",
            f"{c['opportunity_cost']:,}",
            f"{c['total_cost']:,}",
            f"{c['effective_ppm']:,}",
        ])
    print(table)


def interactive_menu_select(
    candidates: List[Dict[str, Any]],
    target_amt: Optional[int] = None,
    max_channels: int = 3,
) -> Optional[Union[Dict[str, Any], List[Dict[str, Any]]]]:
    """
    Renders an interactive CLI terminal menu with arrow-key navigation,
    Spacebar multi-select toggling, and greedy batching fallback.
    """
    if not candidates:
        return None

    if not sys.stdin.isatty():
        # If target_amt is specified and exceeds first candidate's drainable surplus,
        # greedily pool top candidates to meet target_amt up to max_channels.
        first_cand_capacity = candidates[0].get("drainable_surplus", candidates[0]["proposed_amt"])
        if target_amt is not None and target_amt > first_cand_capacity:
            batch = []
            accumulated = 0
            for c in candidates:
                batch.append(c)
                accumulated += c.get("drainable_surplus", c.get("proposed_amt", 0))
                if accumulated >= target_amt or len(batch) >= max_channels:
                    break
            if len(batch) > 1:
                return batch

        # Non-interactive fallback
        print("\nAvailable Candidates:")
        print_candidates_table(candidates)
        try:
            prompt_msg = f"\nSelect candidate [1-{len(candidates)}] (comma-separated for multi) or [q] to cancel: "
            choice = input(prompt_msg).strip().lower()
            if not choice or choice == "q":
                return None
            if "," in choice:
                indices = [int(x.strip()) - 1 for x in choice.split(",") if x.strip().isdigit()]
                valid = [candidates[i] for i in indices if 0 <= i < len(candidates)]
                return valid if valid else None
            if choice.isdigit() and 1 <= int(choice) <= len(candidates):
                return candidates[int(choice) - 1]
        except (EOFError, KeyboardInterrupt):
            pass
        return None

    current_idx = 0
    total = len(candidates)
    selected_indices = set()

    while True:
        # Clear screen segment and render
        print("[2J[H", end="")  # Clear screen and move to top-left
        print_color("=== Lightning Loop Out - Economic Channel Selection ===", Colors.HEADER, bold=True)
        print_color(
            "Use [↑/k] & [↓/j] to navigate, [Space] to toggle multi-channel, [a] toggle all, [Enter] to select, [q] to cancel.\n",
            Colors.OKCYAN,
        )

        table = PrettyTable()
        table.field_names = [
            "Sel",
            "Alias",
            "Channel ID",
            "Local %",
            "Swap Size",
            "Loop Fee",
            "Sweep Fee",
            "Route Fee",
            "Opp Cost",
            "Total Cost",
            "Net PPM",
        ]
        table.align = "r"
        table.align["Alias"] = "l"
        table.align["Sel"] = "c"

        for idx, c in enumerate(candidates):
            is_active_cursor = idx == current_idx
            is_checked = idx in selected_indices

            cursor_mark = "▶" if is_active_cursor else " "
            check_mark = "[✓]" if is_checked else "[ ]"
            sel_display = f"{cursor_mark} {check_mark}"

            row = [
                sel_display,
                c["alias"][:20],
                c["chan_id"],
                f"{c['local_ratio']:.1f}%",
                f"{c['proposed_amt']:,}",
                f"{c['server_fee']:,}",
                f"{c['onchain_fee']:,}",
                f"{c['routing_fee']:,}",
                f"{c['opportunity_cost']:,}",
                f"{c['total_cost']:,}",
                f"{c['effective_ppm']:,}",
            ]

            if is_active_cursor:
                # Highlight active row in color
                row = [f"{Colors.OKGREEN}{Colors.BOLD}{val}{Colors.ENDC}" for val in row]
            elif is_checked:
                row = [f"{Colors.OKCYAN}{val}{Colors.ENDC}" for val in row]
            table.add_row(row)

        print(table)
        print()

        if selected_indices:
            sel_list = [candidates[i] for i in sorted(selected_indices)]
            if target_amt and target_amt > 0:
                swap_size = target_amt
            else:
                swap_size = min(c["proposed_amt"] for c in sel_list)

            ppms = [c["effective_ppm"] for c in sel_list]
            costs = [c["total_cost"] for c in sel_list]
            min_ppm, max_ppm = min(ppms), max(ppms)
            min_cost, max_cost = min(costs), max(costs)
            ppm_str = f"{min_ppm:,} ppm" if min_ppm == max_ppm else f"{min_ppm:,} – {max_ppm:,} ppm"
            cost_str = f"{min_cost:,} sats" if min_cost == max_cost else f"{min_cost:,} – {max_cost:,} sats"

            print_color(
                f"Multi-Select Active ({len(selected_indices)} chans): Fixed Swap Size {swap_size:,} sats | Net PPM: {ppm_str} (Est Cost: {cost_str})",
                Colors.OKGREEN,
                bold=True,
            )
            print_color("Press [Enter] to execute multi-channel swap with selected channels.", Colors.OKBLUE)
        else:
            selected_cand = candidates[current_idx]
            print_color(
                f"Active: {selected_cand['alias']} | Swap: {selected_cand['proposed_amt']:,} sats | Total Cost: {selected_cand['total_cost']:,} sats ({selected_cand['effective_ppm']} ppm)",
                Colors.OKBLUE,
                bold=True,
            )

        key = read_single_keypress()
        if key in ("[A", "k", "K"):  # Up
            current_idx = (current_idx - 1) % total
        elif key in ("[B", "j", "J"):  # Down
            current_idx = (current_idx + 1) % total
        elif key == " ":  # Space toggles selection
            if current_idx in selected_indices:
                selected_indices.remove(current_idx)
            else:
                if len(selected_indices) < max_channels:
                    selected_indices.add(current_idx)
                else:
                    print_color(f"Max channels ({max_channels}) reached!", Colors.WARNING)
                    time.sleep(0.3)
        elif key in ("a", "A"):  # Toggle all up to max_channels
            if len(selected_indices) == min(total, max_channels):
                selected_indices.clear()
            else:
                selected_indices = set(range(min(total, max_channels)))
        elif key in ("\r", "\n"):  # Enter to confirm
            if selected_indices:
                return [candidates[i] for i in sorted(selected_indices)]
            return candidates[current_idx]
        elif key in ("q", "Q", "\x1b"):  # Quit or Escape
            print_color("\nLoop Out selection cancelled.", Colors.WARNING)
            return None



def calculate_max_routing_fee_budget(
    probed_routing_fee: int,
    config: Any,
    explicit_max_routing_fee: Optional[int] = None,
    explicit_leeway_pct: Optional[float] = None,
) -> int:
    """
    Calculates the max off-chain routing fee budget to pass to loop out.
    - If explicit_max_routing_fee is 0, returns 0 (unbounded / loop daemon default).
    - If explicit_max_routing_fee > 0, returns that exact satoshi cap.
    - Otherwise applies percentage leeway (default 100% / 2.0x) plus base satoshi buffer (default 500).
    """
    if explicit_max_routing_fee is not None and explicit_max_routing_fee == 0:
        return 0
    if explicit_max_routing_fee is not None and explicit_max_routing_fee > 0:
        return explicit_max_routing_fee

    leeway_pct = (
        explicit_leeway_pct
        if explicit_leeway_pct is not None
        else (
            config.getfloat("loop", "fee_leeway_pct", fallback=100.0)
            if hasattr(config, "getfloat")
            else 100.0
        )
    )
    base_buffer = (
        config.getint("loop", "fee_leeway_base_sats", fallback=500)
        if hasattr(config, "getint")
        else 500
    )
    multiplier = 1.0 + (max(0.0, leeway_pct) / 100.0)
    return int(probed_routing_fee * multiplier) + base_buffer


def execute_loop_out(
    config: Any,
    channel_id: Union[str, List[str]],
    amt: int,
    conf_target: int = 9,
    max_routing_fee: int = 0,
    dest_addr: Optional[str] = None,
    alias: str = "",
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Executes litloop out / loop out with specified channel(s), amount, conf_target, and fees.
    channel_id can be a single short channel ID string or a list of channel IDs.
    """
    if isinstance(channel_id, list):
        chan_arg = ",".join(str(c) for c in channel_id)
        chan_label = f"{len(channel_id)} chans"
    else:
        chan_arg = str(channel_id)
        chan_label = chan_arg

    if alias:
        if chan_label in alias or f"({chan_label})" in alias:
            label = f"Loop-Out: {alias}"
        else:
            label = f"Loop-Out: {alias} ({chan_label})"
    else:
        label = f"Loop-Out: {chan_label}"
    if dry_run:
        fake_swap_id = "dry-run-swap-" + binascii.hexlify(os.urandom(16)).decode()
        fee_arg = f" --max_swap_routing_fee {max_routing_fee}" if max_routing_fee > 0 else ""
        print_color(
            f'  Command: litloop out --amt {amt} --channel {chan_arg} --conf_target {conf_target} --label "{label}" --force{fee_arg}',
            Colors.WARNING,
        )
        return {"success": True, "swap_id": fake_swap_id, "dry_run": True}

    loop_cmd = resolve_loop_command(config)
    cmd = list(loop_cmd) + [
        "out",
        "--amt",
        str(amt),
        "--channel",
        chan_arg,
        "--conf_target",
        str(conf_target),
        "--label",
        label,
        "--force",
    ]

    if max_routing_fee > 0:
        cmd.extend(["--max_swap_routing_fee", str(max_routing_fee)])
    if dest_addr:
        cmd.extend(["--addr", dest_addr])

    print_color(f"\nInitiating Loop Out: {' '.join(cmd)}", Colors.OKBLUE, bold=True)
    success, output, err = run_command(cmd, timeout=120)

    if not success or not output:
        print_color(f"Failed to initiate loop out: {err}", Colors.FAIL, bold=True)
        return {"success": False, "error": err, "dry_run": False}

    # Extract swap ID from output if available
    swap_id = "unknown_swap_id"
    for line in output.splitlines():
        if "Swap initiated" in line or "Swap ID" in line or "swap hash" in line.lower():
            parts = line.split()
            if len(parts) > 1:
                swap_id = parts[-1].strip(".:,")

    print_color("✓ Swap Initiated Successfully!", Colors.OKGREEN, bold=True)
    print_color(output, Colors.OKCYAN)
    return {"success": True, "swap_id": swap_id, "output": output, "dry_run": False}


def evaluate_single_candidate(
    c: Dict[str, Any],
    config: Any,
    loop_cmd: List[str],
    loop_pubkey: str,
    conf_target: int,
    probe_timeout: int,
    skip_prepay_probe: bool,
    print_lock: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    """Evaluates a single candidate channel by querying quote, route, and prepay probe."""
    chan_id = c["chan_id"]
    amt = c["proposed_amt"]
    alias = c["alias"]

    def log(msg: str):
        if print_lock:
            with print_lock:
                print(msg)
        else:
            print(msg)

    log(f"  → Checking {alias[:20]} ({chan_id}) for {amt:,} sats...")

    # 1. Fetch Loop Quote (read-only query)
    quote = get_loop_quote(loop_cmd, amt, conf_target=conf_target, dry_run=False)

    # 2. Stage 1: queryroutes (read-only query)
    route_ok, route_fee, hops = query_route_to_loop(config, loop_pubkey, amt, chan_id, dry_run=False)
    if not route_ok:
        log(f"    ✗ Queryroutes found no route to Loop node for {alias[:20]}.")
        return None

    # 3. Stage 2: Prepay probe with fake hash (proves actual live liquidity without spending funds)
    probe_ok, verified_fee, verified_hops, probe_err = send_prepay_probe(
        config=config,
        dest_pubkey=loop_pubkey,
        amt=amt,
        outgoing_chan_id=chan_id,
        remote_pubkey=c.get("remote_pubkey", ""),
        timeout=probe_timeout,
        skip_probe=skip_prepay_probe,
    )
    if not probe_ok:
        log(f"    ✗ Prepay probe failed for {alias[:20]}: {probe_err}")
        return None

    actual_fee = verified_fee if verified_fee > 0 or skip_prepay_probe else route_fee
    actual_hops = verified_hops if verified_hops > 0 or skip_prepay_probe else hops
    log(f"    ✓ Route verified with live liquidity for {alias[:20]} ({actual_hops} hops, {actual_fee:,} sat routing fee).")

    # 4. Economic cost breakdown
    econ = calculate_economic_cost(
        amt=amt,
        service_fee=quote["service_fee"],
        onchain_fee=quote["estimated_onchain_fee"],
        routing_fee=actual_fee,
        local_fee_rate=c["local_fee_rate"],
    )

    c_eval = dict(c)
    c_eval.update(quote)
    c_eval.update(econ)
    return c_eval


def monitor_loop(config: configparser.ConfigParser):
    """Streams litloop monitor to terminal with detachment instructions."""
    loop_cmd = resolve_loop_command(config)
    cmd = list(loop_cmd) + ["monitor"]

    print_color("\n" + "=" * 70, Colors.OKCYAN)
    print_color("Streaming Loop Monitor (Real-Time Swaps Progress):", Colors.OKCYAN, bold=True)
    print_color("NOTE: Sweeping requires ~6-9 on-chain block confirmations (~30-60+ min).", Colors.WARNING)
    print_color("You can safely press [Ctrl+C] to detach anytime; loopd runs in the background.", Colors.OKGREEN)
    print_color("=" * 70 + "\n", Colors.OKCYAN)

    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        print_color("\nDetached from Loop Monitor. Background loopd will continue processing.", Colors.OKGREEN, bold=True)


def parse_arguments() -> argparse.Namespace:
    """Configures CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Economically optimized Loop Out liquidity rebalancing via litloop."
    )
    parser.add_argument(
        "--amt",
        type=int,
        default=None,
        help=f"Specific amount to loop out in satoshis ({MIN_LOOP_OUT_SATS:,} to {MAX_LOOP_OUT_SATS:,}). If omitted, dynamically calculated per channel.",
    )
    parser.add_argument(
        "-c",
        "--capacity",
        type=int,
        default=None,
        help="Minimum channel capacity in satoshis (default from config or 3,000,000).",
    )
    parser.add_argument(
        "-f",
        "--fee-limit",
        type=int,
        default=None,
        help="Maximum local fee rate in ppm to consider for looping out (default from config or 100).",
    )
    parser.add_argument(
        "-r",
        "--min-ratio",
        type=float,
        default=None,
        help="Minimum local liquidity ratio percentage to qualify as candidate (default 60%%).",
    )
    parser.add_argument(
        "--conf-target",
        type=int,
        default=None,
        help="Confirmation target in blocks for on-chain sweep transaction (minimum 6, default 9).",
    )
    parser.add_argument(
        "--max-routing-fee",
        type=int,
        default=None,
        help="Maximum off-chain swap routing fee in satoshis. If 0, no fee limit is enforced (uses loop daemon default).",
    )
    parser.add_argument(
        "--fee-leeway-pct",
        type=float,
        default=None,
        help="Percentage leeway added on top of probed routing fee for max off-chain fee limit (default from config or 100%%).",
    )
    parser.add_argument(
        "--dest-addr",
        type=str,
        default=None,
        help="Custom on-chain Bitcoin address for swept funds (defaults to LND internal wallet).",
    )
    parser.add_argument(
        "--max-channels",
        type=int,
        default=None,
        help="Maximum number of outgoing channels to batch in a multi-channel Loop Out (default from config or 3).",
    )
    parser.add_argument(
        "--channel",
        "--channels",
        type=str,
        default=None,
        help="Optional comma-separated list of short channel IDs to target directly.",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=None,
        help="Number of concurrent worker threads for route probing (default from config or 1). Recommended max: 2.",
    )
    parser.add_argument(
        "--probe-timeout",
        type=int,
        default=15,
        help="Timeout in seconds for individual route prepay probes (default: 15s).",
    )
    parser.add_argument(
        "--skip-prepay-probe",
        action="store_true",
        help="Skip active prepay probing and rely on queryroutes theoretical fees (not recommended).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate route probing and quote calculation without initiating any real swap.",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Automatically select and initiate the top (#1) most economical candidate without interactive prompt.",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="Display past Loop Out swaps from Loop's database (Source of Truth).",
    )
    parser.add_argument(
        "--csv",
        nargs="?",
        const="default",
        default=None,
        help="Export historical swaps to CSV file (default: data/loop_out_history.csv).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=30,
        help="Maximum number of historical swaps to display (default: 30).",
    )
    parser.add_argument(
        "-p",
        "--pubkey",
        action="store_true",
        help="Display remote pubkeys in candidate table.",
    )
    return parser.parse_args()


def display_history(config: Any, limit: int = 30, csv_path: Optional[str] = None):
    """Prints table of historical loop-out operations from the Source of Truth."""
    swaps, source = fetch_loop_history(config, limit=limit)
    if not swaps:
        print_color(f"No past Loop Out operations found via {source}.", Colors.WARNING)
        return

    print_color(f"\n=== Lightning Loop Out - Historical Swaps ===", Colors.HEADER, bold=True)
    print_color(f"Source of Truth: {source}\n", Colors.OKCYAN)

    table = PrettyTable()
    table.field_names = [
        "Time (UTC)",
        "Swap ID",
        "Label / Channel",
        "Amount (sat)",
        "Server Fee",
        "Onchain Fee",
        "Route Fee",
        "Total Cost",
        "Net PPM",
        "Status",
    ]
    table.align = "r"
    table.align["Time (UTC)"] = "l"
    table.align["Swap ID"] = "l"
    table.align["Label / Channel"] = "l"
    table.align["Status"] = "c"

    for s in swaps:
        disp_label = s.get("label", "")
        if not disp_label:
            chans = s.get("outgoing_chan_set", "")
            disp_label = (chans[:28] + "...") if len(chans) > 28 else chans
        else:
            if disp_label.startswith("Loop-Out: "):
                disp_label = disp_label[10:]
            disp_label = re.sub(r"(\(\d+\s+chans\))(?:\s+\1)+", r"\1", disp_label)

        status = s.get("status", "UNKNOWN")
        status_colored = status
        if status == "SUCCESS":
            status_colored = f"{Colors.OKGREEN}{status}{Colors.ENDC}"
        elif status == "FAILED":
            status_colored = f"{Colors.FAIL}{status}{Colors.ENDC}"
        elif status in ["INITIATED", "HTLC_PUBLISHED", "PREIMAGE_REVEALED"]:
            status_colored = f"{Colors.WARNING}{status}{Colors.ENDC}"

        table.add_row(
            [
                s.get("initiation_time", ""),
                s.get("swap_id", "")[:12] + "...",
                disp_label[:30],
                f"{s.get('amount', 0):,}",
                f"{s.get('server_fee', 0):,}",
                f"{s.get('onchain_fee', 0):,}",
                f"{s.get('routing_fee', 0):,}",
                f"{s.get('total_cost', 0):,}",
                f"{s.get('effective_ppm', 0):,}",
                status_colored,
            ]
        )
    print(table)

    if csv_path:
        export_history_to_csv(swaps, csv_path)
        print_color(f"\n✓ Exported {len(swaps)} records to CSV: {csv_path}", Colors.OKGREEN)


def main():
    args = parse_arguments()
    config, project_root = load_config()
    logger = setup_logger(project_root)

    if args.history:
        csv_target = None
        if args.csv is not None:
            csv_target = (
                args.csv
                if args.csv != "default"
                else os.path.join(project_root, "data", "loop_out_history.csv")
            )
        display_history(config, limit=args.limit, csv_path=csv_target)
        return

    # Configuration defaults
    loop_pubkey = config.get("loop", "loop_pubkey", fallback=LOOP_PUBKEY_DEFAULT)
    min_capacity = args.capacity or config.getint("loop", "min_capacity", fallback=3_000_000)
    max_fee_ppm = args.fee_limit if args.fee_limit is not None else config.getint("loop", "max_local_fee_ppm", fallback=100)
    min_local_ratio = args.min_ratio if args.min_ratio is not None else config.getfloat("loop", "min_local_balance_ratio", fallback=60.0)
    target_local_ratio = config.getfloat("loop", "target_local_ratio", fallback=50.0)
    conf_target = args.conf_target or config.getint("loop", "conf_target", fallback=DEFAULT_CONF_TARGET)

    if conf_target < MIN_CONF_TARGET:
        print_color(f"Warning: Minimum conf-target is {MIN_CONF_TARGET} for economical sweeping. Adjusting to {MIN_CONF_TARGET}.", Colors.WARNING)
        conf_target = MIN_CONF_TARGET

    blacklist_raw = config.get("no-swapout", "swapout_blacklist", fallback="")
    blacklist = [pk.strip() for pk in blacklist_raw.split(",") if pk.strip()]

    print_color("=== Lightning Loop Out - Economic Liquidity Optimizer ===", Colors.HEADER, bold=True)
    if args.dry_run:
        print_color("[!] RUNNING IN DRY-RUN SIMULATION MODE (No funds will move)", Colors.WARNING, bold=True)

    print(f"Fetching channels from LNDg API...")
    channels = fetch_channels_lndg(config)
    if not channels:
        print_color("No channels found or could not connect to LNDg API. Exiting.", Colors.FAIL)
        sys.exit(1)

    print(f"Filtering candidates (min_capacity: {min_capacity:,} sat, max_fee: {max_fee_ppm} ppm, min_ratio: {min_local_ratio}%)...")
    candidates = filter_and_size_candidates(
        channels=channels,
        target_amt=args.amt,
        min_capacity=min_capacity,
        max_fee_rate=max_fee_ppm,
        min_local_ratio=min_local_ratio,
        target_local_ratio=target_local_ratio,
        blacklist=blacklist,
    )

    target_channel_ids = []
    if getattr(args, "channel", None):
        target_channel_ids = [c.strip() for c in args.channel.split(",") if c.strip()]
        candidates = [c for c in candidates if c["chan_id"] in target_channel_ids]

    if not candidates:
        print_color("No suitable candidate channels found matching criteria.", Colors.WARNING)
        sys.exit(0)

    print_color(f"Found {len(candidates)} channel candidates. Fetching quotes and probing paths to Loop server...", Colors.OKBLUE)

    # Route probing & quote evaluation on top candidates
    loop_cmd = resolve_loop_command(config)
    evaluated_candidates = []
    top_candidates = candidates[:8]
    workers = args.workers or config.getint("loop", "workers", fallback=1)
    workers = max(1, workers)

    if workers > 1:
        import concurrent.futures
        import threading
        print_color(f"Probing {len(top_candidates)} candidate channels using {workers} concurrent workers...", Colors.OKCYAN)
        print_lock = threading.Lock()
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(
                    evaluate_single_candidate,
                    c=c,
                    config=config,
                    loop_cmd=loop_cmd,
                    loop_pubkey=loop_pubkey,
                    conf_target=conf_target,
                    probe_timeout=args.probe_timeout,
                    skip_prepay_probe=args.skip_prepay_probe,
                    print_lock=print_lock,
                )
                for c in top_candidates
            ]
            for future in concurrent.futures.as_completed(futures):
                res = future.result()
                if res:
                    evaluated_candidates.append(res)
    else:
        for c in top_candidates:
            res = evaluate_single_candidate(
                c=c,
                config=config,
                loop_cmd=loop_cmd,
                loop_pubkey=loop_pubkey,
                conf_target=conf_target,
                probe_timeout=args.probe_timeout,
                skip_prepay_probe=args.skip_prepay_probe,
                print_lock=None,
            )
            if res:
                evaluated_candidates.append(res)

    if not evaluated_candidates:
        print_color("\nNo candidates passed both queryroutes and prepay probing.", Colors.FAIL, bold=True)
        sys.exit(1)

    # Rank ascending by effective_ppm
    evaluated_candidates.sort(key=lambda x: x["effective_ppm"])

    # Selection
    max_channels = args.max_channels or config.getint("loop", "max_channels", fallback=3)
    selected = None
    if args.auto_approve:
        print("\nEvaluated Candidates:")
        print_candidates_table(evaluated_candidates)
        top_cap = evaluated_candidates[0].get("drainable_surplus", evaluated_candidates[0]["proposed_amt"])
        if args.amt and args.amt > top_cap:
            batch = []
            acc = 0
            for c in evaluated_candidates:
                batch.append(c)
                acc += c.get("drainable_surplus", c.get("proposed_amt", 0))
                if acc >= args.amt or len(batch) >= max_channels:
                    break
            selected = batch
        else:
            selected = evaluated_candidates[0]

        if isinstance(selected, list):
            aliases = ", ".join(c["alias"] for c in selected)
            print_color(f"\nAuto-approved batch of {len(selected)} candidates: {aliases}", Colors.OKGREEN, bold=True)
        else:
            print_color(f"\nAuto-approved top candidate: {selected['alias']} ({selected['chan_id']})", Colors.OKGREEN, bold=True)
    else:
        selected = interactive_menu_select(evaluated_candidates, target_amt=args.amt, max_channels=max_channels)

    if not selected:
        print_color("Operation cancelled. No swap initiated.", Colors.WARNING)
        sys.exit(0)

    selected_channels = [selected] if isinstance(selected, dict) else selected

    if len(selected_channels) == 1:
        sel = selected_channels[0]
        chan_ids = sel["chan_id"]
        alias_str = sel["alias"]
        total_swap_amt = sel["proposed_amt"]
        probed_routing_fee = sel["routing_fee"]
        total_cost = sel["total_cost"]
    else:
        chan_ids = [c["chan_id"] for c in selected_channels]
        aliases = [c["alias"] if c.get("alias") else str(c["chan_id"]) for c in selected_channels]
        if len(selected_channels) <= 3 and sum(len(a) for a in aliases) <= 60:
            alias_str = ", ".join(aliases)
        elif len(selected_channels) > 1:
            alias_str = f"{aliases[0]} + {len(selected_channels) - 1} more"
        else:
            alias_str = aliases[0]
        if args.amt and args.amt > 0:
            total_swap_amt = args.amt
        else:
            total_swap_amt = min(c["proposed_amt"] for c in selected_channels)
        total_swap_amt = min(total_swap_amt, MAX_LOOP_OUT_SATS)

        probed_routing_fee = max(c["routing_fee"] for c in selected_channels)
        total_cost = max(c["total_cost"] for c in selected_channels)

        ppms = [c["effective_ppm"] for c in selected_channels]
        costs = [c["total_cost"] for c in selected_channels]
        min_ppm, max_ppm = min(ppms), max(ppms)
        min_cost, max_cost = min(costs), max(costs)
        ppm_str = f"{min_ppm:,} ppm" if min_ppm == max_ppm else f"{min_ppm:,} – {max_ppm:,} ppm"
        cost_str = f"{min_cost:,} sats" if min_cost == max_cost else f"{min_cost:,} – {max_cost:,} sats"

        print_color(f"\nMulti-Channel Loop Out Outbound Set ({len(selected_channels)} channels):", Colors.OKGREEN, bold=True)
        for idx, sc in enumerate(selected_channels, 1):
            print(f"  [{idx}] {sc['alias']} ({sc['chan_id']}): Probed Route Fee: {sc['routing_fee']:,} sat, Net PPM: {sc['effective_ppm']}")
        print(f"  Fixed Swap Size: {total_swap_amt:,} sats")
        print(f"  Effective PPM Range: {ppm_str} (Est Cost: {cost_str})")

    # Max routing fee budget with configurable leeway
    max_rf = calculate_max_routing_fee_budget(
        probed_routing_fee=probed_routing_fee,
        config=config,
        explicit_max_routing_fee=args.max_routing_fee,
        explicit_leeway_pct=args.fee_leeway_pct,
    )

    if max_rf > 0:
        buffer_sats = max_rf - probed_routing_fee
        print_color(
            f"Routing Fee Budget: {max_rf:,} sats (Max Probed: {probed_routing_fee:,} sats + {buffer_sats:,} sat leeway)",
            Colors.OKCYAN,
        )
    else:
        print_color(
            "Routing Fee Budget: Unbounded (using Loop daemon default limit)",
            Colors.OKCYAN,
        )

    # Execute
    res = execute_loop_out(
        config=config,
        channel_id=chan_ids,
        amt=total_swap_amt,
        conf_target=conf_target,
        max_routing_fee=max_rf,
        dest_addr=args.dest_addr,
        alias=alias_str,
        dry_run=args.dry_run,
    )

    if res.get("success"):
        chan_ids_str = ",".join(str(c) for c in chan_ids) if isinstance(chan_ids, list) else str(chan_ids)
        logger.info(
            f"Swap initiated for {alias_str} ({chan_ids_str}): "
            f"amt={total_swap_amt} sats, total_cost={total_cost} sats"
        )

        if not args.dry_run:
            monitor_loop(config)


if __name__ == "__main__":
    main()
