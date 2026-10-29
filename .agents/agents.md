# Agent Documentation: Lightning-Python-Tools

This document serves as an architectural, operational, and development guide for AI agents, developers, and node operators working with the **Lightning-Python-Tools** repository.

---

## 1. Architecture & Ecosystem Overview

`Lightning-Python-Tools` is a modular collection of automation daemons, systemd timer units, CLI utilities, and cron jobs for Lightning Network node operators running **LND**. It integrates with the following infrastructure components:

- **LND (`lncli`) / Balance of Satoshis (`bos`)**: Channel operations, UTXO inspection/consolidation, invoice generation, route probing, and fee estimation.
- **Amboss Space & Magma Market**: Liquidity marketplace integration using the Amboss Magma GraphQL API (`https://magma.amboss.tech/graphql`) and Amboss Space GraphQL API (`https://api.amboss.space/graphql`).
- **LNDg**: Node management dashboard REST API (and optional PostgreSQL database) for channel balances, forward timestamps, fee policies, notes, and auto-rebalancer coordination.
- **charge-lnd**: Rule file generation (`fee_adjuster.txt`, `magma-channels_*.txt`, `magma-finished.txt`, `0_fee.txt`) for external policy enforcement.
- **PeerSwap (`peerswapd` / `pscli`)**: Layer-1 BTC and Liquid (L-BTC) peer swap monitoring, Telegram bot control, and LNDg note synchronization.
- **Lightning Loop (`litloop` / `loopd`) & Boltz (`boltzd` / `boltzcli`)**: Economically scored Loop Out swaps with live prepay route probes and Boltz reverse submarine swaps to Liquid L-BTC.
- **LNbits**: Scheduled fiat-denominated satoshi allowances across child sub-wallets.
- **Telegram Bot API (`pyTelegramBotAPI`)**: Real-time alerts, interactive order approval callbacks, error log streaming, and bot commands.

---

## 2. Directory & Module Breakdown

### 2.1 `Magma/` - Amboss Magma Liquidity Market Automation

| File | Type | Description |
| :--- | :--- | :--- |
| `magma_sale_process.py` | Systemd Daemon / CLI | **Full-lifecycle seller automation**: Monitors sales orders in `WAITING_FOR_SELLER_APPROVAL` and `WAITING_FOR_CHANNEL_OPEN`. Checks mempool fees and unlocked UTXOs, generates Lightning invoices via `lncli addinvoice`, handles interactive Telegram approval callbacks (`/processmagmaorders`) or timeout auto-approval, opens channels via `lncli`, auto-rejects banned pubkeys, and confirms funding outpoints via `market.order.seller.add_transaction`. |
| `magma_market_fee.py` | Cronjob / CLI | **Dynamic pricing & offer optimizer**: Queries public Magma sell offers via `market.offer.offers`, filters by minimum seller score, analyzes percentile fee distributions, computes annualized yield (APR), allocates on-chain capital, and creates (`market.offer.create`), updates (`market.offer.update`), or toggles (`market.offer.toggle`) Magma offers. Supports `--dry-run` and `--force`. |
| `magma_config.ini.example` | Config Template | Copy to `Magma/magma_config.ini` (gitignored). Defines pricing percentiles, seller score filters, capital allocation shares, and `[magma_offer_*]` templates. |
| `magma_sale.md` | Documentation | Setup, systemd unit configuration, troubleshooting, and simulation guide for `magma_sale_process.py`. |

#### Amboss GraphQL Endpoints
- **Magma Liquidity Market API**: `https://magma.amboss.tech/graphql` (Sales, Orders, Offers, Mutations).
- **Amboss Space API**: `https://api.amboss.space/graphql` (Node lookups, aliases, peer fee metrics).

---

### 2.2 `LNDg/` - LNDg Dashboard & Node Coordination

