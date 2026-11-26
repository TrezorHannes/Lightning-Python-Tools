"""Unified installer and manager for Lightning-Python-Tools (pure Python stdlib)."""

from __future__ import annotations

VERSION = "1.0.0"
DISCLAIMER_VERSION = "2026-11"
DONATION_URL = "https://hodlmetight.org"

DISCLAIMER_TEXT = (
    "WARNING / OPERATOR DISCLAIMER\n"
    "1. Self-Verify Before Running: Several scripts move mainnet Bitcoin/Lightning funds\n"
    "   or modify live channel fee policies. Verify configs, run --dry-run / --debug\n"
    "   previews, and inspect the source code before enabling automation.\n"
    "2. No Warranty / No Liability: This software is provided 'AS IS' under the MIT\n"
    "   License without warranty of any kind. The open-source developer accepts no\n"
    "   liability for lost funds, routing losses, or misconfigured nodes.\n"
    "3. Stay Updated: Watch the GitHub repository (Releases & Security Advisories) and\n"
    "   review SECURITY.md regularly for vulnerability patches (main is supported)."
)

DONATION_FOOTER = (
    f"Enjoying Lightning-Python-Tools? Support open-source development: {DONATION_URL}"
)

__all__ = [
    "VERSION",
    "DISCLAIMER_VERSION",
    "DONATION_URL",
    "DISCLAIMER_TEXT",
    "DONATION_FOOTER",
]
