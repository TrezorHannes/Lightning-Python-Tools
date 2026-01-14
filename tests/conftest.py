import pytest

@pytest.fixture
def fee_conditions():
    """Returns a sample fee_conditions dictionary."""
    return {
        "fee_bands": {
            "enabled": True,
            "discount": -0.15,
            "premium": 0.40
        },
        "stuck_channel_adjustment": {
            "enabled": True,
            "stuck_time_period": 5,
            "min_local_balance_for_stuck_discount": 0.1,
            "min_updates_for_discount": 100
        }
    }
