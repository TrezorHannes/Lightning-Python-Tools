## Guidelines for the Fee Adjuster Script

This script automates the adjustment of channel fees based on network conditions, peer behavior,
and local liquidity using data from the Amboss API and LNDg API.

### Configuration Settings:
- base_adjustment_percentage: Percentage adjustment applied to the selected fee base (median, mean, etc.).
- group_adjustment_percentage: Additional adjustment based on node group membership.
- max_cap: Maximum allowed fee rate (in ppm).
- trend_sensitivity: Multiplier for the influence of Amboss fee trends on the adjustment.
- fee_base: Statistical measure from Amboss used as the fee calculation base ("median", "mean", etc.).
- groups: Node categories for applying differentiated strategies via group_adjustment_percentage.
- fee_bands: (Optional) Dynamic fee adjustments based on local liquidity.
  - enabled: true/false.
  - discount: Negative percentage adjustment for high local balance (80-100%).
  - premium: Positive percentage adjustment for low local balance (0-40%).
- stuck_channel_adjustment: (Optional) Gradually reduces fees for channels without recent forwards.
  - enabled: true/false.
  - stuck_time_period: Number of days defining one 'stuck period' interval (e.g., 7).
- inbound_protection: (Optional) Configures protection against unprofitable rebalance feedback loops.
  - enabled: true/false.
  - max_inbound_discount_ppm: Hard cap on negative inbound fee discounts (default: 250 ppm).
  - lock_ar_out_target_on_discount: Automatically sets ar_out_target = 100% when an inbound discount is active to prevent rebalance drainage.
  - default_restored_ar_out_target: Restores ar_out_target (e.g. 75%) once channel balance is restored and discount is removed.
  - restore_liquidity_threshold: Local liquidity ratio required before unlocking (default: 75.0%).

### Inbound Discount Protection & Rebalance Guard:
When inbound discounts are offered on depleted channels, `fee_adjuster.py` and `rebalance_guard.py` ensure:
1. Inbound discounts are capped at `max_inbound_discount_ppm` to prevent routing payments at net losses against exit channels.
2. The channel is locked (`ar_out_target = 100%`) while offering discounts so LNDg will not cannibalize refilling liquidity as a rebalance donor.
3. When local liquidity recovers and discounts are lifted, standard `ar_out_target` settings are restored.

### Standalone Rebalance Guard Tool:
`rebalance_guard.py` audits all open channels in LNDg across both native `af.py` and `fee_adjuster.py` channels:
```bash
# Dry run check
python3 Other/rebalance_guard.py --dry-run

# Live execution
python3 Other/rebalance_guard.py
```
Allows tailored fee strategies for nodes in specific categories (e.g., "sink", "expensive").

### Fee Bands:
Adjusts fees based on local balance ratio, dividing liquidity into 5 bands (0-20%, 20-40%, 40-60%, 60-80%, 80-100%). A graduated adjustment is applied between the configured discount (high local balance) and premium (low local balance). The premium is capped at the 20-40% liquidity band to avoid excessively high fees on nearly drained channels.

### Stuck Channel Adjustment:
This feature incrementally reduces fees for channels that haven't forwarded payments recently.
For each multiple of the `stuck_time_period` (in days) that a peer's channels have gone without an *outbound* forwarding, the fee band is moved down by one level (towards the maximum discount).
The adjustment is capped at moving down 4 bands (reaching the maximum discount band).
If an outbound forward is detected for any channel of the peer, the stuck adjustment is reset to 0 bands down.
This adjustment is automatically skipped if the aggregate local liquidity for the peer is below 20%, preventing discounts on heavily imbalanced channels needing rebalancing. The script queries the LNDg API to find the timestamp of the last outbound forward for the peer.

### Usage:
- Configure nodes and their settings in `feeConfig.json`.
- Run the script to automatically adjust fees based on configured settings.
- Requires a running LNDg instance for local channel details and fee updates.

### Test Suite:
New features and refactors are guarded by a suite of unit tests. To run them locally:

```bash
# Activate your venv first if not active
source .venv/bin/activate

# Install test dependencies
pip install -r requirements-dev.txt

# Run the tests (pytest auto-discovers tests in the current directory)
pytest
```

### Command Line Arguments:
- --debug: Enable detailed debug output, including stuck channel check results.

### Charge-lnd Details:
Configure charge-lnd to use the output file:
```ini
# charge-lnd.config
[🤖 FeeAdjuster Import]
strategy = use_config
config_file = file:///path/to/your/charge-lnd/.config/fee_adjuster.txt
```

### Installation:
Add to crontab or use the systemd installer script:
```bash
crontab -e
# Add the following line (adjust path as needed)
0 * * * * /path/Lightning-Python-Tools/.venv/bin/python3 /path/Lightning-Python-Tools/Other/fee_adjuster.py >/dev/null 2>&1
```