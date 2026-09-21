# Economically Optimized Lightning Loop Out Tool (`swap_out-loop.py`)

## 1. Overview

`Other/swap_out-loop.py` is an interactive CLI tool designed to help Lightning Network node operators rebalance channels with high local liquidity by executing the most economically advantageous **Loop Out** submarine swaps via Lightning Labs Loop (`litloop` / `loopd`).

Rather than simply finding channels with high local balances, `swap_out-loop.py` scores and ranks candidate channels based on their **Total Economic Cost per sat**, incorporating:
1. **Loop Server Service Fee**: The fee charged by Lightning Labs Loop for the swap.
2. **On-Chain Sweep Fee**: The miner fee required to sweep the on-chain HTLC into your LND wallet (or custom cold-storage address).
3. **Off-Chain LN Routing Fee**: The actual network fees incurred across intermediate hops to route payments to the Loop server node (`021c97a90a411ff2b10dc2a8e32de2f29d2fa49d41bfbb52bd416e460db0747d0d`).
4. **Opportunity Cost (Foregone Profit)**: Draining a channel with a high local fee rate (e.g. 500 ppm) forfeits future routing profits, whereas draining a low-fee channel (e.g. 5 ppm) preserves profit margins.

---

## 2. Key Features

- **Hybrid Sizing**:
  - Specify an explicit target amount via `--amt <sats>` (e.g., `--amt 2000000`).
  - Omit `--amt` to let the tool dynamically calculate the exact drain amount needed per channel to bring it down to a healthy equilibrium (default: 50% local ratio), respecting Loop's limits (250,000 to 240,000,000 sats).
- **Two-Stage Route Validation & Real Liquidity Probing**:
  - **Stage 1 (`lncli queryroutes`)**: Fast graph traversal to verify route existence and compute theoretical off-chain hop fees to the Loop server node.
  - **Stage 2 (Mandatory Live Prepay Probe)**: Probes the route with a random 32-byte fake hash via `lncli sendpayment`. Proves actual downstream liquidity without risking funds. Ghost liquidity paths from gossip graphs that fail live probing are automatically filtered out.
  - **Direct 2-Hop Route Fallback**: If multi-hop routes fail due to intermediate bottlenecks or dead gossip nodes, the tool automatically checks if the candidate peer maintains a direct channel to the Loop node (`021c97a9...`), constructing the route via `lncli buildroute` and probing it with `lncli sendtoroute` to accurately capture the exact verified route fee.
- **Concurrent Worker Probing (`--workers <N>`)**:
  - Probes candidates sequentially by default for zero downstream HTLC collision, or in parallel via `--workers 2` using `ThreadPoolExecutor` with thread-safe output formatting, cutting scanning time in half.
- **Interactive Terminal UI (Arrow-Key Navigation)**:
  - Native Python implementation (`termios` and `tty`) with zero Node/npm/npx dependencies.
  - Navigate candidates with `↑` / `↓` (or `k` / `j`) arrow keys, showing real-time highlighted selection and cost breakdown.
  - Press `[Enter]` to select and confirm, or `[q]` / `[Esc]` to abort.
  - Automatically falls back to a clean PrettyTable and numbered prompt in non-interactive/piped environments.
- **Economical Sweep Timing**:
  - Enforces a minimum confirmation target of 6 blocks (default: 9) to prevent overpaying for fast on-chain sweeps.
  - Omits `--fast` so Loop's swap server batches the on-chain HTLC publication, reducing chain fees.
- **Native Single Source of Truth (SOT) Accounting**:
  - Eliminates custom redundant SQLite databases by directly leveraging Loop daemon's native database (`~/.loop/mainnet/loop_sqlite.db`) as the authoritative Source of Truth.
  - Configurable via `loop_db_path` in `config.ini` (with automatic fallback to `litloop listswaps` RPC).
  - Swaps are automatically tagged on initiation with `--label "Loop-Out: <alias> (<chan_id>)"` to preserve peer and channel metadata directly in `loop.db`.
  - Displays historical swaps, settled costs, and realized PPM with `python Other/swap_out-loop.py --history`.
  - Supports spreadsheet bookkeeping via `--history --csv [optional_path]`.
