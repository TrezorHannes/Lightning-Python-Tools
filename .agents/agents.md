# Agent Documentation: Lightning-Python-Tools

This document serves as an architectural, operational, and development guide for AI agents, developers, and node operators working with the **Lightning-Python-Tools** repository.

---

## 1. Architecture & Ecosystem Overview

`Lightning-Python-Tools` is a modular collection of automation daemons, background services, CLI utilities, and cron jobs designed for Lightning Network (LN) node operators running **LND**. It integrates seamlessly with key Lightning infrastructure components:

- **LND (`lncli`) / Balance of Satoshi (`bos`)**: Channel operations, UTXO management, invoice generation, and on-chain fee estimation.
- **Amboss Space & Magma Market**: Liquidity marketplace integration using the modern Amboss Magma GraphQL API (`https://magma.amboss.tech/graphql`) and Amboss Space metadata API (`https://api.amboss.space/graphql`).
- **LNDg**: Lightning Network node management dashboard, database sync, fee policies, and rebalancer coordination.
- **PeerSwap**: Trustless off-chain to on-chain (L-BTC / BTC) peer rebalancing monitoring and dashboard integration.
- **Boltz & Lightning Loop**: Automated submarine swap-out operations for off-chain to on-chain liquidity rebalancing.
- **LNbits**: Sub-wallet allowance management and automated budget allocations.
- **Telegram Bot API**: Real-time alerts, interactive manual approval callbacks, and remote command execution.

---

## 2. Directory & Module Breakdown

### 2.1 `Magma/` — Amboss Magma Liquidity Market Automation

Amboss Magma is a decentralized Lightning liquidity marketplace where node operators buy and sell inbound channels.

| File | Type | Description |
| :--- | :--- | :--- |
| `magma_sale_process.py` | Systemd Daemon / CLI | **Full-lifecycle seller automation**: Continuously monitors sales orders with `WAITING_FOR_SELLER_APPROVAL` and `WAITING_FOR_CHANNEL_OPEN`. Computes on-chain fee requirements via mempool API, verifies available UTXOs (filtering Loop locks), creates pre-image HODL invoices, opens channels via `lncli` or `bos`, auto-rejects banned peer pubkeys, confirms funding transaction outpoints to Amboss Magma via `market.order.seller.add_transaction`, and handles interactive Telegram approval callbacks. |
| `magma_market_fee.py` | Cronjob / CLI / Systemd | **Dynamic pricing & offer optimizer**: Gathers public Magma sell offers via `market.offer.offers`, filters out low seller scores (< threshold), analyzes percentile fee distributions (base fee & PPM), calculates annualized yield (APR), reserves required on-chain capital, and dynamically creates (`market.offer.create`), updates (`market.offer.update`), or toggles (`market.offer.toggle`) Magma sell offers. |
| `magma_config.ini` | Configuration | Defines pricing percentiles, minimum seller score filters, duration templates, and on-chain reserve thresholds for Magma. |
| `magma_sale.MD` | Documentation | Step-by-step setup guide for running `magma_sale_process.py` as a persistent `systemd` service. |

#### Amboss Magma GraphQL Endpoints
- **Magma Liquidity Market API**: `https://magma.amboss.tech/graphql` (Sales, Orders, Offers, Mutations).
- **Amboss Space API**: `https://api.amboss.space/graphql` (Node lookups, aliases, extended info).

---

### 2.2 `LNDg/` — LNDg Dashboard & Node Coordination

