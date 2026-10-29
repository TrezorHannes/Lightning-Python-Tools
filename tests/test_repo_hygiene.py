"""Tests for repository configuration, URL normalization, import safety, and README links."""

import importlib
import json
from pathlib import Path
import re
from unittest.mock import MagicMock

import pytest

import pocketmoney


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("raw_url", "expected"),
    [
        ("https://lnbits.example.com", "https://lnbits.example.com"),
        ("https://lnbits.example.com/", "https://lnbits.example.com"),
        ("https://lnbits.example.com/api/v1", "https://lnbits.example.com"),
        ("https://lnbits.example.com/api/v1/", "https://lnbits.example.com"),
        ("  http://localhost:5000/api/v1  ", "http://localhost:5000"),
    ],
)
def test_normalize_lnbits_base_url_prevents_double_api_v1(raw_url, expected):
    assert pocketmoney.normalize_lnbits_base_url(raw_url) == expected


def test_pocketmoney_get_exchange_rate_uses_single_api_v1_prefix(monkeypatch, requests_mock):
    monkeypatch.setattr(
        pocketmoney,
        "LNBITS_URL",
        pocketmoney.normalize_lnbits_base_url("https://lnbits.example.com/api/v1"),
    )
    requests_mock.get("https://lnbits.example.com/api/v1/rate/EUR", json={"rate": 1500})

    assert pocketmoney.get_exchange_rate("EUR") == 1500


def test_pocketmoney_validate_runtime_config_fails_closed(monkeypatch):
    monkeypatch.setattr(pocketmoney, "LNBITS_URL", "")
    monkeypatch.setattr(pocketmoney, "ADMIN_KEY", "")
    monkeypatch.setattr(pocketmoney, "TOKEN", "invalid-token")
    monkeypatch.setattr(pocketmoney, "CHAT_ID", "")

    with pytest.raises(ValueError, match="Missing or invalid config.ini settings"):
        pocketmoney.validate_runtime_config()


def test_ps_peers_has_no_import_time_subprocess_side_effects(monkeypatch):
    mock_run = MagicMock(side_effect=AssertionError("subprocess.run called at import time"))
    monkeypatch.setattr("subprocess.run", mock_run)

    module = importlib.import_module("ps_peers")
    importlib.reload(module)
    mock_run.assert_not_called()

    responses = {
        ("lncli", "getinfo"): {"identity_pubkey": "02local"},
        ("pscli", "listpeers"): {
            "peers": [
                {
                    "node_id": "03peer",
                    "swaps_allowed": True,
                    "supported_assets": ["lbtc"],
                    "channels": [
                        {
                            "channel_id": "123",
                            "local_balance": "500000",
                            "remote_balance": "500000",
                            "active": True,
                        }
                    ],
                }
            ]
        },
        ("pscli", "lbtc-getbalance"): {"sat_amount": "250000"},
        ("lncli", "listchannels"): {
            "channels": [{"remote_pubkey": "03peer", "peer_alias": "PeerNode"}]
        },
        ("lncli", "getchaninfo", "--chan_id", "123"): {
            "node1_pub": "02local",
            "node2_pub": "03peer",
            "node1_policy": {"fee_rate_milli_msat": "150"},
            "node2_policy": {"fee_rate_milli_msat": "200"},
        },
    }

    def fake_run(cmd, **_kwargs):
        res = MagicMock()
        res.stdout = json.dumps(responses[tuple(cmd)]).encode("utf-8")
        return res

    monkeypatch.setattr("subprocess.run", fake_run)
    module.main()


def test_requirements_excludes_stdlib_and_shadow_packages():
    req_lines = [
        line.strip().lower()
        for line in (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    pkg_names = {re.split(r"[><=!~\s]", line, maxsplit=1)[0] for line in req_lines}

    assert "configparser" not in pkg_names, "configparser is part of Python 3 stdlib"
    assert "telebot" not in pkg_names, "pyTelegramBotAPI already provides the telebot module"
    assert "pytelegrambotapi" in pkg_names


def test_readme_relative_file_links_exist():
    readme_text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    targets = re.findall(r"\]\(([^)]+)\)", readme_text)
    for target in targets:
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        rel_path = target.split("#", maxsplit=1)[0]
        assert (REPO_ROOT / rel_path).exists(), f"Broken relative link in README.md: {target}"
