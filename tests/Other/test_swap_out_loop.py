import json
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Unit tests for Other/swap_out-loop.py
"""

import os
import sys
import tempfile
import sqlite3
import csv
from unittest.mock import patch, MagicMock
import pytest

import importlib.util
from pathlib import Path

script_path = Path(__file__).resolve().parent.parent.parent / "Other" / "swap_out-loop.py"
spec = importlib.util.spec_from_file_location("swap_out_loop", str(script_path))
swap_out_loop = importlib.util.module_from_spec(spec)
sys.modules["swap_out_loop"] = swap_out_loop
spec.loader.exec_module(swap_out_loop)


@pytest.fixture
def sample_channels():
    return [
        {
            "chan_id": "111111111111111111",
            "capacity": 10_000_000,
            "local_balance": 8_500_000,
            "remote_balance": 1_500_000,
            "local_fee_rate": 5,
            "remote_pubkey": "02aaaabbbbcccc1111222233334444555566667777888899990000aaaabbbbcccc",
            "alias": "Cheap-High-Liquidity-Node",
            "is_active": True,
            "is_open": True,
        },
        {
            "chan_id": "222222222222222222",
            "capacity": 10_000_000,
            "local_balance": 9_000_000,
            "remote_balance": 1_000_000,
            "local_fee_rate": 450,  # High fee (high opportunity cost)
            "remote_pubkey": "02bbbbccccdddd1111222233334444555566667777888899990000bbbbccccdddd",
            "alias": "Expensive-High-Liquidity-Node",
            "is_active": True,
            "is_open": True,
        },
        {
            "chan_id": "333333333333333333",
            "capacity": 10_000_000,
            "local_balance": 3_000_000,  # Low local balance (30%)
            "remote_balance": 7_000_000,
            "local_fee_rate": 10,
            "remote_pubkey": "02ccccddddeeee1111222233334444555566667777888899990000ccccddddeeee",
            "alias": "Low-Local-Balance-Node",
            "is_active": True,
            "is_open": True,
        },
        {
            "chan_id": "444444444444444444",
            "capacity": 2_000_000,  # Below min capacity
            "local_balance": 1_800_000,
            "remote_balance": 200_000,
            "local_fee_rate": 10,
            "remote_pubkey": "02ddddeeeeffff1111222233334444555566667777888899990000ddddeeeeffff",
            "alias": "Small-Channel-Node",
            "is_active": True,
            "is_open": True,
        },
        {
            "chan_id": "555555555555555555",
            "capacity": 10_000_000,
            "local_balance": 8_000_000,
            "remote_balance": 2_000_000,
            "local_fee_rate": 15,
            "remote_pubkey": "02blacklist1111111111111111111111111111111111111111111111111111111",
            "alias": "Blacklisted-Node",
            "is_active": True,
            "is_open": True,
        },
        {
            "chan_id": "666666666666666666",
            "capacity": 10_000_000,
            "local_balance": 8_000_000,
            "remote_balance": 2_000_000,
            "local_fee_rate": 15,
            "remote_pubkey": "02inactive11111111111111111111111111111111111111111111111111111111",
            "alias": "Inactive-Node",
            "is_active": False,  # Inactive
            "is_open": True,
        },
    ]


def test_filter_and_size_candidates_dynamic(sample_channels):
    """Test dynamic sizing and filtering with default thresholds."""
    blacklist = ["02blacklist1111111111111111111111111111111111111111111111111111111"]
    candidates = swap_out_loop.filter_and_size_candidates(
        channels=sample_channels,
        target_amt=None,
        min_capacity=3_000_000,
        max_fee_rate=100,
        min_local_ratio=60.0,
        blacklist=blacklist,
    )

    # Only Cheap-High-Liquidity-Node should qualify
    assert len(candidates) == 1
    c = candidates[0]
    assert c["chan_id"] == "111111111111111111"
    assert c["alias"] == "Cheap-High-Liquidity-Node"
    # Dynamic swap amount brings local balance down to 50% (5,000,000), so 8,500,000 - 5,000,000 = 3,500,000
    assert c["proposed_amt"] == 3_500_000


def test_filter_and_size_candidates_fixed_amount(sample_channels):
    """Test candidate filtering when an explicit target amount is passed."""
    blacklist = ["02blacklist1111111111111111111111111111111111111111111111111111111"]
    # User specifies 4,000,000 sats
    candidates = swap_out_loop.filter_and_size_candidates(
        channels=sample_channels,
        target_amt=4_000_000,
        min_capacity=3_000_000,
        max_fee_rate=500,  # allow expensive one as well
        min_local_ratio=60.0,
        blacklist=blacklist,
    )
    # Both Cheap and Expensive qualify and have proposed_amt == 4,000,000
    assert len(candidates) == 2
    for c in candidates:
        assert c["proposed_amt"] == 4_000_000


def test_parse_loop_quote_output_verbose():
    sample_quote_output = """