- **Interactive Monitoring with Safe Detach**:
  - Launches `litloop monitor` directly in the foreground.
  - Node operators can press `[Ctrl+C]` at any time to detach without aborting the swap; background `loopd` continues processing automatically.

---

## 3. Prerequisites & Setup

### Debian NUC Configuration
The tool is designed to run directly on the node host (e.g., Debian NUC) where `lnd`, `litd` (or `loopd`), and `LNDg` are configured:
1. **LND**: Accessible via `lncli` or configured via `[lnd]` in `config.ini`.
2. **Loop Daemon**: Running via Lightning Terminal (`litd`) or standalone `loopd`.
   - On Debian NUC, the user alias is:
     ```bash
     alias litloop="loop --rpcserver=localhost:8443 --tlscertpath=~/.lit/tls.cert"
     ```
   - `swap_out-loop.py` automatically detects and uses `litloop` or falls back to `loop` with `~/.lit/tls.cert`.
3. **LNDg**: Channel balances and fee rates are queried via LNDg REST API.

### Configuration (`config.ini`)
Ensure the following sections are configured in `../config.ini`:
```ini
[credentials]
lndg_username = your_lndg_username
lndg_password = your_lndg_password

[lndg]
lndg_api_url = http://localhost:8889

[no-swapout]
# Add pubkeys you want to filter out for swapout outputs, comma-separated
swapout_blacklist = pubkey1,pubkey2

[loop]
# Path or alias to litloop/loop command. Defaults to litloop or loop
loop_command = litloop
# Path to loop's local SQLite database (Source of Truth for history and swap accounting)
loop_db_path = ~/.loop/mainnet/loop_sqlite.db
# Public key of the Lightning Labs Loop server node
loop_pubkey = 021c97a90a411ff2b10dc2a8e32de2f29d2fa49d41bfbb52bd416e460db0747d0d
# Default confirmation target for sweep transaction (minimum 6 for economical sweep)
conf_target = 9
# Default maximum local fee rate (ppm) to consider for looping out (opportunity cost cap)
max_local_fee_ppm = 100
# Minimum local balance ratio (percentage) on candidate channel to consider
min_local_balance_ratio = 60
# Minimum channel capacity (sats)
min_capacity = 3000000
# Target equilibrium local balance ratio (percentage) for dynamic sizing
target_local_ratio = 50
# Number of concurrent workers for route probing (default: 1, recommendation: 1-2)
workers = 1
# Timeout in seconds for individual route prepay probes (default: 15)
probe_timeout = 15
# Percentage leeway added on top of probed routing fee for max off-chain fee limit (default: 100 for +100% / 2x headroom)
fee_leeway_pct = 100
# Base satoshis buffer added to fee leeway to absorb base fees and small fluctuations (default: 500)
fee_leeway_base_sats = 500
```

---

## 4. Usage & Command-Line Flags

```bash
# Basic run with dynamic sizing and interactive arrow-key selection
python3 Other/swap_out-loop.py

# Specify an explicit swap amount (e.g., 5,000,000 sats)
python3 Other/swap_out-loop.py --amt 5000000

# Fast parallel route evaluation with 2 workers
python3 Other/swap_out-loop.py --amt 8000000 --workers 2

# Dry-run simulation (probes routes and fetches live quotes without spending funds)
python3 Other/swap_out-loop.py --dry-run

# Custom thresholds (minimum 5M capacity, max 50 ppm local fee, 70% min local ratio)
python3 Other/swap_out-loop.py --capacity 5000000 --fee-limit 50 --min-ratio 70

# Sweeping to an external cold-storage address with custom 12-block confirmation target
python3 Other/swap_out-loop.py --amt 3000000 --conf-target 12 --dest-addr bc1q...

# Non-interactive / headless automation (automatically selects top-ranked candidate)
python3 Other/swap_out-loop.py --amt 2000000 --auto-approve

# View historical loop-out operations from Loop's SOT database
python3 Other/swap_out-loop.py --history --limit 15

# Export historical swaps directly to CSV
python3 Other/swap_out-loop.py --history --csv
```

