"""Regression tests for CA trust store hardening (eMudhra / certifi>=2026.10.19)."""

from pathlib import Path
import re

import certifi


REPO_ROOT = Path(__file__).resolve().parent.parent


def _parse_version_tuple(version_str: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version_str.strip().split("."))


def test_requirements_pins_hardened_tls_dependencies():
    """requirements.txt must pin certifi>=2026.10.19, requests>=2.34.2, urllib3>=2.8.0 with no duplicates."""
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
    assert pkg_specs["certifi"][0] == ">=2026.10.19"
    assert pkg_specs["requests"][0] == ">=2.34.2"
    assert pkg_specs["urllib3"][0] == ">=2.8.0"


def test_installed_certifi_meets_minimum_safe_version():
    """Active Python environment must run certifi >= 2026.10.19 (distrusts compromised eMudhra RSA roots)."""
    installed = _parse_version_tuple(certifi.__version__)
    assert installed >= (2026, 10, 19), (
        f"Installed certifi {certifi.__version__} is vulnerable to compromised eMudhra CA roots; "
        "upgrade to certifi>=2026.10.19"
    )


def test_systemd_installers_export_os_ca_bundle():
    """Systemd service installers must bind SSL_CERT_FILE and REQUESTS_CA_BUNDLE to the OS trust store."""
    installers = [
        REPO_ROOT / "LNBits" / "install_pocketmoney_service.sh",
        REPO_ROOT / "Other" / "install_fee_adjuster_service.sh",
    ]
    for script in installers:
        content = script.read_text(encoding="utf-8")
        assert 'Environment="SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"' in content, (
            f"{script.name} missing SSL_CERT_FILE systemd Environment directive"
        )
        assert 'Environment="REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt"' in content, (
            f"{script.name} missing REQUESTS_CA_BUNDLE systemd Environment directive"
        )
