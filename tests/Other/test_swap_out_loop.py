import json
import configparser
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


def test_get_loop_db_path():
    """Test determining loop_db_path from config.ini or default path."""
    config = configparser.ConfigParser()
    config.add_section("loop")
    config.set("loop", "loop_db_path", "/tmp/custom_loop_sqlite.db")
    path = swap_out_loop.get_loop_db_path(config)
    assert path == "/tmp/custom_loop_sqlite.db"


def test_fetch_loop_history_from_db_sot():
    """Test querying Loop Out history directly from Loop's SQLite DB (Source of Truth)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "loop_sqlite.db")
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE swaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            swap_hash BLOB,
            initiation_time TIMESTAMP,
            amount_requested BIGINT,
            label TEXT
        );
        """)
        cur.execute("""
        CREATE TABLE loopout_swaps (
            swap_hash BLOB PRIMARY KEY,
            dest_address TEXT,
            outgoing_chan_set TEXT
        );
        """)
        cur.execute("""
        CREATE TABLE swap_updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            swap_hash BLOB,
            update_timestamp TIMESTAMP,
            update_state INTEGER,
            server_cost BIGINT,
            onchain_cost BIGINT,
            offchain_cost BIGINT
        );
        """)
        hash1 = bytes.fromhex("11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff")
        cur.execute("INSERT INTO swaps (swap_hash, initiation_time, amount_requested, label) VALUES (?, ?, ?, ?)",
                    (hash1, "2026-09-20 14:00:00", 5000000, "Loop-Out: block-iad-1 (896468114071224320)"))
        cur.execute("INSERT INTO loopout_swaps (swap_hash, dest_address, outgoing_chan_set) VALUES (?, ?, ?)",
                    (hash1, "bc1ptest", "896468114071224320"))
        cur.execute("INSERT INTO swap_updates (swap_hash, update_timestamp, update_state, server_cost, onchain_cost, offchain_cost) VALUES (?, ?, ?, ?, ?, ?)",
                    (hash1, "2026-09-20 14:45:00", 2, 5000, 153, 12206))
        conn.commit()
        conn.close()

        swaps = swap_out_loop.fetch_loop_history_from_db(db_path, limit=10)
        assert len(swaps) == 1
        s0 = swaps[0]
        assert s0["swap_id"] == "11223344556677889900aabbccddeeff11223344556677889900aabbccddeeff"
        assert s0["amount"] == 5000000
        assert s0["server_fee"] == 5000
        assert s0["onchain_fee"] == 153
        assert s0["routing_fee"] == 12206
        assert s0["total_cost"] == 17359
        assert s0["effective_ppm"] == 3471
        assert s0["status"] == "SUCCESS"
        assert "block-iad-1" in s0["label"]


def test_fetch_loop_history_from_cli_fallback():
    """Test querying loop listswaps via RPC CLI when SQLite DB direct access is not used."""
    mock_listswaps = {
        "swaps": [
            {
                "id": "aabbccddeeff",
                "type": "LOOP_OUT",
                "amt": "4000000",
                "state": "SUCCESS",
                "initiation_time": "1726830000000000000",
                "cost_server": "4000",
                "cost_onchain": "150",
                "cost_offchain": "8000",
                "label": "Loop-Out: Sunny Sarah",
                "outgoing_chan_set": ["1055691691402854401"],
            },
            {
                "id": "112233",
                "type": "LOOP_IN",
                "amt": "20000000",
                "state": "SUCCESS",
            }
        ]
    }
    with patch.object(swap_out_loop, "run_command", return_value=(True, mock_listswaps, None)):
        swaps = swap_out_loop.fetch_loop_history_from_cli(config={}, limit=10)
        assert len(swaps) == 1
        assert swaps[0]["swap_id"] == "aabbccddeeff"
        assert swaps[0]["amount"] == 4000000
        assert swaps[0]["total_cost"] == 12150
        assert swaps[0]["status"] == "SUCCESS"