Send off-chain:                           2000000 sat
Receive on-chain:                         1997799 sat

Estimated on-chain fee:                       153 sat
Loop service fee:                            2048 sat
Estimated total fee:                         2201 sat

No show penalty (prepay):                   30000 sat
Conf target:                                    9 block
CLTV expiry delta:                              0 block
Publication deadline:                  2026-09-20 16:41:19 +0200 CEST
"""
    quote = swap_out_loop.parse_loop_quote_output(sample_quote_output, 2_000_000)
    assert quote["send_offchain"] == 2_000_000
    assert quote["receive_onchain"] == 1_997_799
    assert quote["estimated_onchain_fee"] == 153
    assert quote["service_fee"] == 2048
    assert quote["total_loop_fee"] == 2201


def test_parse_loop_quote_output_standard():
    sample_standard_output = """
Send off-chain:                           3500000 sat
Receive on-chain:                         3496000 sat
Estimated total fee:                         4000 sat
"""
    quote = swap_out_loop.parse_loop_quote_output(sample_standard_output, 3_500_000)
    assert quote["send_offchain"] == 3_500_000
    assert quote["receive_onchain"] == 3_496_000
    assert quote["total_loop_fee"] == 4000


def test_calculate_economic_cost():
    """Verify total economic cost calculation and effective PPM."""
    result = swap_out_loop.calculate_economic_cost(
        amt=2_000_000,
        service_fee=2_000,
        onchain_fee=200,
        routing_fee=50,
        local_fee_rate=20,
    )
    assert result["server_fee"] == 2_000
    assert result["onchain_fee"] == 200
    assert result["routing_fee"] == 50
    assert result["opportunity_cost"] == 40
    assert result["total_cost"] == 2_290
    assert result["effective_ppm"] == 1145


def test_queryroutes_parsing_success():
    """Test parsing lncli queryroutes JSON."""
    mock_routes = {
        "routes": [
            {
                "total_time_lock": 967983,
                "total_fees": "120",
                "total_amt": "2000120",
                "hops": [
                    {"chan_id": "111111111111111111", "pub_key": "02aaa..."},
                    {"chan_id": "999999999999999999", "pub_key": "021c97a90a411ff2b10dc2a8e32de2f29d2fa49d41bfbb52bd416e460db0747d0d"},
                ],
            }
        ]
    }
    with patch.object(swap_out_loop, "run_command") as mock_run:
        mock_run.return_value = (True, mock_routes, None)
        success, fee, hops = swap_out_loop.query_route_to_loop(
            config={},
            dest_pubkey="021c97a90a411ff2b10dc2a8e32de2f29d2fa49d41bfbb52bd416e460db0747d0d",
            amt=2_000_000,
            outgoing_chan_id="111111111111111111",
            dry_run=False,
        )
        assert success is True
        assert fee == 120
        assert hops == 2


def test_prepay_probe_success():
    """Test prepay probe detecting INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS as proof of viable route."""
    mock_output = {
        "failure_reason": "FAILURE_REASON_INCORRECT_PAYMENT_DETAILS",
        "payment_error": "incorrect or unknown payment details",
        "htlcs": [
            {
                "failure": {"code": "INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS"},
                "route": {"total_fees": "120", "hops": [{"chan_id": "111"}, {"chan_id": "222"}]},
            }
        ],
    }
    with patch.object(swap_out_loop, "run_command") as mock_run:
        mock_run.return_value = (True, mock_output, None)
        success, fee, hops, err = swap_out_loop.send_prepay_probe(
            config={},
            dest_pubkey="021c97a90a411ff2b10dc2a8e32de2f29d2fa49d41bfbb52bd416e460db0747d0d",
            amt=2_000_000,
            outgoing_chan_id="111111111111111111",
        )
        assert success is True
        assert fee == 120
        assert hops == 2
        assert err is None


def test_prepay_probe_channel_failure():
    """Test prepay probe failing due to lack of intermediate liquidity."""
    mock_output = {
        "failure_reason": "FAILURE_REASON_NO_ROUTE",
        "payment_error": "temporary channel failure",
    }
    with patch.object(swap_out_loop, "run_command") as mock_run:
        mock_run.return_value = (False, mock_output, "Probe failed: no route")
        success, fee, hops, err = swap_out_loop.send_prepay_probe(
            config={},
            dest_pubkey="021c97a90a411ff2b10dc2a8e32de2f29d2fa49d41bfbb52bd416e460db0747d0d",
            amt=2_000_000,
            outgoing_chan_id="111111111111111111",
        )
        assert success is False
        assert "no_route" in err.lower() or "no route" in err.lower() or "temporary channel failure" in err.lower()


def test_probe_direct_route_to_loop_success():
    """Test probe_direct_route_to_loop building direct route and probing with fake hash."""
    build_output = {
        "route": {
            "total_fees": "12206",
            "hops": [{"pub_key": "03peer"}, {"pub_key": "02loop"}],
        }
    }
    probe_output = {
        "failure": {"code": "INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS"}
    }
    with patch.object(swap_out_loop, "run_command", return_value=(True, build_output, None)),          patch("subprocess.run") as mock_subproc:
        mock_subproc.return_value.stdout = json.dumps(probe_output)
        success, fee, hops, err = swap_out_loop.probe_direct_route_to_loop(
            config={},
            remote_pubkey="03peer",
            dest_pubkey="02loop",
            amt=4_880_000,
        )
        assert success is True
        assert fee == 12206
        assert hops == 2
        assert err is None


def test_prepay_probe_fallback_to_direct_route():
    """Test send_prepay_probe falling back to direct route when multi-hop probe fails."""
    fail_output = {
        "failure_reason": "FAILURE_REASON_NO_ROUTE",
        "payment_error": "temporary channel failure",
    }
    with patch.object(swap_out_loop, "run_command", return_value=(False, fail_output, "temporary failure")),          patch.object(swap_out_loop, "probe_direct_route_to_loop", return_value=(True, 12206, 2, None)) as mock_dir:
        success, fee, hops, err = swap_out_loop.send_prepay_probe(
            config={},
            dest_pubkey="021c97a90a411ff2b10dc2a8e32de2f29d2fa49d41bfbb52bd416e460db0747d0d",
            amt=4_880_000,
            outgoing_chan_id="111111111111111111",
            remote_pubkey="03peer",
        )
        assert success is True
        assert fee == 12206
        assert hops == 2
        assert err is None
        mock_dir.assert_called_once()


def test_sqlite_accounting_store_lifecycle():
    """Test creating accounting store, inserting swap record, updating state, and CSV export."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_loop.db")
        csv_path = os.path.join(tmpdir, "test_loop.csv")

        store = swap_out_loop.AccountingStore(db_path=db_path, csv_path=csv_path)

        swap_data = {
            "swap_id": "swap-test-hash-12345",
            "amount": 3_500_000,
            "channel_id": "111111111111111111",
            "peer_alias": "Cheap-High-Liquidity-Node",
            "peer_pubkey": "02aaaabbbbcccc",
            "server_fee": 3500,
            "onchain_fee": 180,
            "routing_fee": 45,
            "opportunity_cost": 35,
            "total_cost": 3760,
            "effective_ppm": 1074,
            "conf_target": 9,
            "sweep_address": "bc1ptestaddress123",
            "status": "INITIATED",
        }
        store.record_swap_initiated(swap_data)

        # Verify in SQLite
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT swap_id, amount, status FROM loop_outs WHERE swap_id = ?", ("swap-test-hash-12345",))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "swap-test-hash-12345"
        assert row[1] == 3_500_000
        assert row[2] == "INITIATED"
        conn.close()

        # Update to SUCCESS
        store.update_swap_status("swap-test-hash-12345", "SUCCESS", "txid-sweep-99999")

        # Verify CSV export
        assert os.path.exists(csv_path)
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            assert len(reader) == 1
            assert reader[0]["swap_id"] == "swap-test-hash-12345"
            assert reader[0]["status"] == "SUCCESS"
            assert reader[0]["sweep_txid"] == "txid-sweep-99999"