| File | Type | Description |
| :--- | :--- | :--- |
| `amboss_pull.py` | Cronjob / CLI | Pulls Magma sell orders from Amboss, resolves channel SCIDs (via Amboss Space batch lookup with mathematical SCID fallback), writes lease maturity notes and auto-fee/rebalance flags to LNDg, and exports `charge-lnd` fee-cap rule files. |
| `mempool_rebalancer_trigger.py` | Systemd Daemon | Polls half-hour mempool fee rates (`sat/vB`) and updates LNDg `AR-Enabled` (`0` during high-fee spikes, `1` when fees normalize). |
| `channel_base-fee.py` | Cronjob / CLI | Resets `base_fee` to `0` across open, active channels in LNDg (excluding pubkeys in `base_fee_ignore`). |
| `channel_fee-pull.py` | Cronjob / CLI | Exports open channel IDs with `local_fee_rate == 0` from LNDg to `~/.chargelnd/.config/0_fee.txt` for `charge-lnd`. |
| `disabled_fee-accelerator.py` | Cronjob / CLI | Incrementally bumps `local_fee_rate` in LNDg for peer groups with low aggregate local liquidity so channels disabled by `charge-lnd` can attract rebalance inflows. |
| `swap_out-candidates.py` | CLI Tool | Filters high-local-balance, low-PPM channels from LNDg as swap-out candidates and optionally exports to `data/low-fee-high-local.log` (`-e`) or `bos/tags.json` (`-b`). |
| `offline_summary.py` | CLI Tool | Queries LNDg's PostgreSQL `gui_peerevents` table (`--month YYYY-MM`) and prints average and total peer offline hours (requires optional `psycopg2` and `pandas`). |

---

### 2.3 `Peerswap/` - PeerSwap Liquidity & Monitoring

| File | Type | Description |
| :--- | :--- | :--- |
| `peerswap-bot.py` | Systemd Daemon | Interactive Telegram bot for checking L-BTC balances/addresses, listing peers and swap requests, and executing `swapin` / `swapout` / `lbtcsend` commands. |
| `peerswap-lndg_push.py` | Cronjob / CLI | Queries `pscli listpeers` and `lncli listchannels`, aggregates swap counts, volumes, and paid fees per peer, and writes them into LNDg channel notes. |
| `ps_peers.py` | CLI Tool | Displays a terminal summary table of L-BTC balance, trusted PeerSwap peers, supported assets, channel balances, and fee rates. |
| `README.md` | Documentation | Bare-metal, RaspiBlitz, and MiniBolt setup guide for `elementsd` and `peerswapd`. |

---

### 2.4 `Other/` - Fee Curves, Rebalance Guard, Swaps & UTXO Consolidation

| File | Type | Description |
| :--- | :--- | :--- |
| `fee_adjuster.py` | Systemd Timer / Cronjob | **Dynamic Liquidity Fee Controller**: Adjusts outbound and inbound channel fees using Amboss Space statistics, 5-band liquidity curves, and stuck-channel decay, with `charge-lnd` export and inbound discount protection. Documented in `Other/fee_adjuster.md`; systemd installer in `Other/install_fee_adjuster_service.sh`. |
| `rebalance_guard.py` | Cronjob / CLI | **Standalone Rebalance Guard**: Audits all open LNDg channels across native Auto-Fees and `fee_adjuster.py`. Locks `ar_out_target = 100%` when an inbound discount is active and restores baseline targets (`data/rebalance_targets_baseline.json`) via dynamic hysteresis once liquidity recovers. |
| `swap_out-loop.py` | CLI Tool | **Economically Optimized Loop Out**: Ranks channels by net effective PPM, runs live fake-hash prepay route probes (with 2-hop direct Loop fallback), supports multi-channel MPP selection (`litloop` / `loopd`), interactive TTY multi-select, and native `loop_sqlite.db` history/CSV accounting. Documented in `Other/swap_out-loop.md`. |
| `boltz_swap-out.py` | CLI Tool | Executes reverse submarine swaps (Lightning to Liquid L-BTC) via `boltzd` / `boltzcli` using LNDg candidate channels, multi-batch payment retries, and `--debug` dry-run mode. |
| `swap_wallet.py` | CLI Tool | Batches Lightning payouts to a Lightning Address via `bos send` through high-local-balance peers under configurable fee caps. |
| `lnd_utxo_consolidator.py` | CLI Tool | Interactive helper that inspects `lncli listunspent` and builds a verified `lncli sendcoins` UTXO consolidation command for manual review (never broadcasts directly). |

