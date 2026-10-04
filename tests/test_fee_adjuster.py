import sys
import os
import pytest

import pytest

from fee_adjuster import calculate_fee_band_adjustment

def test_high_liquidity_not_stuck_no_discount(fee_conditions):
    """
    Test that a channel with high local liquidity (Band 0) that is NOT stuck
    does NOT receive a discount.
    """
    # 90% outbound ratio => Band 0 (Initial)
    outbound_ratio = 0.90
    num_updates = 200 # Sufficient updates
    stuck_bands_to_move_down = 0 # Not stuck
    
    adj_factor, init_band, final_band = calculate_fee_band_adjustment(
        fee_conditions, 
        outbound_ratio, 
        num_updates, 
        stuck_bands_to_move_down
    )
    
    # Expectation: 
    # initial_raw_band = 0
    # adjusted_raw_band = 0
    # calculated_adjustment = -0.15 (discount)
    # BUT is_channel_stuck is False, so adjustment should become 0
    
    assert init_band == 0
    assert final_band == 0
    assert adj_factor == 1.0 # 1 + 0
    
def test_high_liquidity_stuck_receives_discount(fee_conditions):
    """
    Test that a channel with high local liquidity that IS stuck 
    receives the discount.
    """
    outbound_ratio = 0.90
    num_updates = 200
    stuck_bands_to_move_down = 1 # Stuck for at least one period
    
    adj_factor, _, _ = calculate_fee_band_adjustment(
        fee_conditions, 
        outbound_ratio, 
        num_updates, 
        stuck_bands_to_move_down
    )
    
    # Expectation: Discount applied.
    # adjustable_raw_band = 0
    # adjustment = -0.15
    # Factor = 0.85
    
    assert adj_factor == 0.85

def test_new_channel_guard_stuck_but_low_updates(fee_conditions):
    """
    Test that a stuck channel with insufficient updates still gets NO discount
    (legacy safeguard check).
    """
    outbound_ratio = 0.90
    num_updates = 50 # < 100
    stuck_bands_to_move_down = 1 # Stuck
    
    adj_factor, _, _ = calculate_fee_band_adjustment(
        fee_conditions, 
        outbound_ratio, 
        num_updates, 
        stuck_bands_to_move_down
    )
    
    # Expectation: 
    # Condition: (not is_channel_stuck or num_updates < min_updates)
    # (False or True) -> True.
    # Adjustment -> 0
    
    assert adj_factor == 1.0

def test_premium_applied_regardless_of_stuck(fee_conditions):
    """
    Test that premiums are applied for low liquidity channels regardless of stuck status.
    """
    # 10% outbound ratio => Band 4 (0-20%) -> capped at Band 3 effective logic
    outbound_ratio = 0.10 
    num_updates = 200
    stuck_bands_to_move_down = 0
    
    adj_factor, init_band, final_band = calculate_fee_band_adjustment(
        fee_conditions, 
        outbound_ratio, 
        num_updates, 
        stuck_bands_to_move_down
    )
    
    # Expectation:
    # initial_raw_band = 4
    # adjusted_raw_band = 4
    # effective_band_for_calc = 3
    # adjustment = discount + 3 * (range/3) = premium = 0.40
    # Factor = 1.40
    
    assert init_band == 4
    assert adj_factor == 1.40


def test_inbound_fee_discount_without_cap():
    """
    Test standard inbound fee discount calculation without max cap.
    Band 4 (0-20% local), Outbound Fee = 2000 ppm, ar_max_cost = 75%.
    Expected raw discount = -round(2000 * 0.75 * 0.90) = -1350 ppm.
    """
    from fee_adjuster import calculate_inbound_fee_discount_ppm
    discount = calculate_inbound_fee_discount_ppm(
        calculated_final_outgoing_fee_ppm=2000,
        initial_raw_band=4,
        ar_max_cost_percent=75,
        max_inbound_discount_ppm=None
    )
    assert discount == -1350


def test_inbound_fee_discount_with_max_cap():
    """
    Test inbound fee discount calculation with max_inbound_discount_ppm cap.
    Raw discount would be -1350 ppm, but with max_inbound_discount_ppm = 250,
    it must be clamped to -250 ppm.
    """
    from fee_adjuster import calculate_inbound_fee_discount_ppm
    discount = calculate_inbound_fee_discount_ppm(
        calculated_final_outgoing_fee_ppm=2000,
        initial_raw_band=4,
        ar_max_cost_percent=75,
        max_inbound_discount_ppm=250
    )
    assert discount == -250


def test_inbound_fee_discount_within_cap_unchanged():
    """
    Test that discounts smaller than max cap are preserved.
    Band 2 (40-60% local), Outbound Fee = 300 ppm, ar_max_cost = 50%.
    Raw discount = -round(300 * 0.50 * 0.20) = -30 ppm.
    With cap of 250 ppm, discount must remain -30 ppm.
    """
    from fee_adjuster import calculate_inbound_fee_discount_ppm
    discount = calculate_inbound_fee_discount_ppm(
        calculated_final_outgoing_fee_ppm=300,
        initial_raw_band=2,
        ar_max_cost_percent=50,
        max_inbound_discount_ppm=250
    )
    assert discount == -30


