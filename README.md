# Lightning Python Tools

[![Tests](https://github.com/TrezorHannes/Lightning-Python-Tools/actions/workflows/tests.yml/badge.svg)](https://github.com/TrezorHannes/Lightning-Python-Tools/actions/workflows/tests.yml)
[![Python 3.11 | 3.12 | 3.13](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](.github/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Security Policy](https://img.shields.io/badge/Security-Policy-blue.svg)](SECURITY.md)

A modular suite of automation scripts, background daemons, liquidity optimizers, and monitoring utilities for **LND** Lightning Network node operators and companion VPS hosts.

> [!WARNING]
> **Operator Safety Disclaimer**
> - **Verify before enabling:** Scripts tagged **FUNDS** or **FEES/POLICY** move mainnet Bitcoin/Lightning funds or modify live channel policies. Always inspect configurations, run `--dry-run` / `--debug` previews, and review source code yourself before enabling automation.
> - **No warranty / no liability:** Provided **"AS IS"** under the [MIT License](LICENSE). The open-source developer assumes **no liability** for lost funds, routing losses, or misconfigured nodes.
> - **Stay updated:** Watch this repository (Releases & Security Advisories) and monitor [`SECURITY.md`](SECURITY.md) regularly for vulnerability patches (`main` is the only supported branch).

---

## Quickstart

Use the unified installer (`./install.sh`) as your regular node operator user (never `root`). It manages `.venv` creation (`uv` fast-path or stdlib `venv`), copies and hardens config templates (`chmod 600`), validates placeholders, detects duplicate legacy units/cronjobs, and configures `systemd` services/timers or managed `cron` blocks:

```bash
git clone https://github.com/TrezorHannes/Lightning-Python-Tools.git
cd Lightning-Python-Tools
chmod +x install.sh

# Launch the interactive menu
./install.sh
```

Common non-interactive CLI commands:

```bash
# View all 19 scripts and current installation status
./install.sh list

# Run environment, TLS/CA trust, permission, and conflict diagnostics
./install.sh doctor

# Install scheduled tools non-interactively (after editing configs)
./install.sh install fee-adjuster pocketmoney --scheduler systemd --accept-disclaimer

# Cleanly remove managed systemd units / cron entries (preserves configs, data/, logs/)
./install.sh uninstall --all
```

---

## Script Directory

The table below is verified in CI against `lpt_installer/registry.py`:

<!-- BEGIN_LPT_SCRIPT_TABLE -->
| ID | Script | Risk | Modes | Hosts | Description |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `pocketmoney` | `LNBits/pocketmoney.py` | **FUNDS** | systemd-timer, cron, systemd-service | node, vps | Scheduled fiat-to-sat child allowances via LNbits + Telegram alerts |
| `magma-sale` | `Magma/magma_sale_process.py` | **FUNDS** | systemd-service | node | Auto-pilot daemon for Amboss Magma channel sale orders |
| `magma-market-fee` | `Magma/magma_market_fee.py` | **FEES/POLICY** | systemd-timer, cron, manual | node | Analyzes Magma offers and updates seller offer pricing/status |
| `fee-adjuster` | `Other/fee_adjuster.py` | **FEES/POLICY** | systemd-timer, cron, manual | node, vps | Dynamic 5-band + stuck-channel + inbound-discount fee optimizer |
| `rebalance-guard` | `Other/rebalance_guard.py` | **FEES/POLICY** | systemd-timer, cron, manual | node, vps | Locks ar_out_target=100% on discounted channels with hysteresis |
| `amboss-pull` | `LNDg/amboss_pull.py` | **FEES/POLICY** | systemd-timer, cron, manual | node, vps | Syncs Magma leases to LNDg notes/auto-fees and charge-lnd caps |
| `channel-base-fee` | `LNDg/channel_base-fee.py` | **FEES/POLICY** | systemd-timer, cron, manual | node, vps | Resets base_fee to 0 on open active LNDg channels |
| `channel-fee-pull` | `LNDg/channel_fee-pull.py` | **FEES/POLICY** | systemd-timer, cron, manual | node | Exports 0 ppm open channel IDs to ~/.chargelnd/.config/0_fee.txt |
| `disabled-fee-accelerator` | `LNDg/disabled_fee-accelerator.py` | **FEES/POLICY** | systemd-timer, cron, manual | node, vps | Bumps local_fee_rate on depleted channels disabled by charge-lnd |
| `mempool-rebalancer-trigger` | `LNDg/mempool_rebalancer_trigger.py` | **FEES/POLICY** | systemd-service | node, vps | Polls mempool.space and toggles LNDg AR-Enabled on fee spikes |
| `offline-summary` | `LNDg/offline_summary.py` | **READ-ONLY** | manual | node | Monthly peer offline-duration report from LNDg PostgreSQL |
| `swap-out-candidates` | `LNDg/swap_out-candidates.py` | **READ-ONLY** | manual, systemd-timer, cron | node | Lists high-local low-PPM swap-out candidates (+ optional bos export) |
| `swap-out-loop` | `Other/swap_out-loop.py` | **FUNDS** | manual | node | Economically scored Loop Out optimizer with live route probing |
| `boltz-swap-out` | `Other/boltz_swap-out.py` | **FUNDS** | manual | node | Reverse submarine swaps (LN -> Liquid L-BTC) via boltzcli |
| `swap-wallet` | `Other/swap_wallet.py` | **FUNDS** | manual | node | Interactive batched LN payments to Lightning Address via bos send |
| `lnd-utxo-consolidator` | `Other/lnd_utxo_consolidator.py` | **READ-ONLY** | manual | node | Interactive UTXO selector printing a lncli sendcoins command |
| `peerswap-bot` | `Peerswap/peerswap-bot.py` | **FUNDS** | systemd-service | node | Telegram bot daemon wrapping pscli swaps and L-BTC sends |
| `peerswap-lndg-push` | `Peerswap/peerswap-lndg_push.py` | **READ-ONLY** | systemd-timer, cron, manual | node | Pushes PeerSwap swap stats from pscli into LNDg channel notes |
| `ps-peers` | `Peerswap/ps_peers.py` | **READ-ONLY** | manual | node | Terminal table of L-BTC balance and PeerSwap peer channel policies |
<!-- END_LPT_SCRIPT_TABLE -->

### Deep-Dive Tool Guides
- [`Magma/magma_sale.md`](Magma/magma_sale.md) — Amboss Magma channel auto-sale daemon setup and Telegram workflow.
- [`Other/fee_adjuster.md`](Other/fee_adjuster.md) — Dynamic liquidity fee bands, stuck-channel decay, inbound discounts, and `charge-lnd` integration.
- [`Other/swap_out-loop.md`](Other/swap_out-loop.md) — Economically scored Loop Out optimizer, fake-hash route probing, and MPP selection.
- [`Peerswap/README.md`](Peerswap/README.md) — Bare-metal, RaspiBlitz, and MiniBolt setup for `elementsd` and `peerswapd`.

---

## Documentation

- [**Installation & Scheduling Guide**](docs/INSTALLATION.md) — Interactive & CLI usage, `uv`/`venv` environments, `systemd` vs `cron`, `node`/`vps` host profiles, legacy unit migration, and clean uninstall.
- [**Configuration Reference**](docs/CONFIGURATION.md) — Template setup (`chmod 600`), placeholder validation, and section-by-section reference for all 4 config files.
- [**Troubleshooting & Diagnostics**](docs/TROUBLESHOOTING.md) — `./install.sh doctor` checks, OS CA trust bindings, duplicate-execution conflicts, file ownership fixes, logs, and rollback snapshots.

---

## Testing & Quality Assurance

GitHub Actions runs `pytest` across Python `3.11`, `3.12`, and `3.13`, `ruff`, `shellcheck`, `systemd-analyze verify`, `pip-audit`, and coverage gates (`>= 85%` on `lpt_installer`, `>= 35%` repo-wide):

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt

# Run full test suite with coverage
pytest tests/ -v --cov=LNDg --cov=Magma --cov=Other --cov=Peerswap --cov=LNBits --cov=lpt_installer --cov-report=term

# Run installer & CA trust gates
pytest tests/test_lpt_installer.py tests/test_ca_trust_hardening.py --cov=lpt_installer --cov-fail-under=85

# Run static checks
ruff check lpt_installer tests/test_lpt_installer.py tests/test_p0_runtime_safety.py tests/test_ca_trust_hardening.py tests/test_repo_hygiene.py
shellcheck -S warning install.sh
pip-audit -r requirements.txt

# Standalone offline Magma lifecycle simulation
python3 tests/simulate_magma_sale.py
```

---

## Security & Vulnerability Reporting

Review [`SECURITY.md`](SECURITY.md) for responsible disclosure channels, supported versions (`main` only), and operator hardening practices (`chmod 600` on configs, scoped macaroons, OS CA trust bindings via `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE`, and `certifi>=2026.11.20`).

---

## Support / Donations

If Lightning Python Tools helps you operate your node, optional donation methods are listed at [hodlmetight.org](https://hodlmetight.org).