Tools designed to augment and automate the [LNDg](https://github.com/cryptoshred/lndg) management interface.

| File | Type | Description |
| :--- | :--- | :--- |
| `amboss_pull.py` | Cronjob / CLI | Pulls Magma sell orders from Amboss and synchronizes channel details, maturity dates, and auto-fee triggers into the local LNDg database and notes. |
| `channel_base-fee.py` | Cronjob / CLI | Evaluates channel conditions (such as fee thresholds and flow metrics) and adjusts base fees dynamically in LNDg. |
| `channel_fee-pull.py` | Cronjob / CLI | Exports channel fee policies and base fees from LNDg into standardized text/JSON formats for external automation tools. |
| `swap_out-candidates.py` | CLI / Tool | Evaluates channel capacity, local balances, and fee rates to identify prime candidates for submarine swap-outs; supports exporting to `.bos` tags format. |
| `mempool_rebalancer_trigger.py`| Systemd / Service | Tracks half-hour mempool fee rates (sat/vB). Automatically disables LNDg Auto-Rebalancer during high-fee congestion and re-enables it when fees normalize. |
| `disabled_fee-accelerator.py` | Cronjob / CLI | Adjusts fees on channels flagged as disabled or low-outbound by LNDg to incentivize liquidity rebalancing and prevent stale channels. |
| `offline_summary.py` | Cronjob / Alert | Scans peer states and compiles summaries of offline channels and inactive peers for Telegram / log reporting. |

---

### 2.3 `Peerswap/` — PeerSwap Liquidity & Monitoring

Integration scripts for [PeerSwap](https://github.com/ElementsProject/peerswap) (L-BTC / BTC on-chain swaps).

| File | Type | Description |
| :--- | :--- | :--- |
| `peerswap-lndg_push.py` | Cronjob / CLI | Queries `pscli listpeers` and `pscli listswaps`, aggregates swap volumes and counts per peer, and injects the telemetry directly into LNDg channel cards and dashboard notes. |
| `ps_peers.py` | CLI Tool | Formats and displays a clean tabular overview of L-BTC on-chain balances, active PeerSwap peers, channel capacities, and available swap liquidity. |
| `peerswap-bot.py` | Telegram Bot | Interactive bot for monitoring PeerSwap daemon health, initiating swaps, and sending alerts upon swap completion or failure. |

---

### 2.4 `Other/` — Advanced Routing, Consolidations & Swap Automation

| File | Type | Description |
| :--- | :--- | :--- |
| `fee_adjuster.py` | Systemd / Cronjob | **Advanced Liquidity Fee Controller**: Dynamically adjusts channel routing fees based on liquidity balance curves, applies progressive discounts for stuck outbound capacity, adds demand premiums, and protects newly opened channels with cooldown guards. |
| `lnd_utxo_consolidator.py` | CLI / Daemon | Monitors the mempool for low-fee windows (e.g. < 10 sat/vB) and consolidates small, fragmented LND wallet UTXOs into single high-value outputs to save future fees. |
| `boltz_swap-out.py` | CLI / Daemon | Performs trustless submarine swap-outs via the Boltz Exchange API, converting excess Lightning outbound liquidity into Liquid BTC (L-BTC). |
| `swap_out-loop.py` | CLI / Daemon | Submarine swap-out automation leveraging Lightning Labs Loop daemon (`loopd` / `loop out`). |
| `swap_wallet.py` | CLI Tool | Automated Lightning payment scheduler and non-custodial batch payout tool for wallet-to-wallet transfers with rate limits and fee caps. |

---

### 2.5 `LNBits/` — Micro-Wallet & Allowance Management

| File | Type | Description |
| :--- | :--- | :--- |
| `pocketmoney.py` | Cronjob / Systemd | Automated allowance daemon that distributes scheduled satoshi payments from a master LNbits wallet to child sub-wallets based on configurable fiat/crypto budgets. |

---

## 3. Configuration Conventions

Configuration files follow standard INI (`configparser`) and JSON formats:

- `config.ini` / `config.ini.example`: Core repository configuration including:
  - `[credentials]`: Amboss tokens, LNbits API keys.
  - `[telegram]`: Telegram bot token and user/group chat IDs.
  - `[paths]`: Path to `lncli`, `bos`, and LNDg database.
  - `[magma]`: Invoice expiry, fee limits, and auto-approval settings.
- `Magma/magma_config.ini`: Granular offer templates, duration brackets, and capital reserve limits.
- `Other/feeConfig.json`: Liquidity curve points, discount steps, and channel tags for `fee_adjuster.py`.

---

## 4. Testing & Quality Standards

- **Unit Testing Framework**: `pytest` with `pytest-mock` and `requests-mock`.
- **Test Locations**: `tests/` directory:
  - `tests/Magma/test_magma_sale_process.py`: Sales order lifecycle, GraphQL queries/mutations, invoice generation, UTXO vbyte calculations, and Telegram alerts.
  - `tests/Magma/test_magma_market_fee.py`: Public offers analysis, APR calculations, offer creation/updates/toggles, and dry-run simulations.
  - `tests/test_fee_adjuster.py`: Liquidity curve calculations, stuck-channel discounts, and fee protections.
- **CI/CD Matrix**: `.github/workflows/tests.yml` executes on every push and pull request across Python `3.10`, `3.11`, and `3.12`.
- **Development Policy**: All new features and bugfixes must adhere to strict Test-Driven Development (TDD) and ensure 100% passing tests before merging.