def test_export_history_to_csv():
    """Test exporting parsed history to CSV file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        csv_path = os.path.join(tmpdir, "test_history.csv")
        sample_swaps = [
            {
                "swap_id": "test1234",
                "initiation_time": "2026-09-20 14:00:00",
                "amount": 5000000,
                "label": "Loop-Out: test",
                "outgoing_chan_set": "123456",
                "server_fee": 5000,
                "onchain_fee": 150,
                "routing_fee": 1000,
                "total_cost": 6150,
                "effective_ppm": 1230,
                "status": "SUCCESS",
            }
        ]
        swap_out_loop.export_history_to_csv(sample_swaps, csv_path)
        assert os.path.exists(csv_path)
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
            assert len(reader) == 1
            assert reader[0]["swap_id"] == "test1234"
            assert reader[0]["total_cost"] == "6150"


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


def test_evaluate_single_candidate_success():
    """Test evaluate_single_candidate successfully validating quote, route, and prepay probe."""
    candidate = {
        "chan_id": "111",
        "proposed_amt": 2_000_000,
        "alias": "Test-Peer",
        "local_fee_rate": 10,
        "remote_pubkey": "03peer",
    }
    mock_quote = {"service_fee": 2000, "estimated_onchain_fee": 150}
    with patch.object(swap_out_loop, "get_loop_quote", return_value=mock_quote),          patch.object(swap_out_loop, "query_route_to_loop", return_value=(True, 25, 2)),          patch.object(swap_out_loop, "send_prepay_probe", return_value=(True, 25, 2, None)):
        res = swap_out_loop.evaluate_single_candidate(
            c=candidate,
            config={},
            loop_cmd=["litloop"],
            loop_pubkey="02loop",
            conf_target=9,
            probe_timeout=15,
            skip_prepay_probe=False,
        )
        assert res is not None
        assert res["chan_id"] == "111"
        assert res["service_fee"] == 2000
        assert res["routing_fee"] == 25
        assert res["total_cost"] == 2195


def test_calculate_max_routing_fee_budget_default():
    """Verify default fee leeway calculation (100% leeway / 2.0x + 500 sats buffer)."""
    budget = swap_out_loop.calculate_max_routing_fee_budget(
        probed_routing_fee=20_000,
        config={},
    )
    # 20,000 * 2.0 + 500 = 40,500
    assert budget == 40_500


def test_calculate_max_routing_fee_budget_explicit_zero_unbounded():
    """Verify that explicit 0 returns 0 (unbounded / loop daemon default)."""
    budget = swap_out_loop.calculate_max_routing_fee_budget(
        probed_routing_fee=20_000,
        config={},
        explicit_max_routing_fee=0,
    )
    assert budget == 0


def test_calculate_max_routing_fee_budget_explicit_positive():
    """Verify that explicit positive fee overrides all leeway calculations."""
    budget = swap_out_loop.calculate_max_routing_fee_budget(
        probed_routing_fee=20_000,
        config={},
        explicit_max_routing_fee=35_000,
    )
    assert budget == 35_000


def test_calculate_max_routing_fee_budget_custom_config():
    """Verify custom config settings for fee_leeway_pct and fee_leeway_base_sats."""
    import configparser
    cfg = configparser.ConfigParser()
    cfg.add_section("loop")
    cfg.set("loop", "fee_leeway_pct", "50")
    cfg.set("loop", "fee_leeway_base_sats", "1000")

    budget = swap_out_loop.calculate_max_routing_fee_budget(
        probed_routing_fee=20_000,
        config=cfg,
    )
    # 20,000 * 1.5 + 1000 = 31,000
    assert budget == 31_000


def test_calculate_max_routing_fee_budget_cli_leeway_override():
    """Verify that explicit_leeway_pct overrides config."""
    budget = swap_out_loop.calculate_max_routing_fee_budget(
        probed_routing_fee=20_000,
        config={},
        explicit_leeway_pct=200.0,
    )
    # 20,000 * 3.0 + 500 = 60,500
    assert budget == 60_500


def test_filter_and_size_candidates_multi_channel_pooling(sample_channels):
    """Verify channels with drainable surplus qualify when target_amt exceeds single channel capacity."""
    blacklist = ["02blacklist1111111111111111111111111111111111111111111111111111111"]
    # User requests 15,000,000 sats (no single channel in sample_channels has 15M)
    candidates = swap_out_loop.filter_and_size_candidates(
        channels=sample_channels,
        target_amt=15_000_000,
        min_capacity=3_000_000,
        max_fee_rate=500,
        min_local_ratio=60.0,
        blacklist=blacklist,
    )
    # Both Cheap (8.5M local) and Expensive (9M local) have drainable surplus >= 250k sats
    assert len(candidates) == 2
    for c in candidates:
        assert c["drainable_surplus"] >= 8_000_000
        assert c["proposed_amt"] <= 15_000_000


def test_execute_loop_out_multi_channel_list():
    """Verify execute_loop_out accepts a list of channel IDs and formats comma-separated argument."""
    res = swap_out_loop.execute_loop_out(
        config={},
        channel_id=["1055691691402854401", "896468114071224320"],
        amt=8_000_000,
        conf_target=9,
        max_routing_fee=40_000,
        alias="Sunny Sarah ☀️ + 1 more (2 chans)",
        dry_run=True,
    )
    assert res["success"] is True
    assert "dry-run-swap-" in res["swap_id"]


def test_interactive_menu_select_multi_channel_non_tty():
    """Verify non-TTY interactive_menu_select returns greedy batch when target_amt exceeds single channel."""
    candidates = [
        {
            "chan_id": "111",
            "alias": "Node-1",
            "proposed_amt": 4_000_000,
            "drainable_surplus": 4_000_000,
            "local_ratio": 90.0,
            "server_fee": 1000,
            "onchain_fee": 150,
            "routing_fee": 1000,
            "opportunity_cost": 0,
            "total_cost": 2150,
            "effective_ppm": 537,
        },
        {
            "chan_id": "222",
            "alias": "Node-2",
            "proposed_amt": 4_000_000,
            "drainable_surplus": 4_000_000,
            "local_ratio": 85.0,
            "server_fee": 1000,
            "onchain_fee": 150,
            "routing_fee": 1200,
            "opportunity_cost": 0,
            "total_cost": 2350,
            "effective_ppm": 587,
        },
        {
            "chan_id": "333",
            "alias": "Node-3",
            "proposed_amt": 4_000_000,
            "drainable_surplus": 4_000_000,
            "local_ratio": 80.0,
            "server_fee": 1000,
            "onchain_fee": 150,
            "routing_fee": 2000,
            "opportunity_cost": 0,
            "total_cost": 3150,
            "effective_ppm": 787,
        },
    ]

    # When target_amt is 7,000,000, first channel alone (4M) is not enough.
    # Non-TTY greedy batching should return 2 channels (111 and 222).
    selected = swap_out_loop.interactive_menu_select(candidates, target_amt=7_000_000, max_channels=3)
    assert selected is not None
    assert isinstance(selected, list)
    assert len(selected) == 2
    assert selected[0]["chan_id"] == "111"
    assert selected[1]["chan_id"] == "222"


def test_parse_arguments_multi_channel():
    """Verify --max-channels and --channel CLI arguments parse correctly."""
    with patch("sys.argv", ["swap_out-loop.py", "--max-channels", "4", "--channel", "111,222"]):
        args = swap_out_loop.parse_arguments()
        assert args.max_channels == 4
        assert args.channel == "111,222"


def test_interactive_menu_select_multi_channel_comma_input():
    """Verify non-TTY interactive_menu_select handles comma-separated manual input."""
    candidates = [
        {"chan_id": "111", "alias": "Node-1", "proposed_amt": 2_000_000, "drainable_surplus": 2_000_000, "local_ratio": 80.0, "server_fee": 500, "onchain_fee": 150, "routing_fee": 100, "opportunity_cost": 0, "total_cost": 750, "effective_ppm": 375},
        {"chan_id": "222", "alias": "Node-2", "proposed_amt": 2_000_000, "drainable_surplus": 2_000_000, "local_ratio": 75.0, "server_fee": 500, "onchain_fee": 150, "routing_fee": 150, "opportunity_cost": 0, "total_cost": 800, "effective_ppm": 400},
        {"chan_id": "333", "alias": "Node-3", "proposed_amt": 2_000_000, "drainable_surplus": 2_000_000, "local_ratio": 70.0, "server_fee": 500, "onchain_fee": 150, "routing_fee": 200, "opportunity_cost": 0, "total_cost": 850, "effective_ppm": 425},
    ]
    with patch("sys.stdin.isatty", return_value=False):
        with patch("builtins.input", return_value="1,3"):
            selected = swap_out_loop.interactive_menu_select(candidates, target_amt=None, max_channels=3)
            assert isinstance(selected, list)
            assert len(selected) == 2
            assert selected[0]["chan_id"] == "111"
            assert selected[1]["chan_id"] == "333"


def test_execute_loop_out_multi_channel_real_command():
    """Verify execute_loop_out formats command arguments correctly for real execution."""
    with patch.object(swap_out_loop, "resolve_loop_command", return_value=["litloop"]):
        with patch.object(swap_out_loop, "run_command", return_value=(True, "Swap initiated: 12345", None)) as mock_run:
            res = swap_out_loop.execute_loop_out(
                config={},
                channel_id=["111", "222"],
                amt=6_000_000,
                conf_target=9,
                max_routing_fee=1500,
                dest_addr="bc1qtestaddr",
                alias="Multi-Peer",
                dry_run=False,
            )
            assert res["success"] is True
            assert res["swap_id"] == "12345"
            mock_run.assert_called_once()
            called_cmd = mock_run.call_args[0][0]
            assert "--channel" in called_cmd
            chan_idx = called_cmd.index("--channel")
            assert called_cmd[chan_idx + 1] == "111,222"
            assert "--amt" in called_cmd
            amt_idx = called_cmd.index("--amt")
            assert called_cmd[amt_idx + 1] == "6000000"
            assert "--max_swap_routing_fee" in called_cmd
            fee_idx = called_cmd.index("--max_swap_routing_fee")
            assert called_cmd[fee_idx + 1] == "1500"
            assert "--addr" in called_cmd
            addr_idx = called_cmd.index("--addr")
            assert called_cmd[addr_idx + 1] == "bc1qtestaddr"


def test_main_multi_channel_logging_success():
    """Verify main() executes and logs successfully when interactive_menu_select returns a multi-channel list."""
    mock_candidates = [
        {
            "chan_id": "896468114071224320",
            "alias": "block-iad-1",
            "proposed_amt": 3_000_000,
            "drainable_surplus": 3_000_000,
            "local_ratio": 99.0,
            "server_fee": 3049,
            "onchain_fee": 163,
            "routing_fee": 7494,
            "opportunity_cost": 0,
            "total_cost": 10706,
            "effective_ppm": 3569,
        },
        {
            "chan_id": "1028289662652973056",
            "alias": "allNice | torq.co",
            "proposed_amt": 3_000_000,
            "drainable_surplus": 3_000_000,
            "local_ratio": 95.4,
            "server_fee": 3049,
            "onchain_fee": 163,
            "routing_fee": 10497,
            "opportunity_cost": 0,
            "total_cost": 13709,
            "effective_ppm": 4570,
        },
    ]

    mock_args = MagicMock()
    mock_args.history = False
    mock_args.capacity = 3_000_000
    mock_args.fee_limit = 100
    mock_args.min_ratio = 60.0
    mock_args.amt = 3_000_000
    mock_args.conf_target = 9
    mock_args.max_routing_fee = None
    mock_args.fee_leeway_pct = None
    mock_args.dest_addr = None
    mock_args.dry_run = True
    mock_args.auto_approve = False
    mock_args.max_channels = 3
    mock_args.channel = None
    mock_args.workers = 1
    mock_args.probe_timeout = 15
    mock_args.skip_prepay_probe = False

    with patch.object(swap_out_loop, "parse_arguments", return_value=mock_args),          patch.object(swap_out_loop, "load_config", return_value=(configparser.ConfigParser(), "/tmp")),          patch.object(swap_out_loop, "setup_logger") as mock_setup_logger,          patch.object(swap_out_loop, "fetch_channels_lndg", return_value=[{"is_active": True, "is_open": True, "capacity": 10000000, "local_balance": 8000000, "local_fee_rate": 5, "chan_id": "111", "alias": "node"}]),          patch.object(swap_out_loop, "filter_and_size_candidates", return_value=mock_candidates),          patch.object(swap_out_loop, "evaluate_single_candidate", side_effect=lambda c, **kwargs: c),          patch.object(swap_out_loop, "interactive_menu_select", return_value=mock_candidates),          patch.object(swap_out_loop, "execute_loop_out", return_value={"success": True, "swap_id": "mock-swap-id", "dry_run": True}):

        mock_logger = MagicMock()
        mock_setup_logger.return_value = mock_logger

        # Calling main() should NOT raise TypeError: list indices must be integers or slices, not str
        swap_out_loop.main()

        mock_logger.info.assert_called_once()
        log_call_msg = mock_logger.info.call_args[0][0]
        assert "block-iad-1, allNice | torq.co" in log_call_msg
        assert "896468114071224320,1028289662652973056" in log_call_msg


def test_multi_channel_fixed_amount_and_max_fee_budget():
    """Verify that multi-channel execution keeps target_amt fixed and bases fee budget on max route fee."""
    mock_candidates = [
        {
            "chan_id": "896468114071224320",
            "alias": "block-iad-1",
            "proposed_amt": 3_000_000,
            "drainable_surplus": 3_000_000,
            "local_ratio": 99.0,
            "server_fee": 3049,
            "onchain_fee": 163,
            "routing_fee": 7494,
            "opportunity_cost": 0,
            "total_cost": 10706,
            "effective_ppm": 3569,
        },
        {
            "chan_id": "1028289662652973056",
            "alias": "allNice | torq.co",
            "proposed_amt": 3_000_000,
            "drainable_surplus": 3_000_000,
            "local_ratio": 95.4,
            "server_fee": 3049,
            "onchain_fee": 163,
            "routing_fee": 10497,
            "opportunity_cost": 0,
            "total_cost": 13709,
            "effective_ppm": 4570,
        },
    ]

    mock_args = MagicMock()
    mock_args.history = False
    mock_args.capacity = 3_000_000
    mock_args.fee_limit = 100
    mock_args.min_ratio = 60.0
    mock_args.amt = 3_000_000  # User specified 3,000,000 sats
    mock_args.conf_target = 9
    mock_args.max_routing_fee = None
    mock_args.fee_leeway_pct = 100.0  # +100% leeway
    mock_args.dest_addr = None
    mock_args.dry_run = True
    mock_args.auto_approve = False
    mock_args.max_channels = 3
    mock_args.channel = None
    mock_args.workers = 1
    mock_args.probe_timeout = 15
    mock_args.skip_prepay_probe = False

    config = configparser.ConfigParser()
    config.add_section("loop")
    config.set("loop", "fee_leeway_pct", "100.0")
    config.set("loop", "fee_leeway_base_sats", "500")

    with patch.object(swap_out_loop, "parse_arguments", return_value=mock_args),          patch.object(swap_out_loop, "load_config", return_value=(config, "/tmp")),          patch.object(swap_out_loop, "setup_logger") as mock_setup_logger,          patch.object(swap_out_loop, "fetch_channels_lndg", return_value=[{"is_active": True, "is_open": True, "capacity": 10000000, "local_balance": 8000000, "local_fee_rate": 5, "chan_id": "111", "alias": "node"}]),          patch.object(swap_out_loop, "filter_and_size_candidates", return_value=mock_candidates),          patch.object(swap_out_loop, "evaluate_single_candidate", side_effect=lambda c, **kwargs: c),          patch.object(swap_out_loop, "interactive_menu_select", return_value=mock_candidates),          patch.object(swap_out_loop, "execute_loop_out", return_value={"success": True, "swap_id": "mock-swap-id", "dry_run": True}) as mock_exec:

        mock_logger = MagicMock()
        mock_setup_logger.return_value = mock_logger

        swap_out_loop.main()

        # execute_loop_out must be called with amt=3,000,000 (NOT 6,000,000)
        assert mock_exec.call_args[1]["amt"] == 3_000_000
        # max_routing_fee must be based on max(7494, 10497) = 10497 -> 10497 * 2.0 + 500 = 21494
        assert mock_exec.call_args[1]["max_routing_fee"] == 21_494
        # channel_id list contains both channels
        assert mock_exec.call_args[1]["channel_id"] == ["896468114071224320", "1028289662652973056"]


def test_execute_loop_out_label_deduplication():
    # Verify execute_loop_out does not duplicate (X chans) if already in alias
    with patch.object(swap_out_loop, "resolve_loop_command", return_value=["litloop"]):
        with patch.object(swap_out_loop, "run_command", return_value=(True, "Swap initiated: 999", None)) as mock_run:
            # Case 1: alias already contains (2 chans)
            swap_out_loop.execute_loop_out(
                config={},
                channel_id=["111", "222"],
                amt=3_000_000,
                alias="block-iad-1 + 1 more (2 chans)",
                dry_run=False,
            )
            cmd1 = mock_run.call_args[0][0]
            label_idx1 = cmd1.index("--label")
            assert cmd1[label_idx1 + 1] == "Loop-Out: block-iad-1 + 1 more (2 chans)"

            # Case 2: alias without channel count
            swap_out_loop.execute_loop_out(
                config={},
                channel_id=["111", "222"],
                amt=3_000_000,
                alias="block-iad-1 + 1 more",
                dry_run=False,
            )
            cmd2 = mock_run.call_args[0][0]
            label_idx2 = cmd2.index("--label")
            assert cmd2[label_idx2 + 1] == "Loop-Out: block-iad-1 + 1 more (2 chans)"


def test_fetch_loop_history_from_db_preimage_revealed():
    # Verify fetch_loop_history_from_db maps state 1 to PREIMAGE_REVEALED when sweep is pending
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "loop_sqlite.db")
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute('''
        CREATE TABLE swaps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            swap_hash BLOB,
            initiation_time TIMESTAMP,
            amount_requested BIGINT,
            label TEXT
        );
        ''')
        cur.execute('''
        CREATE TABLE loopout_swaps (
            swap_hash BLOB PRIMARY KEY,
            dest_address TEXT,
            outgoing_chan_set TEXT
        );
        ''')
        cur.execute('''
        CREATE TABLE swap_updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            swap_hash BLOB,
            update_timestamp TIMESTAMP,
            update_state INTEGER,
            server_cost BIGINT,
            onchain_cost BIGINT,
            offchain_cost BIGINT
        );
        ''')
        cur.execute('''
        CREATE TABLE sweep_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            confirmed BOOLEAN NOT NULL DEFAULT FALSE,
            batch_tx_id TEXT
        );
        ''')
        cur.execute('''
        CREATE TABLE sweeps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            swap_hash BLOB NOT NULL,
            batch_id INTEGER NOT NULL,
            outpoint TEXT NOT NULL,
            amt BIGINT NOT NULL,
            completed BOOLEAN NOT NULL DEFAULT FALSE
        );
        ''')

        hash1 = bytes.fromhex("da536e98a98a16a15030d8f5224f41aaeebec1eb07d096285aaa34fd13d5379a")
        cur.execute(
            "INSERT INTO swaps (swap_hash, initiation_time, amount_requested, label) VALUES (?, ?, ?, ?)",
            (hash1, "2026-09-21 17:34:59", 3000000, "Loop-Out: block-iad-1 + 1 more (2 chans)")
        )
        cur.execute(
            "INSERT INTO loopout_swaps (swap_hash, dest_address, outgoing_chan_set) VALUES (?, ?, ?)",
            (hash1, "bc1ptest", "896468114071224320,1028289662652973056")
        )
        cur.execute(
            "INSERT INTO swap_updates (swap_hash, update_timestamp, update_state, server_cost, onchain_cost, offchain_cost) VALUES (?, ?, ?, ?, ?, ?)",
            (hash1, "2026-09-21 18:53:55", 1, 0, 0, 74)
        )
        cur.execute(
            "INSERT INTO sweep_batches (confirmed, batch_tx_id) VALUES (?, ?)",
            (False, "393404e4c9c51490fa638cf2a25690ca70e2354bd0dc0d5fea7553f1bd6b40ff")
        )
        cur.execute(
            "INSERT INTO sweeps (swap_hash, batch_id, outpoint, amt, completed) VALUES (?, ?, ?, ?, ?)",
            (hash1, 1, "e3e3:6", 2999887, False)
        )
        conn.commit()
        conn.close()

        swaps = swap_out_loop.fetch_loop_history_from_db(db_path, limit=10)
        assert len(swaps) == 1
        s0 = swaps[0]
        assert s0["status"] == "PREIMAGE_REVEALED"
        assert s0["routing_fee"] == 74
        assert s0["onchain_fee"] == 113
        assert s0["total_cost"] == 187
        assert s0["effective_ppm"] == 62
