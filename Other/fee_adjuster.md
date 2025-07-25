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
  - min_local_balance_for_stuck_discount: (Optional) If the peer's aggregate local balance ratio is below this threshold (e.g., 0.2 for 20%), the stuck discount will not be applied.
  - min_updates_for_discount: (Optional) If the channel's `num_updates` is below this threshold, the fee band discount will not be applied. This is useful to prevent applying a discount to a newly opened channel.

### Groups and group_adjustment_percentage:
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