def test_inbound_fee_discount_high_liquidity_is_zero():
    """
    Test that Band 0 and Band 1 (high local liquidity) never receive inbound discounts.
    """
    from fee_adjuster import calculate_inbound_fee_discount_ppm
    assert calculate_inbound_fee_discount_ppm(2000, 0, 75, 250) == 0
    assert calculate_inbound_fee_discount_ppm(2000, 1, 75, 250) == 0


def test_determine_ar_out_target_update_locks_on_discount():
    """
    Test that when a negative inbound fee is set and current ar_out_target < 100,
    the target is locked to 100% to prevent rebalancer drain.
    """
    from fee_adjuster import determine_ar_out_target_update
    channel_data = {
        "ar_out_target": 45,
        "local_balance_ratio": 15.0
    }
    inbound_protection = {
        "enabled": True,
        "lock_ar_out_target_on_discount": True,
        "default_restored_ar_out_target": 75
    }
    new_target = determine_ar_out_target_update(channel_data, new_inbound_fee_ppm=-250, inbound_protection_config=inbound_protection)
    assert new_target == 100


def test_determine_ar_out_target_update_restores_when_balanced():
    """
    Test that when inbound discount is removed and local balance is high (>= 90% for baseline 75),
    a locked ar_out_target (100) is restored to its recorded baseline.
    """
    from fee_adjuster import determine_ar_out_target_update
    channel_data = {
        "ar_out_target": 100,
        "local_balance_ratio": 92.0
    }
    inbound_protection = {
        "lock_ar_out_target_on_discount": True,
        "default_restored_ar_out_target": 75
    }
    baseline_map = {"chan_123": 75}
    new_target = determine_ar_out_target_update(
        channel_data,
        new_inbound_fee_ppm=0,
        inbound_protection_config=inbound_protection,
        baseline_map=baseline_map,
        chan_id="chan_123"
    )
    assert new_target == 75


def test_determine_ar_out_target_update_ignores_unmanaged_100():
    """
    Test that an intentionally 100% channel (e.g. bfx-lnd0) without a lower baseline is not modified.
    """
    from fee_adjuster import determine_ar_out_target_update
    channel_data = {
        "ar_out_target": 100,
        "local_balance_ratio": 85.0
    }
    inbound_protection = {
        "lock_ar_out_target_on_discount": True,
        "default_restored_ar_out_target": 75
    }
    new_target = determine_ar_out_target_update(
        channel_data,
        new_inbound_fee_ppm=0,
        inbound_protection_config=inbound_protection,
        baseline_map={},
        chan_id="bfx_0"
    )
    assert new_target is None



def test_determine_ar_out_target_update_no_change_needed():
    """
    Test that if channel already has appropriate target, no update is requested (returns None).
    """
    from fee_adjuster import determine_ar_out_target_update
    channel_data = {
        "ar_out_target": 100,
        "local_balance_ratio": 15.0
    }
    inbound_protection = {
        "enabled": True,
        "lock_ar_out_target_on_discount": True,
        "default_restored_ar_out_target": 75
    }
    # Already 100 while discounted -> None
    assert determine_ar_out_target_update(channel_data, new_inbound_fee_ppm=-250, inbound_protection_config=inbound_protection) is None


def test_determine_ar_out_target_update_disabled_lock():
    """
    Test that if lock_ar_out_target_on_discount is False, no locking action is taken.
    """
    from fee_adjuster import determine_ar_out_target_update
    channel_data = {
        "ar_out_target": 45,
        "local_balance_ratio": 15.0
    }
    inbound_protection = {
        "lock_ar_out_target_on_discount": False,
        "default_restored_ar_out_target": 75
    }
    assert determine_ar_out_target_update(channel_data, new_inbound_fee_ppm=-250, inbound_protection_config=inbound_protection) is None


def test_determine_ar_out_target_update_restores_custom_baseline():
    """
    Test that when a channel has a saved baseline in baseline_map (e.g. 45%),
    it restores to 45% instead of generic default (e.g. 75%).
    """
    from fee_adjuster import determine_ar_out_target_update
    channel_data = {
        "ar_out_target": 100,
        "local_balance_ratio": 85.0
    }
    inbound_protection = {
        "lock_ar_out_target_on_discount": True,
        "default_restored_ar_out_target": 75
    }
    baseline_map = {
        "1026788829343318018": 45
    }
    new_target = determine_ar_out_target_update(
        channel_data,
        new_inbound_fee_ppm=0,
        inbound_protection_config=inbound_protection,
        baseline_map=baseline_map,
        chan_id="1026788829343318018"
    )
    assert new_target == 45


