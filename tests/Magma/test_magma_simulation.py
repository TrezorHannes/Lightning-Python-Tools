import pytest
import os
import sys

# Ensure tests/ is importable
TESTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

from simulate_magma_sale import run_simulation


def test_simulation_timeout_auto_approval():
    """Verify end-to-end timeout auto-approval flow with unsettled fee fallback."""
    success = run_simulation(scenario="timeout", live_telegram=False, timeout_seconds=1)
    assert success is True


def test_simulation_manual_callback_approval():
    """Verify end-to-end inline button approval callback flow."""
    success = run_simulation(scenario="callback", live_telegram=False, timeout_seconds=1)
    assert success is True


def test_simulation_telegram_error_logging():
    """Verify TelegramLoggingHandler dispatches formatted error and traceback."""
    success = run_simulation(scenario="error", live_telegram=False, timeout_seconds=1)
    assert success is True


def test_simulation_full_suite():
    """Verify full simulation harness executes all scenarios cleanly."""
    success = run_simulation(scenario="all", live_telegram=False, timeout_seconds=1)
    assert success is True