def test_dry_run_execution():
    """Verify that dry_run mode does not invoke litloop out or send real funds."""
    with patch.object(swap_out_loop, "run_command") as mock_run:
        result = swap_out_loop.execute_loop_out(
            config={},
            channel_id="111111111111111111",
            amt=2_000_000,
            conf_target=9,
            max_routing_fee=100,
            dest_addr=None,
            dry_run=True,
        )
        assert result["dry_run"] is True
        assert result["success"] is True
        mock_run.assert_not_called()


def test_interactive_menu_select_non_tty():
    """Verify fallback to text input when stdin is not a TTY."""
    sample_candidates = [
        {
            "alias": "Node-A",
            "chan_id": "111",
            "proposed_amt": 2000000,
            "effective_ppm": 1100,
            "local_ratio": 80.0,
            "server_fee": 2000,
            "onchain_fee": 150,
            "routing_fee": 10,
            "opportunity_cost": 40,
            "total_cost": 2200,
        },
        {
            "alias": "Node-B",
            "chan_id": "222",
            "proposed_amt": 3000000,
            "effective_ppm": 1150,
            "local_ratio": 75.0,
            "server_fee": 3000,
            "onchain_fee": 150,
            "routing_fee": 20,
            "opportunity_cost": 60,
            "total_cost": 3230,
        },
    ]

    with patch('sys.stdin.isatty', return_value=False):
        with patch('builtins.input', return_value='1'):
            selected = swap_out_loop.interactive_menu_select(sample_candidates)
            assert selected is not None
            assert selected['chan_id'] == '111'

        with patch('builtins.input', return_value='q'):
            selected = swap_out_loop.interactive_menu_select(sample_candidates)
            assert selected is None