def test_fetch_amboss_data_success_and_timeout(monkeypatch):
    """
    Verifies fetch_amboss_data passes timeout=20 and extracts remote fee info across time ranges.
    """
    import requests
    from fee_adjuster import fetch_amboss_data

    recorded_calls = []

    class DummyResponse:
        status_code = 200
        text = '{"data": {}}'

        def __init__(self, time_range):
            self._time_range = time_range

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "data": {
                    "getNode": {
                        "graph_info": {
                            "channels": {
                                "fee_info": {
                                    "remote": {
                                        "max": "1500",
                                        "mean": "350",
                                        "median": "250" if self._time_range == "TODAY" else "200",
                                        "weighted": "275",
                                        "weighted_corrected": "260",
                                    }
                                }
                            }
                        }
                    }
                }
            }

    def fake_post(url, json=None, headers=None, timeout=None):
        recorded_calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return DummyResponse(json["variables"]["timeRange"])

    monkeypatch.setattr(requests, "post", fake_post)
    config = {"credentials": {"amboss_authorization": "test-token"}}

    result = fetch_amboss_data("02abc", config, time_ranges=["TODAY", "ONE_DAY"])

    assert len(recorded_calls) == 2
    assert all(call["timeout"] == 20 for call in recorded_calls)
    assert result["TODAY"]["median"] == "250"
    assert result["ONE_DAY"]["median"] == "200"


def test_fetch_amboss_data_handles_null_fee_info_or_remote_gracefully(monkeypatch):
    """
    Verifies that when channels, fee_info, or remote is None for a time range,
    fetch_amboss_data returns {} for that time range instead of raising TypeError,
    and raises AmbossAPIError when getNode is None.
    """
    import requests
    from fee_adjuster import fetch_amboss_data, AmbossAPIError

    payload_by_range = {
        "TODAY": {"data": {"getNode": {"graph_info": {"channels": None}}}},
        "ONE_DAY": {"data": {"getNode": {"graph_info": {"channels": {"fee_info": None}}}}},
        "ONE_WEEK": {"data": {"getNode": {"graph_info": {"channels": {"fee_info": {"remote": None}}}}}},
        "ONE_MONTH": {"data": {"getNode": {"graph_info": None}}},
    }

    class DummyResponse:
        status_code = 200
        text = "{}"

        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            pass

        def json(self):
            return self._body

    monkeypatch.setattr(
        requests,
        "post",
        lambda url, json=None, headers=None, timeout=None: DummyResponse(
            payload_by_range[json["variables"]["timeRange"]]
        ),
    )
    config = {"credentials": {"amboss_authorization": "test-token"}}

    result = fetch_amboss_data(
        "02abc", config, time_ranges=["TODAY", "ONE_DAY", "ONE_WEEK", "ONE_MONTH"]
    )
    assert result == {
        "TODAY": {},
        "ONE_DAY": {},
        "ONE_WEEK": {},
        "ONE_MONTH": {},
    }

    # Also verify getNode=None raises AmbossAPIError rather than TypeError
    monkeypatch.setattr(
        requests,
        "post",
        lambda url, json=None, headers=None, timeout=None: DummyResponse(
            {"data": {"getNode": None}}
        ),
    )
    with pytest.raises(AmbossAPIError, match="Missing getNode data"):
        fetch_amboss_data("02abc", config, time_ranges=["TODAY"])


def test_fetch_amboss_data_raises_amboss_api_error_on_http_400_with_body(monkeypatch, caplog):
    """
    Verifies that an HTTP 400 GRAPHQL_VALIDATION_FAILED response logs the GraphQL error
    message and raises AmbossAPIError with the response details.
    """
    import logging
    import requests
    from fee_adjuster import fetch_amboss_data, AmbossAPIError

    error_body = (
        '{"errors":[{"message":"Cannot query field \\"bad_field\\" on type \\"FeeInfo\\".",'
        '"extensions":{"code":"GRAPHQL_VALIDATION_FAILED"}}]}'
    )

    class Dummy400Response:
        status_code = 400
        text = error_body

        def raise_for_status(self):
            raise requests.exceptions.HTTPError("400 Client Error: Bad Request")

        def json(self):
            import json
            return json.loads(error_body)

    monkeypatch.setattr(
        requests,
        "post",
        lambda url, json=None, headers=None, timeout=None: Dummy400Response(),
    )
    config = {"credentials": {"amboss_authorization": "test-token"}}

    with caplog.at_level(logging.ERROR):
        with pytest.raises(AmbossAPIError) as exc_info:
            fetch_amboss_data("02abc", config, time_ranges=["TODAY"])

    assert "GRAPHQL_VALIDATION_FAILED" in str(exc_info.value)
    assert any("GRAPHQL_VALIDATION_FAILED" in rec.message for rec in caplog.records)
