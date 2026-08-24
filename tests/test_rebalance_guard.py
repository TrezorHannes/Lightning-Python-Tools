import pytest
from rebalance_guard import audit_channel_rebalance_targets, evaluate_channel_action

def test_evaluate_channel_action_locks_discounted_channel():
    """
    If a channel has a negative inbound fee and ar_out_target < 95%,
    it must be flagged to be locked to 100%.
    """
    channel = {
        "chan_id": "1026788829343318018",
        "alias": "Garlic🧄",
        "local_inbound_fee_rate": -410,
        "ar_out_target": 35,
        "local_balance": 1704000,
        "capacity": 3000000,
        "auto_rebalance": False
    }
    action, target = evaluate_channel_action(channel, lock_target=100, restore_target=75, restore_liquidity_threshold=75.0)
    assert action == "LOCK"
    assert target == 100

def test_evaluate_channel_action_restores_balanced_channel():
    """
    If a channel has no inbound discount (>=0), local liquidity >= 75%,
    and its ar_out_target is currently locked at 100%, it should be restored.
    """
    channel = {
        "chan_id": "1046355738178945025",
        "alias": "Volarte⚡",
        "local_inbound_fee_rate": 0,
        "ar_out_target": 100,
        "local_balance": 4900000,
        "capacity": 5000000, # 98%
        "auto_rebalance": False
    }
    action, target = evaluate_channel_action(channel, lock_target=100, restore_target=75, restore_liquidity_threshold=75.0)
    assert action == "RESTORE"
    assert target == 75

def test_evaluate_channel_action_no_op_for_healthy_channel():
    """
    If a channel has normal settings and no discount, no action is taken.
    """
    channel = {
        "chan_id": "996248794333052929",
        "alias": "Play-asia.com",
        "local_inbound_fee_rate": 0,
        "ar_out_target": 65,
        "local_balance": 4500000,
        "capacity": 5000000,
        "auto_rebalance": False
    }
    action, target = evaluate_channel_action(channel, lock_target=100, restore_target=75, restore_liquidity_threshold=75.0)
    assert action == "NOOP"
    assert target is None

def test_audit_channel_rebalance_targets():
    """
    Test auditing a batch of channels.
    """
    channels = [
        {
            "chan_id": "1",
            "alias": "Garlic🧄",
            "local_inbound_fee_rate": -410,
            "ar_out_target": 35,
            "local_balance": 1704000,
            "capacity": 3000000,
            "auto_rebalance": False,
            "is_open": True
        },
        {
            "chan_id": "2",
            "alias": "HealthyNode",
            "local_inbound_fee_rate": 0,
            "ar_out_target": 65,
            "local_balance": 4500000,
            "capacity": 5000000,
            "auto_rebalance": False,
            "is_open": True
        },
        {
            "chan_id": "3",
            "alias": "RestorableNode",
            "local_inbound_fee_rate": 0,
            "ar_out_target": 100,
            "local_balance": 4800000,
            "capacity": 5000000,
            "auto_rebalance": False,
            "is_open": True
        }
    ]
    plans = audit_channel_rebalance_targets(channels, lock_target=100, restore_target=75)
    assert len(plans) == 2
    assert plans[0]["chan_id"] == "1"
    assert plans[0]["action"] == "LOCK"
    assert plans[0]["new_target"] == 100

    assert plans[1]["chan_id"] == "3"
    assert plans[1]["action"] == "RESTORE"
    assert plans[1]["new_target"] == 75


def test_evaluate_channel_action_restores_custom_channel_baseline():
    """
    Test that when a baseline map contains a custom target (e.g. 35 for Garlic),
    it restores to 35 instead of generic default 75.
    """
    channel = {
        "chan_id": "1026788829343318018",
        "alias": "Garlic🧄",
        "local_inbound_fee_rate": 0,
        "ar_out_target": 100,
        "local_balance": 2700000,
        "capacity": 3000000, # 90%
        "auto_rebalance": False
    }
    baseline_map = {
        "1026788829343318018": 35
    }
    action, target = evaluate_channel_action(
        channel,
        lock_target=100,
        restore_target=75,
        restore_liquidity_threshold=75.0,
        baseline_map=baseline_map
    )
    assert action == "RESTORE"
    assert target == 35

