# Lightning Python Tools

[![Tests](https://github.com/TrezorHannes/Lightning-Python-Tools/actions/workflows/tests.yml/badge.svg)](https://github.com/TrezorHannes/Lightning-Python-Tools/actions/workflows/tests.yml)
[![Python 3.11 | 3.12 | 3.13](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](.github/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Security Policy](https://img.shields.io/badge/Security-Policy-blue.svg)](SECURITY.md)

A modular suite of automation scripts, background daemons, liquidity optimizers, and monitoring utilities for **LND** Lightning Network node operators.

---

## Table of Contents
- [Overview](#overview)
- [Architecture & Script Directory](#architecture--script-directory)
  - [1. Magma (Amboss Liquidity Market)](#1-magma-amboss-liquidity-market)
  - [2. LNDg Integration & Fee Management](#2-lndg-integration--fee-management)
  - [3. PeerSwap Automation](#3-peerswap-automation)
  - [4. Swaps, Fees & UTXO Management](#4-swaps-fees--utxo-management)
  - [5. LNbits & Micro-services](#5-lnbits--micro-services)
- [Prerequisites & Installation](#prerequisites--installation)
- [Configuration Guide](#configuration-guide)
- [Running as Background Services](#running-as-background-services)
  - [Automated Systemd Service Installers](#automated-systemd-service-installers)
  - [Systemd Daemon Example](#systemd-daemon-example)
  - [Crontab Example](#crontab-example)
- [Testing & Quality Assurance](#testing--quality-assurance)
- [Security & Vulnerability Reporting](#security--vulnerability-reporting)

---

## Overview

Operating a routing Lightning node benefits from consistent automation across liquidity pricing, channel policy tuning, and capital rebalancing:
- **Inbound Liquidity Sales**: Autonomous offer pricing, order approval, invoice creation, and channel opening on the Amboss Magma liquidity market.
- **Dynamic Fee Optimization**: Channel fee adjustments driven by Amboss network statistics, forward flow activity, liquidity bands, and stuck-channel decay.
- **Inbound Discount Protection & Rebalance Guard**: Locks `ar_out_target = 100%` on discounted channels to prevent circular rebalance loops from draining refilling liquidity, restoring targets via dynamic hysteresis once balances recover.
- **Submarine & Liquid Swaps**: Economically scored Loop Out swaps (`litloop` / `loopd`) with live prepay route probing, plus Boltz reverse swaps (Lightning to Liquid L-BTC).
- **PeerSwap Operations**: Peer eligibility tracking, LNDg telemetry sync, and interactive swap management over Telegram.

---

## Architecture & Script Directory

### 1. Magma (Amboss Liquidity Market)
Automate inbound liquidity sales on the Amboss Magma marketplace via the Magma GraphQL API (`https://magma.amboss.tech/graphql`).

| Script | Execution Mode | Purpose |
| :--- | :--- | :--- |
| [`Magma/magma_sale_process.py`](Magma/magma_sale_process.py) | `systemd service` | **Seller Auto-Pilot Daemon**: Monitors orders awaiting seller approval or channel open, validates on-chain UTXOs and mempool fee bounds, generates Lightning invoices via `lncli addinvoice`, supports interactive Telegram approval/timeout auto-approval, opens channels, confirms funding outpoints to Amboss, and auto-rejects banned peers. See [`Magma/magma_sale.md`](Magma/magma_sale.md). |
| [`Magma/magma_market_fee.py`](Magma/magma_market_fee.py) | `cronjob` / `CLI` | **Dynamic APR & Offer Optimizer**: Queries public Magma market offers, filters by seller score, computes percentile rates and annualized yield (APR) across offer templates in `Magma/magma_config.ini` ([example](Magma/magma_config.ini.example)), and creates, updates, or toggles sell offers (with `--dry-run` support). |

---

### 2. LNDg Integration & Fee Management
Synchronize marketplace metadata and automate channel policies against a local [LNDg](https://github.com/cryptosharks131/lndg) instance.

| Script | Execution Mode | Purpose |
| :--- | :--- | :--- |
| [`LNDg/amboss_pull.py`](LNDg/amboss_pull.py) | `cronjob` / `CLI` | Pulls active Magma sell orders from Amboss, writes maturity details into LNDg channel notes, toggles auto-fees/auto-rebalance upon lease expiration, and exports `charge-lnd` fee rules. |
| [`LNDg/mempool_rebalancer_trigger.py`](LNDg/mempool_rebalancer_trigger.py) | `systemd service` | Monitors half-hour mempool fee rates (`sat/vB`) and toggles LNDg `AR-Enabled`, pausing auto-rebalancing during high-fee spikes and re-enabling it when fees normalize. |
| [`LNDg/channel_base-fee.py`](LNDg/channel_base-fee.py) | `cronjob` / `one-off` | Resets `base_fee` to `0` across open, active channels in LNDg (excluding pubkeys in `base_fee_ignore`). |
| [`LNDg/channel_fee-pull.py`](LNDg/channel_fee-pull.py) | `cronjob` / `CLI` | Exports open channel IDs currently set to `0 ppm` in LNDg to `~/.chargelnd/.config/0_fee.txt` for `charge-lnd` rule ingestion. |
| [`LNDg/disabled_fee-accelerator.py`](LNDg/disabled_fee-accelerator.py) | `cronjob` / `CLI` | Incrementally bumps `local_fee_rate` in LNDg for depleted peer groups so locally initiated channels disabled by `charge-lnd` at low liquidity can attract rebalance flow. |
| [`LNDg/swap_out-candidates.py`](LNDg/swap_out-candidates.py) | `CLI tool` | Lists high-local-balance, low-PPM channels from LNDg as swap-out candidates, with optional export to `data/low-fee-high-local.log` (`-e`) or `bos/tags.json` (`-b`). |
| [`LNDg/offline_summary.py`](LNDg/offline_summary.py) | `CLI tool` | Queries LNDg's PostgreSQL `gui_peerevents` table (`--month YYYY-MM`) and prints a summary table of average and total peer offline hours (requires optional `psycopg2` and `pandas`). |

---

### 3. PeerSwap Automation
Monitor and execute Layer-1 BTC and Liquid (L-BTC) swaps with connected peers. For bare-metal, RaspiBlitz, and MiniBolt installation instructions for `elementsd` and `peerswapd`, see [`Peerswap/README.md`](Peerswap/README.md).

| Script | Execution Mode | Purpose |
| :--- | :--- | :--- |
| [`Peerswap/peerswap-bot.py`](Peerswap/peerswap-bot.py) | `systemd service` | **PeerSwap Telegram Bot**: Interactive bot for checking L-BTC balances, listing eligible peers, initiating `swapin`/`swapout` operations, and inspecting swap status. |
| [`Peerswap/peerswap-lndg_push.py`](Peerswap/peerswap-lndg_push.py) | `cronjob` / `CLI` | Aggregates `pscli` swap counts, satoshi volumes, and paid fees per peer and writes them into LNDg channel notes. |
| [`Peerswap/ps_peers.py`](Peerswap/ps_peers.py) | `CLI tool` | Displays a terminal summary table of L-BTC balance, trusted PeerSwap peers, supported assets, channel balances, and local/remote fee rates. |

---

### 4. Swaps, Fees & UTXO Management
Manage dynamic fee curves, rebalance protection, submarine swaps, and on-chain UTXO consolidation.

| Script | Execution Mode | Purpose |
| :--- | :--- | :--- |
| [`Other/fee_adjuster.py`](Other/fee_adjuster.py) | `systemd timer` / `cronjob` | **Dynamic Fee Optimizer**: Adjusts outbound and inbound channel fees using Amboss Space peer fee metrics, 5-band liquidity curves, and stuck-channel step-downs, with optional `charge-lnd` rule export and inbound discount protection. See [`Other/fee_adjuster.md`](Other/fee_adjuster.md). |
| [`Other/rebalance_guard.py`](Other/rebalance_guard.py) | `cronjob` / `CLI tool` | **Standalone Rebalance Guard**: Audits all open LNDg channels across native Auto-Fees and `fee_adjuster.py`. Locks `ar_out_target = 100%` while an inbound discount is active and restores baseline targets via dynamic hysteresis once liquidity recovers (`--dry-run` supported). |
| [`Other/swap_out-loop.py`](Other/swap_out-loop.py) | `CLI tool` | **Economically Optimized Loop Out**: Scores candidate channels by net effective PPM (server fee + sweep fee + probed routing fee + opportunity cost), performs live fake-hash prepay probes via `lncli`, supports multi-channel MPP selection (`litloop` / `loopd`), interactive terminal UI, and native `loop_sqlite.db` history/CSV accounting. See [`Other/swap_out-loop.md`](Other/swap_out-loop.md). |
| [`Other/boltz_swap-out.py`](Other/boltz_swap-out.py) | `CLI tool` | Automates reverse submarine swaps (Lightning to Liquid L-BTC) via `boltzd` / `boltzcli`, selecting candidate outbound channels from LNDg with multi-batch payment retries and `--debug` dry-run mode. |
| [`Other/swap_wallet.py`](Other/swap_wallet.py) | `CLI tool` | Batches scheduled Lightning payouts to a Lightning Address via `bos send` through high-local-balance peers under configurable fee rate caps. |
| [`Other/lnd_utxo_consolidator.py`](Other/lnd_utxo_consolidator.py) | `CLI tool` | Interactive helper that inspects `lncli listunspent`, filters UTXOs by size and strategy, and prints a verified `lncli sendcoins` consolidation command for manual operator execution (never broadcasts directly). |

---

### 5. LNbits & Micro-services

| Script | Execution Mode | Purpose |
| :--- | :--- | :--- |
| [`LNBits/pocketmoney.py`](LNBits/pocketmoney.py) | `systemd timer` / `daemon` | Automated recurring satoshi allowances: converts fiat budgets (`USD`, `EUR`, `GBP`, etc.) via LNbits exchange rates, pays child wallets configured in `wallets.json`, and sends Telegram balance alerts (`--run-once` supported for systemd timers/cron). |

---

## Prerequisites & Installation

### System Requirements
- **Python**: `3.11`, `3.12`, or `3.13`
- **OS**: Linux (Debian, Ubuntu, RaspiBlitz / Raspberry Pi OS)
- **Core Node**: Running **LND** node (`lncli` accessible)
- **Optional Integrations** (depending on which scripts you use):
  - [LNDg](https://github.com/cryptosharks131/lndg) REST API (used by `LNDg/` scripts, `fee_adjuster.py`, `rebalance_guard.py`, `swap_out-loop.py`, and `boltz_swap-out.py`)
  - `bos` (Balance of Satoshis), `charge-lnd`, `litd` / `loopd` (`litloop`), `peerswapd` (`pscli`), `boltzd` (`boltzcli`)
  - Optional Python packages `psycopg2-binary` and `pandas` (only required for `LNDg/offline_summary.py` against a PostgreSQL LNDg database)

### Clone & Set Up Virtual Environment
```bash
git clone https://github.com/TrezorHannes/Lightning-Python-Tools.git
cd Lightning-Python-Tools

# Create and activate Python virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Upgrade pip and install runtime dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

---

## Configuration Guide

All local configuration files containing credentials or node-specific rules are ignored by Git (`.gitignore`). Copy the templates you need:

```bash
cp config.ini.example config.ini
cp Magma/magma_config.ini.example Magma/magma_config.ini   # Required for Magma/magma_market_fee.py
cp feeConfig.json.example feeConfig.json                   # Required for Other/fee_adjuster.py & Other/rebalance_guard.py
cp wallets.json.example wallets.json                       # Required for LNBits/pocketmoney.py
chmod 600 config.ini Magma/magma_config.ini feeConfig.json wallets.json
```

### Configuration Files Overview
1. **[`config.ini`](config.ini.example)** (shared core configuration):
   - `[credentials]`: Amboss API token (`amboss_authorization`) and LNDg login (`lndg_username`, `lndg_password`).
   - `[lndg]`: Local LNDg API URL (e.g., `http://localhost:8889`).
   - `[paths]` & `[system]`: Paths to `lncli`, `pscli`, `boltzcli`, `bos` (`full_path_bos`), `boltzd` TLS cert/macaroon, `charge_lnd_path`, and `log_level`.
   - `[lnd]` & `[loop]`: LND gRPC connection details and Loop Out (`litloop`, `loop_db_path`, confirmation target, liquidity ratios, probe worker count, and routing fee leeway).
   - `[magma]`: Invoice expiry, route hints, on-chain fee cap ratio, initial channel PPM, peer connection retries, and polling interval.
   - `[pubkey]` & `[no-swapout]`: Comma-separated pubkey exclusion lists (`base_fee_ignore`, `banned_magma_pubkeys`, `swapout_blacklist`).
   - `[telegram]`: Bot tokens (`magma_bot_token`, `peerswap_bot_token`, `lnbits_bot_token`) and authorized `telegram_user_id` / `chat_id`.
   - `[LNBits]` & `[Schedule]`: LNbits server URL (`base_url`, without `/api/v1`), parent wallet `admin_key`, and allowance recurrence (`daily` or `weekly`).
   - `[database]`: Optional PostgreSQL settings (`name`, `host`, `port`) for `LNDg/offline_summary.py`.
2. **[`feeConfig.json`](feeConfig.json.example)**: Peer groups (`sink`, `expensive`, etc.), Amboss fee bases (`median`/`mean`), 5-band liquidity discounts/premiums, stuck-channel intervals, and `inbound_protection` thresholds.
3. **[`Magma/magma_config.ini`](Magma/magma_config.ini.example)**: Auto-pricing toggle, LND on-chain balance allocation fraction, seller-score filter, target competitiveness percentile, and `[magma_offer_*]` channel size/duration/APR templates.
4. **[`wallets.json`](wallets.json.example)**: Child wallet IDs, invoice keys, and fiat allowance amounts/currencies for `LNBits/pocketmoney.py`.

---

## Running as Background Services

### Automated Systemd Service Installers
Helper scripts are included to generate and enable `systemd` oneshot services and timers (pre-configured with OS CA trust bundle environment variables):

```bash
# Install hourly fee_adjuster.service + fee_adjuster.timer
chmod +x Other/install_fee_adjuster_service.sh
sudo ./Other/install_fee_adjuster_service.sh

# Install scheduled pocketmoney.service + pocketmoney.timer (reads [Schedule] from config.ini)
chmod +x LNBits/install_pocketmoney_service.sh
sudo ./LNBits/install_pocketmoney_service.sh
```

### Systemd Daemon Example
For long-running daemons such as `Magma/magma_sale_process.py` (full guide in [`Magma/magma_sale.md`](Magma/magma_sale.md)), create `/etc/systemd/system/magma-sale.service`:

```ini
[Unit]
Description=Amboss Magma Channel Auto-Sale Service
After=network.target lnd.service

[Service]
Type=simple
User=admin
WorkingDirectory=/home/admin/Lightning-Python-Tools
Environment="SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"
Environment="REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt"
ExecStart=/home/admin/Lightning-Python-Tools/.venv/bin/python3 /home/admin/Lightning-Python-Tools/Magma/magma_sale_process.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now magma-sale.service
```

### Crontab Example
```cron
# Run dynamic fee adjuster every 30 minutes (if not using the systemd timer)
*/30 * * * * /home/admin/Lightning-Python-Tools/.venv/bin/python3 /home/admin/Lightning-Python-Tools/Other/fee_adjuster.py >> /home/admin/Lightning-Python-Tools/logs/cron_fee.log 2>&1

# Run standalone rebalance guard audit hourly
0 * * * * /home/admin/Lightning-Python-Tools/.venv/bin/python3 /home/admin/Lightning-Python-Tools/Other/rebalance_guard.py >> /home/admin/Lightning-Python-Tools/logs/cron_guard.log 2>&1

# Sync Amboss Magma channel orders to LNDg hourly
15 * * * * /home/admin/Lightning-Python-Tools/.venv/bin/python3 /home/admin/Lightning-Python-Tools/LNDg/amboss_pull.py >> /home/admin/Lightning-Python-Tools/logs/cron_amboss.log 2>&1
```

---

## Testing & Quality Assurance

Unit tests, Amboss GraphQL contract checks, CA trust hardening checks, and Magma sale lifecycle simulations are executed across Python `3.11`, `3.12`, and `3.13` in GitHub Actions.

```bash
source .venv/bin/activate

# Install test and coverage dependencies
pip install -r requirements-dev.txt

# Run the full test suite (configured via pyproject.toml)
pytest

# Run with module coverage report
pytest --cov=LNDg --cov=Magma --cov=Other --cov=Peerswap --cov=LNBits

# Run the standalone offline Magma buy-order lifecycle simulation
python3 tests/simulate_magma_sale.py
```

---

## Security & Vulnerability Reporting

For our responsible disclosure policy, supported versions, and contact channels, review [`SECURITY.md`](SECURITY.md).

- **Vulnerability Disclosure**: Report security vulnerabilities privately via [GitHub Security Advisories](https://github.com/TrezorHannes/Lightning-Python-Tools/security/advisories) or email `security@tunnelsats.com`.
- **Secrets Management**: Never commit `config.ini`, `feeConfig.json`, `wallets.json`, `.env`, or macaroon files to Git.
- **Permissions**: Restrict file permissions on sensitive config files (`chmod 600 config.ini`).
- **Least Privilege**: Use read-only or scoped API keys and LND macaroons wherever possible.
