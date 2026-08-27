# ⚡ Lightning Python Tools

A comprehensive suite of production-grade automation scripts, background daemons, liquidity optimizers, and monitoring utilities for **LND** Lightning Network node operators.

---

## 📑 Table of Contents
- [Overview](#-overview)
- [Architecture & Script Directory](#-architecture--script-directory)
  - [1. Magma (Amboss Liquidity Market)](#1-magma-amboss-liquidity-market)
  - [2. LNDg Integration & Fee Management](#2-lndg-integration--fee-management)
  - [3. PeerSwap Automation](#3-peerswap-automation)
  - [4. Swaps & Capital Management](#4-swaps--capital-management)
  - [5. LNbits & Micro-services](#5-lnbits--micro-services)
- [Prerequisites & Installation](#-prerequisites--installation)
- [Configuration Guide](#-configuration-guide)
- [Running as Background Services](#-running-as-background-services)
  - [Systemd Daemon Example](#systemd-daemon-example)
  - [Crontab Example](#crontab-example)
- [Testing & Quality Assurance](#-testing--quality-assurance)
- [Security Best Practices](#-security-best-practices)

---

## 🌟 Overview

Operating an institutional or routing Lightning node requires robust, automated tooling across multiple dimensions:
- **Inbound Liquidity Auctioning**: Autonomous pricing and clearance on the Amboss Magma liquidity market.
- **Dynamic Fee Optimization**: Real-time channel fee adjustment based on remote market statistics, flow metrics, and stuck liquidity.
- **Inbound Discount Protection & Rebalance Guard**: Prevents circular rebalance loops from draining channels with active fee discounts.
- **Submarine & Liquid Swaps**: Programmatic balance management across on-chain, Liquid (L-BTC), and Lightning.
- **Automated PeerSwap Operations**: Triggering rebalances and swaps based on live peer flow.

---

## 🛠️ Architecture & Script Directory

### 1. Magma (Amboss Liquidity Market)
Automate your inbound liquidity sales on the Amboss Magma marketplace using the modern Magma GraphQL API (`https://magma.amboss.tech/graphql`).

| Script | Execution Mode | Purpose |
| :--- | :--- | :--- |
| [`Magma/magma_sale_process.py`](Magma/magma_sale_process.py) | `systemd service` | **Seller Auto-Pilot Daemon**: Listens for orders awaiting seller approval or channel open, checks UTXO and mempool fees, creates pre-image HODL invoices, opens channels, confirms funding transactions to Amboss, and auto-rejects banned peers. |
| [`Magma/magma_market_fee.py`](Magma/magma_market_fee.py) | `cronjob` / `service` | **Dynamic APR & Fee Synchronizer**: Scrapes active Magma public market offers, analyzes percentile rates, computes APR across timeframes (30d/60d/90d), and updates or toggles your Magma sell offers. Supports dry-run simulations. |

---

### 2. LNDg Integration & Fee Management
Synchronize metrics with your local LNDg dashboard and automate channel policies.

| Script | Execution Mode | Purpose |
| :--- | :--- | :--- |
| [`LNDg/amboss_pull.py`](LNDg/amboss_pull.py) | `cronjob` / `one-off` | Pulls Magma orders from Amboss, writes channel details into LNDg GUI, and configures auto-fee triggers upon channel maturity. |
| [`LNDg/mempool_rebalancer_trigger.py`](LNDg/mempool_rebalancer_trigger.py) | `systemd service` | Monitors live mempool block fee rates; triggers LNDg rebalancer when fees dip below user-defined thresholds. |
| [`LNDg/channel_base-fee.py`](LNDg/channel_base-fee.py) | `cronjob` / `one-off` | Normalizes or updates base fees across all open channels in LNDg. |
| [`LNDg/channel_fee-pull.py`](LNDg/channel_fee-pull.py) | `cronjob` | Pulls channel fee configurations from remote references and synchronizes into LNDg. |
| [`LNDg/disabled_fee-accelerator.py`](LNDg/disabled_fee-accelerator.py) | `cronjob` | Identifies disabled/inactive channels and escalates fees or triggers alerts. |
| [`LNDg/swap_out-candidates.py`](LNDg/swap_out-candidates.py) | `CLI tool` | Identifies channels with heavy local balances as candidates for off-loading liquidity via swap-out. |
| [`LNDg/offline_summary.py`](LNDg/offline_summary.py) | `cronjob` / `alert` | Sends Telegram reports summarizing offline channel downtime and liquidity impacts. |

---

### 3. PeerSwap Automation
Automate Layer-1 / Liquid swaps with connected peers.

| Script | Execution Mode | Purpose |
| :--- | :--- | :--- |
| [`Peerswap/peerswap-bot.py`](Peerswap/peerswap-bot.py) | `systemd service` | **PeerSwap Telegram Bot**: Interactive management bot to monitor peer swap eligibility, submit swaps, and view historical swap status. |
| [`Peerswap/peerswap-lndg_push.py`](Peerswap/peerswap-lndg_push.py) | `cronjob` | Ingests PeerSwap metrics and logs them directly into LNDg notes and database records. |
| [`Peerswap/ps_peers.py`](Peerswap/ps_peers.py) | `CLI tool` | Summarizes liquidity distribution and active status of all PeerSwap-compatible peers. |

---

### 4. Swaps & Capital Management
Manage on-chain UTXOs, submarine swaps, and dynamic channel fee pricing.

| Script | Execution Mode | Purpose |
| :--- | :--- | :--- |
| [`Other/fee_adjuster.py`](Other/fee_adjuster.py) | `cronjob` / `service` | **Dynamic Fee Optimizer**: Adjusts channel fees based on Amboss Space network fee trends, stuck channel detection, and liquidity percentages. Includes inbound discount protection and global rebalance guard auditing. |
| [`Other/rebalance_guard.py`](Other/rebalance_guard.py) | `cronjob` / `CLI tool` | **Standalone Rebalance Guard**: Audits all open LNDg channels across both native Auto-Fees and `fee_adjuster.py`. Protects channels with active inbound discounts by setting `ar_out_target = 100%`, and automatically restores baseline targets using **Dynamic Hysteresis** once liquidity recovers. |
| [`Other/boltz_swap-out.py`](Other/boltz_swap-out.py) | `CLI tool` | Automates submarine swap-outs through the Boltz exchange (Lightning to Liquid L-BTC). |
| [`Other/swap_wallet.py`](Other/swap_wallet.py) | `CLI tool` | Batches automated payouts or drain payments over Lightning to a designated Lightning Address. |
| [`Other/swap_out-loop.py`](Other/swap_out-loop.py) | `CLI tool` | Orchestrates continuous swap-outs for rebalancing large liquidity sinks. |
| [`Other/lnd_utxo_consolidator.py`](Other/lnd_utxo_consolidator.py) | `CLI tool` | Safely consolidates fragmented on-chain LND UTXOs during low-mempool fee environments. |

---

### 5. LNbits & Micro-services

| Script | Execution Mode | Purpose |
| :--- | :--- | :--- |
| [`LNBits/pocketmoney.py`](LNBits/pocketmoney.py) | `cronjob` | Automated pocket money allowances: tops up linked LNbits wallets on a regular schedule. |

---

## 🚀 Prerequisites & Installation

### System Requirements
- Python 3.10, 3.11, or 3.12
- Linux environment (Debian/Ubuntu/Raspberry Pi OS)
- Running **LND** node (`lncli` accessible)
- (Optional) Installed `bos` (Balance of Satoshis) and `peerswapd`

### 1. Clone & Set Up Virtual Environment
```bash
git clone https://github.com/TrezorHannes/Lightning-Python-Tools.git
cd Lightning-Python-Tools

# Create and activate Python virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Upgrade pip and install all required dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

---

## ⚙️ Configuration Guide

1. Create your local `config.ini`:
   ```bash
   cp config.ini.example config.ini
   ```
2. Populate the required sections:
   - `[credentials]`: Amboss API token (`amboss_authorization`), LNbits keys, LNDg credentials.
   - `[telegram]`: Telegram bot token and chat ID for notifications.
   - `[lndg]`: LNDg local API URL (e.g. `http://localhost:8889`).
   - `[paths]`: Absolute path to `lncli`, `bos`, and output rule directories.
   - `[magma]`: Min/max fee caps, invoice expiry, auto-approval thresholds.

---

## 🔄 Running as Background Services

### Systemd Daemon Example
Create `/etc/systemd/system/magma-sale.service`:
```ini
[Unit]
Description=Amboss Magma Channel Auto-Sale Service
After=network.target lnd.service

[Service]
Type=simple
User=admin
WorkingDirectory=/home/admin/Lightning-Python-Tools
ExecStart=/home/admin/Lightning-Python-Tools/.venv/bin/python3 /home/admin/Lightning-Python-Tools/Magma/magma_sale_process.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```
Enable and start the service:
```bash
sudo systemctl daemon-reload
sudo systemctl enable magma-sale.service
sudo systemctl start magma-sale.service
```

### Crontab Example
```cron
# Run fee adjuster every 30 minutes
*/30 * * * * /home/admin/Lightning-Python-Tools/.venv/bin/python3 /home/admin/Lightning-Python-Tools/Other/fee_adjuster.py >> /home/admin/cron_fee.log 2>&1

# Run standalone rebalance guard audit hourly
0 * * * * /home/admin/Lightning-Python-Tools/.venv/bin/python3 /home/admin/Lightning-Python-Tools/Other/rebalance_guard.py >> /home/admin/cron_guard.log 2>&1

# Sync Amboss Magma channel data to LNDg hourly
0 * * * * /home/admin/Lightning-Python-Tools/.venv/bin/python3 /home/admin/Lightning-Python-Tools/LNDg/amboss_pull.py >> /home/admin/cron_amboss.log 2>&1
```

---

## 🧪 Testing & Quality Assurance

The codebase includes full test coverage with unit tests and live GraphQL schema verification:

```bash
# Run the entire test suite
source .venv/bin/activate
pytest tests/ -v
```

---

## 🔒 Security Best Practices
- Never commit `config.ini`, `.env`, or credential files to Git.
- Restrict file permissions on `config.ini`: `chmod 600 config.ini`.
- Use read-only or restricted API keys whenever possible.