### CLI Flag Reference

| Flag | Type | Default | Description |
|---|---|---|---|
| `--amt` | `int` | `None` | Specific amount in sats to loop out (`250,000` to `240,000,000`). If omitted, dynamically calculated. |
| `-c`, `--capacity` | `int` | Config / `3M` | Minimum channel capacity in sats. |
| `-f`, `--fee-limit` | `int` | Config / `100` | Maximum local outbound fee rate in ppm. |
| `-r`, `--min-ratio` | `float` | Config / `60%` | Minimum local balance ratio percentage. |
| `--conf-target` | `int` | Config / `9` | Confirmation target for on-chain sweep (minimum `6`). |
| `-w`, `--workers` | `int` | Config / `1` | Number of concurrent worker threads for route probing (recommended: `1` or `2`). |
| `--probe-timeout` | `int` | Config / `15` | Timeout in seconds for individual route prepay probes. |
| `--skip-prepay-probe` | `flag` | `False` | Skip active prepay probing and rely on queryroutes theoretical fees (not recommended). |
| `--max-routing-fee`| `int` | Leeway buffer | Upper limit on off-chain routing fees in satoshis. Set to 0 to omit fee limit (uses Loop daemon default). |
| `--fee-leeway-pct` | `float`| Config / `100%` | Percentage leeway added on top of probed routing fee for max off-chain fee budget (e.g. 100 = 2x headroom). |
| `--dest-addr` | `str` | LND wallet | Custom destination address for swept on-chain funds. |
| `--dry-run` | `flag` | `False` | Simulates candidate selection, live quotes, and route probes without executing. |
| `--auto-approve` | `flag` | `False` | Automatically executes the top-ranked candidate without interactive prompt. |
| `--history` | `flag` | `False` | Prints table of historical swaps from Loop's Source of Truth database. |
| `--csv` | `str` | `None` | Export history to CSV file (defaults to `data/loop_out_history.csv` if no path provided). |
| `--limit` | `int` | `30` | Maximum number of historical swaps to display with `--history`. |
| `-p`, `--pubkey` | `flag` | `False` | Shows remote node pubkeys in candidate table. |

---

## 5. Economic Scoring Formula

For each candidate channel routing amount $A$:
$$\text{Loop Fee} = \text{Server Fee} + \text{On-Chain Sweep Fee}$$
$$\text{Routing Fee} = \text{LN Hop Fees to Loop Node}$$
$$\text{Opportunity Cost} = \text{round}\left(A \times \frac{\text{Local Fee Rate (ppm)}}{1,000,000}\right)$$
$$\text{Total Cost} = \text{Loop Fee} + \text{Routing Fee} + \text{Opportunity Cost}$$
$$\text{Net Effective PPM} = \text{round}\left(\frac{\text{Total Cost}}{A} \times 1,000,000\right)$$

Candidates are sorted ascending by **Net Effective PPM**.

---

## 6. Accounting & Data Persistence (Source of Truth)

Rather than keeping a separate desynchronized database, swap records are read directly from Loop daemon's native SQLite database:
- **Location**: `~/.loop/mainnet/loop_sqlite.db` (configurable via `loop_db_path` in `config.ini`).
- **RPC Fallback**: If the database file is not directly mounted or accessible, queries automatically fall back to `litloop listswaps`.

Each swap initiated by `swap_out-loop.py` is tagged with `--label "Loop-Out: <alias> (<channel_id>)"` to preserve full channel and peer metadata in Loop's own database.

To view past operations:
```bash
# Formatted table view
python3 Other/swap_out-loop.py --history

# Export to CSV spreadsheet
python3 Other/swap_out-loop.py --history --csv
```
