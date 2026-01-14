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
    
