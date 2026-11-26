"""Regression tests for P0 runtime safety fixes across production scripts."""

from __future__ import annotations

import configparser
import importlib.util
import json
import locale
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, mock_open, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_isolated_module(rel_path: str, module_name: str):
    """Load a script under an isolated module name so hyphenated paths and monkeypatches stay local."""
    script_path = REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fee_adjuster_atomic_charge_lnd_write_overwrites_and_sets_0644(tmp_path):
    module = _load_isolated_module("Other/fee_adjuster.py", "_p0_fee_adjuster_atomic")
    target = tmp_path / "charge-lnd" / "fee_adjuster.txt"

    first_entries = [
        module.format_charge_lnd_entry("02aaa", "PeerA", 120, False, True, -30),
        module.format_charge_lnd_entry("03bbb", "PeerB", 250, True, False, 0),
    ]
    module.write_charge_lnd_entries_atomic(str(target), first_entries)

    second_entries = [
        module.format_charge_lnd_entry("02aaa", "PeerA", 140, False, True, -35),
    ]
    module.write_charge_lnd_entries_atomic(str(target), second_entries)

    content = target.read_text(encoding="utf-8")
    assert content.count("node.id = 02aaa") == 1
    assert "fee_ppm = 140" in content
    assert "node.id = 03bbb" not in content
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_fee_adjuster_fatal_error_exits_nonzero_without_sleeping(monkeypatch):
    module = _load_isolated_module("Other/fee_adjuster.py", "_p0_fee_adjuster_exit")
    sleep_mock = MagicMock()
    monkeypatch.setattr(module.time, "sleep", sleep_mock)
    monkeypatch.setattr(module, "load_config", MagicMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(sys, "argv", ["fee_adjuster.py"])

    with pytest.raises(SystemExit) as exc_info:
        module.main()

    assert exc_info.value.code == 1
    sleep_mock.assert_not_called()


def test_fee_adjuster_http_calls_pass_timeout(monkeypatch):
    module = _load_isolated_module("Other/fee_adjuster.py", "_p0_fee_adjuster_timeout")
    cfg = configparser.ConfigParser()
    cfg.read_dict(
        {
            "lndg": {"lndg_api_url": "http://localhost:8889"},
            "credentials": {"lndg_username": "u", "lndg_password": "p"},
        }
    )

    timeouts: list[tuple[str, object]] = []

    class DummyResponse:
        status_code = 200
        text = "{}"

        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    def fake_get(url, **kwargs):
        timeouts.append(("GET", kwargs.get("timeout")))
        if "forwards" in url:
            return DummyResponse({"results": []})
        return DummyResponse(
            {
                "results": [
                    {
                        "remote_pubkey": "02aaa",
                        "chan_id": "111",
                        "is_open": True,
                        "is_active": True,
                        "local_balance": 500,
                        "capacity": 1000,
                        "local_fee_rate": 100,
                        "alias": "PeerA",
                        "num_updates": 10,
                    }
                ]
            }
        )

    def fake_put(url, **kwargs):
        timeouts.append(("PUT", kwargs.get("timeout")))
        return DummyResponse({})

    def fake_post(url, **kwargs):
        timeouts.append(("POST", kwargs.get("timeout")))
        return DummyResponse({})

    monkeypatch.setattr(module.requests, "get", fake_get)
    monkeypatch.setattr(module.requests, "put", fake_put)
    monkeypatch.setattr(module.requests, "post", fake_post)
    module._CHANNEL_CACHE = None

    module.fetch_all_channels(cfg)
    module.get_last_peer_forwarding_timestamp("02aaa", 7, ["111"], cfg)
    module.update_lndg_fee(
        "111",
        150,
        -20,
        {"auto_fees": True, "ar_out_target": 50},
        cfg,
        new_ar_out_target=100,
    )
    module.update_channel_notes(
        "111",
        "PeerA",
        "default",
        "median",
        {},
        0.5,
        2,
        1.0,
        cfg,
        {"update_channel_notes": True},
    )

    assert len(timeouts) == 5
    assert all(val == module.DEFAULT_HTTP_TIMEOUT for _, val in timeouts)


def test_pocketmoney_skips_success_alert_on_failed_payment(
    tmp_path, monkeypatch, requests_mock
):
    module = _load_isolated_module("LNBits/pocketmoney.py", "_p0_pocketmoney_fail")
    lnbits_dir = tmp_path / "LNBits"
    lnbits_dir.mkdir()
    (tmp_path / "wallets.json").write_text(
        json.dumps(
            {
                "ChildA": {
                    "wallet_id": "w1",
                    "invoice_key": "invkey",
                    "fiat_amount": 5,
                    "fiat_currency": "EUR",
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(module, "parent_dir", str(lnbits_dir))
    monkeypatch.setattr(module, "LNBITS_URL", "https://lnbits.example.com")
    monkeypatch.setattr(module, "ADMIN_KEY", "admkey")
    monkeypatch.setattr(module, "CHAT_ID", "12345")
    bot_mock = MagicMock()
    monkeypatch.setattr(module, "bot", bot_mock)

    requests_mock.get("https://lnbits.example.com/api/v1/rate/EUR", json={"rate": 1000})
    requests_mock.post(
        "https://lnbits.example.com/api/v1/payments",
        [
            {"status_code": 201, "json": {"bolt11": "lnbc1testinvoice"}},
            {"status_code": 500, "json": {"detail": "route failure"}},
        ],
    )
    wallet_mock = requests_mock.get(
        "https://lnbits.example.com/api/v1/wallet", json={"balance": 5000000}
    )

    failures = module.scheduled_transfer()

    assert failures == 1
    assert not wallet_mock.called
    sent_texts = [call.args[1] for call in bot_mock.send_message.call_args_list]
    assert not any("Transferred" in txt for txt in sent_texts)
    assert any("Error paying invoice" in txt for txt in sent_texts)


def test_pocketmoney_success_sends_alert_and_checks_balance_with_timeouts(
    tmp_path, monkeypatch, requests_mock
):
    module = _load_isolated_module("LNBits/pocketmoney.py", "_p0_pocketmoney_ok")
    lnbits_dir = tmp_path / "LNBits"
    lnbits_dir.mkdir()
    (tmp_path / "wallets.json").write_text(
        json.dumps(
            {
                "ChildA": {
                    "wallet_id": "w1",
                    "invoice_key": "invkey",
                    "fiat_amount": 5,
                    "fiat_currency": "EUR",
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(module, "parent_dir", str(lnbits_dir))
    monkeypatch.setattr(module, "LNBITS_URL", "https://lnbits.example.com")
    monkeypatch.setattr(module, "ADMIN_KEY", "admkey")
    monkeypatch.setattr(module, "CHAT_ID", "12345")
    bot_mock = MagicMock()
    monkeypatch.setattr(module, "bot", bot_mock)

    requests_mock.get("https://lnbits.example.com/api/v1/rate/EUR", json={"rate": 1000})
    requests_mock.post(
        "https://lnbits.example.com/api/v1/payments",
        [
            {"status_code": 201, "json": {"bolt11": "lnbc1testinvoice"}},
            {"status_code": 201, "json": {"payment_hash": "abc"}},
        ],
    )
    wallet_mock = requests_mock.get(
        "https://lnbits.example.com/api/v1/wallet", json={"balance": 5000000}
    )

    failures = module.scheduled_transfer()

    assert failures == 0
    assert wallet_mock.called
    assert all(req.timeout == module.DEFAULT_HTTP_TIMEOUT for req in requests_mock.request_history)
    sent_texts = [call.args[1] for call in bot_mock.send_message.call_args_list]
    assert any("Transferred" in txt for txt in sent_texts)

    # Verify --run-once exits 1 when scheduled_transfer returns failures
    monkeypatch.setattr(module, "scheduled_transfer", lambda: 1)
    monkeypatch.setattr(module, "validate_runtime_config", lambda: None)
    monkeypatch.setattr(sys, "argv", ["pocketmoney.py", "--run-once"])
    with pytest.raises(SystemExit) as exc_info:
        module.main()
    assert exc_info.value.code == 1


def test_magma_sale_process_expands_tilde_in_loop_tlscertpath(monkeypatch):
    mock_telebot = MagicMock()
    bot_mock = MagicMock()
    bot_mock.callback_query_handler = MagicMock(return_value=lambda f: f)
    bot_mock.message_handler = MagicMock(return_value=lambda f: f)
    mock_telebot.TeleBot = MagicMock(return_value=bot_mock)

    cfg = configparser.ConfigParser()
    cfg.read_dict(
        {
            "telegram": {"magma_bot_token": "fake_token", "telegram_user_id": "123"},
            "credentials": {"amboss_authorization": "fake_auth"},
            "system": {"full_path_bos": "/path/to/bos", "path_command": "/usr/local/bin"},
            "magma": {
                "invoice_expiry_seconds": "1800",
                "max_fee_percentage_of_invoice": "0.9",
                "channel_fee_rate_ppm": "350",
            },
            "urls": {"mempool_fees_api": "https://mempool.space/api/v1/fees/recommended"},
            "pubkey": {"banned_magma_pubkeys": ""},
            "paths": {"lncli_path": "/usr/local/bin/lncli"},
        }
    )
    mock_cp_mod = MagicMock()
    mock_cp_mod.ConfigParser.return_value = cfg

    with (
        patch.dict(
            sys.modules,
            {
                "telebot": mock_telebot,
                "telebot.types": MagicMock(),
                "configparser": mock_cp_mod,
                "schedule": MagicMock(),
                "logging.handlers": MagicMock(),
            },
        ),
        patch("builtins.open", mock_open(read_data="")),
        patch("os.makedirs"),
    ):
        module = _load_isolated_module(
            "Magma/magma_sale_process.py", "_p0_magma_loop_tilde"
        )

    recorded_cmds: list[list[str]] = []

    class FakeProc:
        def __init__(self, stdout_bytes: bytes):
            self._stdout = stdout_bytes
            self.returncode = 0

        def communicate(self):
            return self._stdout, b""

    def fake_popen(cmd, **_kwargs):
        recorded_cmds.append(cmd)
        return FakeProc(b'{"utxos": []}')

    monkeypatch.setattr(module.os.path, "exists", lambda p: p == "/usr/local/bin/loop")
    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)

    module.get_lncli_utxos()

    assert len(recorded_cmds) == 2
    loop_cmd = recorded_cmds[1]
    tls_arg = next(arg for arg in loop_cmd if arg.startswith("--tlscertpath="))
    assert "~" not in tls_arg
    assert tls_arg.endswith("/.lit/tls.cert")


def test_peerswap_lndg_push_survives_locale_error_and_help_skips_binaries(monkeypatch):
    def failing_setlocale(*_args, **_kwargs):
        raise locale.Error("unsupported locale setting")

    monkeypatch.setattr(locale, "setlocale", failing_setlocale)
    module = _load_isolated_module("Peerswap/peerswap-lndg_push.py", "_p0_ps_lndg_push")

    run_mock = MagicMock(side_effect=AssertionError("subprocess.run called during --help"))
    monkeypatch.setattr(module.subprocess, "run", run_mock)
    monkeypatch.setattr(sys, "argv", ["peerswap-lndg_push.py", "--help"])

    with pytest.raises(SystemExit) as exc_info:
        module.main()

    assert exc_info.value.code == 0
    run_mock.assert_not_called()


def test_peerswap_lndg_push_http_calls_pass_timeout(monkeypatch):
    module = _load_isolated_module(
        "Peerswap/peerswap-lndg_push.py", "_p0_ps_lndg_push_timeout"
    )
    monkeypatch.setattr(module, "lndg_api_url", "http://localhost:8889/api/channels")
    monkeypatch.setattr(module, "username", "u")
    monkeypatch.setattr(module, "password", "p")

    seen: list[tuple[str, object]] = []

    def fake_get(url, **kwargs):
        seen.append(("GET", kwargs.get("timeout")))
        return SimpleNamespace(status_code=200, json=lambda: {"notes": "ok"})

    def fake_put(url, **kwargs):
        seen.append(("PUT", kwargs.get("timeout")))
        return SimpleNamespace(status_code=400)

    monkeypatch.setattr(module.requests, "get", fake_get)
    monkeypatch.setattr(module.requests, "put", fake_put)

    assert module.get_current_notes("123") == "ok"
    module.update_notes("123", "hello")
    assert seen == [("GET", module.DEFAULT_HTTP_TIMEOUT), ("PUT", module.DEFAULT_HTTP_TIMEOUT)]


def test_channel_fee_pull_import_safe_and_atomic_write(tmp_path, monkeypatch):
    get_mock = MagicMock(side_effect=AssertionError("HTTP called during import"))
    monkeypatch.setattr("requests.get", get_mock)

    module = _load_isolated_module("LNDg/channel_fee-pull.py", "_p0_channel_fee_pull")
    get_mock.assert_not_called()

    target = tmp_path / ".chargelnd" / ".config" / "0_fee.txt"
    monkeypatch.setattr(module, "file_path", str(target))
    monkeypatch.setattr(module, "api_url", "http://localhost:8889/api/channels")
    monkeypatch.setattr(module, "ignore_remote_pubkeys", ["02ignored"])

    recorded_timeouts: list[object] = []

    def fake_get(url, **kwargs):
        recorded_timeouts.append(kwargs.get("timeout"))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "results": [
                    {"chan_id": "111", "local_fee_rate": 0, "remote_pubkey": "02keep"},
                    {"chan_id": "222", "local_fee_rate": 0, "remote_pubkey": "02ignored"},
                    {"chan_id": "333", "local_fee_rate": 50, "remote_pubkey": "02keep"},
                ]
            },
        )

    monkeypatch.setattr(module.requests, "get", fake_get)
    module.main()

    assert recorded_timeouts == [module.DEFAULT_HTTP_TIMEOUT]
    assert target.read_text(encoding="utf-8") == "111\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_peerswap_bot_import_safe_authz_and_timeout(monkeypatch):
    module = _load_isolated_module("Peerswap/peerswap-bot.py", "_p0_peerswap_bot")

    assert "//" not in module.PSCLI_BIN

    monkeypatch.setattr(module, "TELEGRAM_USER_ID", "999")
    assert module.is_authorized_user(999) is True
    assert module.is_authorized_user(111) is False

    seen_timeouts: list[object] = []

    def fake_get(url, **kwargs):
        seen_timeouts.append(kwargs.get("timeout"))
        return SimpleNamespace(json=lambda: {"alias": "TestNode"})

    monkeypatch.setattr(module.requests, "get", fake_get)
    assert module.get_node_alias("02abc") == "TestNode"
    assert seen_timeouts == [15]

    monkeypatch.setattr(module, "BOT_TOKEN", "")
    with pytest.raises(ValueError, match="peerswap_bot_token"):
        module.run_bot()