---

### 2.5 `LNBits/` - Allowance Management

| File | Type | Description |
| :--- | :--- | :--- |
| `pocketmoney.py` | Systemd Timer / Daemon | Distributes scheduled fiat-denominated satoshi allowances from a parent LNbits wallet to child wallets defined in `wallets.json` (`--run-once` supported). Systemd timer installer in `LNBits/install_pocketmoney_service.sh`. |

---

## 3. Configuration Conventions

All user-specific configuration files are ignored by `.gitignore` and initialized from `.example` templates:

- `config.ini` (from `config.ini.example`): Shared configuration across scripts (`[credentials]`, `[lndg]`, `[pubkey]`, `[no-swapout]`, `[paths]`, `[telegram]`, `[info]`, `[system]`, `[urls]`, `[parameters]`, `[magma]`, `[LNBits]`, `[Schedule]`, `[lnd]`, `[loop]`, `[database]`).
- `Magma/magma_config.ini` (from `Magma/magma_config.ini.example`): Offer templates, duration brackets, seller score filter, and capital allocation shares for `Magma/magma_market_fee.py`.
- `feeConfig.json` (from `feeConfig.json.example`): Peer groups, liquidity bands, stuck-channel settings, and `inbound_protection` thresholds for `Other/fee_adjuster.py` and `Other/rebalance_guard.py`.
- `wallets.json` (from `wallets.json.example`): Child wallet IDs, invoice keys, and fiat allowance amounts for `LNBits/pocketmoney.py`.

---

## 4. Testing & Quality Standards

- **Testing Stack**: `pytest`, `pytest-cov`, `pytest-mock`, and `requests-mock` (installed via `pip install -r requirements-dev.txt`, configured in `pyproject.toml`).
- **Test Suite (`tests/`)**:
  - `tests/LNDg/test_amboss_pull.py`: Magma order sync to LNDg, SCID conversion fallback, Amboss Space batch lookup, fee-cap clustering, and REST updates.
  - `tests/Magma/test_magma_sale_process.py`: Order lifecycle, GraphQL queries/mutations, `lncli addinvoice`, UTXO vbyte fee validation, and Telegram callbacks.
  - `tests/Magma/test_magma_market_fee.py`: Public offer analysis, APR math, offer create/update/toggle logic, and `--dry-run` mode.
  - `tests/Magma/test_magma_simulation.py` & `tests/simulate_magma_sale.py`: End-to-end offline and live-Telegram Magma order simulation harness.
  - `tests/Other/test_swap_out_loop.py`: Economic PPM scoring, route prepay probing, 2-hop fallback, multi-channel batching, TTY UI, and `loop_sqlite.db` history/CSV export.
  - `tests/test_amboss_graphql_contracts.py`: AST-level Amboss Magma and Space GraphQL field/input schema contract checks.
  - `tests/test_ca_trust_hardening.py`: Enforces `certifi>=2026.10.19`, `requests>=2.34.2`, `urllib3>=2.8.0`, and OS CA trust bundle bindings in systemd installers.
  - `tests/test_fee_adjuster.py`: Liquidity band adjustments, stuck-channel step-downs, inbound fee discount caps, `ar_out_target` locking, and Amboss error handling.
  - `tests/test_rebalance_guard.py`: Cross-channel rebalance guard auditing and dynamic hysteresis baseline restoration.
  - `tests/test_repo_hygiene.py`: LNbits URL normalization/fail-closed config checks, import-time side-effect guards, dependency hygiene, and `README.md` link integrity.
- **CI/CD Matrix**: `.github/workflows/tests.yml` runs on every push and pull request across Python `3.11`, `3.12`, and `3.13` with pip caching and `pytest-cov` reporting.
- **Development Policy**: Follow Test-Driven Development (TDD), maintain zero broken CI checks, and ensure all tests pass before merging.
