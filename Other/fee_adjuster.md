# Guidelines for the Fee Adjuster Script (`Other/fee_adjuster.py`)

`Other/fee_adjuster.py` automates outbound and inbound channel fee adjustments based on Amboss Space network statistics, forward activity, and local liquidity balances via the Amboss GraphQL API and LNDg REST API.

## Configuration (`feeConfig.json`)

Copy `feeConfig.json.example` to `feeConfig.json` in the repository root:

- `Terminal_output`: Print adjustment summary tables to stdout (`true`/`false`).
- `LNDg_fee_update`: Push calculated outbound/inbound fees directly to the LNDg API (`true`/`false`).
- `write_charge_lnd_file`: Export rule stanzas to `<charge_lnd_path>/fee_adjuster.txt` (`true`/`false`).
- `update_channel_notes`: Write fee calculation summaries into LNDg channel notes (`true`/`false`).
- `inbound_protection`: Protects channels from unprofitable circular rebalance loops:
  - `lock_ar_out_target_on_discount`: Locks `ar_out_target` to `lock_target` whenever a negative inbound fee discount is active (`default: true`).
  - `lock_target`: Target outbound auto-rebalance percentage applied while locked (`default: 100`).
  - `default_restored_ar_out_target`: Fallback `ar_out_target` restored if no prior baseline was recorded (`default: 75`).
  - `restore_liquidity_threshold`: Minimum local liquidity percentage required before unlocking (`default: 75.0`).
  - `max_inbound_discount_ppm`: Optional cap on negative inbound fee discounts in ppm (`null` by default for uncapped, or set e.g. `250` to cap at `-250 ppm`).
- `groups`: Named peer strategy profiles (for example `sink`, `expensive`) defining:
  - `group_adjustment_percentage`: Multiplier offset applied to the selected `fee_base`.
  - `max_cap`: Maximum allowed outbound fee rate in ppm.
  - `trend_sensitivity`: Multiplier weighting Amboss fee trend deltas.
  - `fee_base`: Statistical measure from Amboss (`"median"`, `"mean"`, etc.).
  - `fee_delta_threshold`: Minimum change in ppm required before pushing an update to LNDg.
  - `inbound_auto_fee_enabled`: Enables dynamic negative inbound fee discounts scaled by LNDg `ar_max_cost` in low-liquidity bands (`true`/`false`).
  - `fee_bands`: Liquidity-based fee adjustments across 5 balance bands (`0-20%`, `20-40%`, `40-60%`, `60-80%`, `80-100%`):
    - `enabled`: `true`/`false`.
    - `discount`: Negative percentage adjustment for high local balance (`80-100%`).
    - `premium`: Positive percentage adjustment for low local balance (capped at the `20-40%` band).
  - `stuck_channel_adjustment`: Progressively steps down the liquidity band for channels without recent outbound forwards:
    - `enabled`: `true`/`false`.
    - `stuck_time_period`: Days without an outbound forward per 1-band step-down (capped at 4 bands).
    - `min_local_balance_for_stuck_discount`: Minimum aggregate peer local liquidity ratio (for example `0.2` for `20%`) required before applying stuck discounts.
    - `min_updates_for_discount`: Minimum channel `num_updates` threshold to avoid discounting newly opened channels.
- `nodes`: Per-peer list mapping `pubkey` to a `group` (with optional per-node overrides or custom `fee_conditions`).

## Inbound Discount Protection & Standalone Rebalance Guard

When inbound discounts are active on depleted channels, `Other/fee_adjuster.py` and `Other/rebalance_guard.py` coordinate to prevent rebalance cannibalization:
1. Optional `max_inbound_discount_ppm` caps negative inbound discounts so transit forwards do not route at a net loss.
2. Discounted channels are locked (`ar_out_target = 100%`), recording their prior baseline target in `data/rebalance_targets_baseline.json`.
3. Once local liquidity recovers to `restore_liquidity_threshold` and the inbound discount is lifted, the channel's baseline `ar_out_target` is automatically restored.

Run the standalone guard across all open LNDg channels (covering both native LNDg Auto-Fees and `fee_adjuster.py`):
```bash
# Dry-run audit
.venv/bin/python3 Other/rebalance_guard.py --dry-run

# Live execution
.venv/bin/python3 Other/rebalance_guard.py
```

## Command-Line Arguments

- `--debug`: Prints detailed fee and stuck-channel diagnostics while skipping live LNDg API mutations and `charge-lnd` file writes.

## `charge-lnd` Integration

Ensure `charge_lnd_path` is set under `[paths]` in `config.ini` (defaults to `~/.chargelnd/.config`), and reference the generated file in `charge-lnd.config`:
```ini
[FeeAdjuster Import]
strategy = use_config
config_file = file:///home/admin/.chargelnd/.config/fee_adjuster.txt
```

## Installation (systemd Timer or Crontab)

Use the included installer script to set up `fee_adjuster.service` and an hourly `fee_adjuster.timer`:
```bash
chmod +x Other/install_fee_adjuster_service.sh
sudo ./Other/install_fee_adjuster_service.sh
```

Or schedule via `crontab -e`:
```cron
0 * * * * /path/to/Lightning-Python-Tools/.venv/bin/python3 /path/to/Lightning-Python-Tools/Other/fee_adjuster.py >> /path/to/Lightning-Python-Tools/logs/cron_fee.log 2>&1
```

## Running Tests

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest tests/test_fee_adjuster.py tests/test_rebalance_guard.py -v
```
