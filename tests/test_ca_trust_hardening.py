"""Regression tests for CA trust store hardening (eMudhra / certifi>=2026.10.19)."""

from __future__ import annotations

import re
from pathlib import Path

import certifi

from lpt_installer.registry import TOOLS
from lpt_installer.scheduler import (
    DEFAULT_CA_BUNDLE,
    render_cron_entry,
    render_systemd_service,
    upsert_cron_block,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _parse_version_tuple(version_str: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version_str.strip().split("."))


def _extract_min_version_tuple(spec_str: str) -> tuple[int, ...]:
    match = re.match(r"^(?:>=|==)\s*([0-9]+(?:\.[0-9]+)+)$", spec_str.strip())
    assert match, f"Expected '>=' or '==' version pin, got: {spec_str!r}"
    return _parse_version_tuple(match.group(1))


def test_requirements_pins_and_installed_hardened_tls_dependencies():
    """requirements.txt and active env must enforce certifi>=2026.10.19, requests>=2.34.2, urllib3>=2.8.0 with no duplicates."""
    req_path = REPO_ROOT / "requirements.txt"
    lines = [
        line.strip()
        for line in req_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    pkg_specs: dict[str, list[str]] = {}
    for line in lines:
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*(.*)$", line)
        assert match, f"Invalid requirement line: {line}"
        pkg_name = match.group(1).lower()
        pkg_specs.setdefault(pkg_name, []).append(match.group(2).strip())

    for pkg_name, specs in pkg_specs.items():
        assert len(specs) == 1, f"Duplicate requirement entry for {pkg_name}: {specs}"

    assert "certifi" in pkg_specs, "certifi must be explicitly pinned in requirements.txt"
    assert _extract_min_version_tuple(pkg_specs["certifi"][0]) >= (2026, 10, 19)
    assert _extract_min_version_tuple(pkg_specs["requests"][0]) >= (2, 34, 2)
    assert _extract_min_version_tuple(pkg_specs["urllib3"][0]) >= (2, 8, 0)

    installed = _parse_version_tuple(certifi.__version__)
    assert installed >= (2026, 10, 19), (
        f"Installed certifi {certifi.__version__} is vulnerable to compromised eMudhra CA roots; "
        "upgrade to certifi>=2026.10.19"
    )


def test_unified_systemd_and_cron_generators_export_os_ca_bundle():
    """Every systemd unit and managed cron block must bind SSL_CERT_FILE and REQUESTS_CA_BUNDLE."""
    for tool in TOOLS:
        for mode in tool.supported_modes:
            if mode not in ("systemd-service", "systemd-timer"):
                continue
            unit_text = render_systemd_service(
                tool,
                REPO_ROOT,
                user="admin",
                group="admin",
                ca_bundle=DEFAULT_CA_BUNDLE,
                mode=mode,
            )
            assert f'Environment="SSL_CERT_FILE={DEFAULT_CA_BUNDLE}"' in unit_text, (
                f"{tool.id} ({mode}) missing SSL_CERT_FILE systemd Environment directive"
            )
            assert f'Environment="REQUESTS_CA_BUNDLE={DEFAULT_CA_BUNDLE}"' in unit_text, (
                f"{tool.id} ({mode}) missing REQUESTS_CA_BUNDLE systemd Environment directive"
            )

    cron_tool = next(t for t in TOOLS if "cron" in t.supported_modes)
    cron_text = upsert_cron_block(
        "",
        {cron_tool.id: render_cron_entry(cron_tool, "0 * * * *", REPO_ROOT)},
        ca_bundle=DEFAULT_CA_BUNDLE,
    )
    assert f"SSL_CERT_FILE={DEFAULT_CA_BUNDLE}" in cron_text
    assert f"REQUESTS_CA_BUNDLE={DEFAULT_CA_BUNDLE}" in cron_text
