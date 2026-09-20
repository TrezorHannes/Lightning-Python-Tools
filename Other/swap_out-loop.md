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
- **Two-Stage Route Validation**:
  - **Stage 1 (`lncli queryroutes`)**: Fast graph traversal to verify route existence and compute exact off-chain hop fees to the Loop server node.
  - **Stage 2 (`lncli sendpayment` Prepay Probing)**: Probes the route with a random 32-byte fake hash. Only routes that reach the Loop server and return `INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS` pass verification. No funds are locked or spent during probing.
- **Interactive Terminal UI (Arrow-Key Navigation)**:
  - Native Python implementation (`termios` and `tty`) with zero Node/npm/npx dependencies.
  - Navigate candidates with `↑` / `↓` (or `k` / `j`) arrow keys, showing real-time highlighted selection and cost breakdown.
  - Press `[Enter]` to select and confirm, or `[q]` / `[Esc]` to abort.
  - Automatically falls back to numbered text prompt in non-interactive/piped environments.
- **Economical Sweep Timing**:
  - Enforces a minimum confirmation target of 6 blocks (default: 9) to prevent overpaying for fast on-chain sweeps.
  - Omits `--fast` so Loop's swap server batches the on-chain HTLC publication, reducing chain fees.
- **Native Single Source of Truth (SOT) Accounting**:
  - Leverages Loop's own database (`~/.loop/mainnet/loop_sqlite.db`) as the authoritative Source of Truth (SOT), avoiding redundant databases and state divergence.
  - Can be configured via `loop_db_path` in `config.ini` or queried dynamically over RPC via `litloop listswaps`.
  - Swaps are automatically tagged on initiation with `--label "Loop-Out: <alias> (<chan_id>)"` to preserve peer and channel metadata directly in `loop.db`.
  - Displays historical swaps and final settled costs with `python Other/swap_out-loop.py --history`.
  - Supports CSV exports via `--history --csv [optional_path]`.
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
swapout_blacklist = pubkey1,pubkey2

[loop]
# Path or alias to litloop/loop command
loop_command = litloop
# Path to loop's local SQLite database (Source of Truth for history and swap accounting)
loop_db_path = ~/.loop/mainnet/loop_sqlite.db
# Public key of the Lightning Labs Loop server node
loop_pubkey = 021c97a90a411ff2b10dc2a8e32de2f29d2fa49d41bfbb52bd416e460db0747d0d
# Default confirmation target for sweep transaction (minimum 6)
conf_target = 9
# Maximum local fee rate (ppm) to consider for looping out
max_local_fee_ppm = 100
# Minimum local balance ratio (%) on candidate channel
min_local_balance_ratio = 60
# Minimum channel capacity (sats)
min_capacity = 3000000
# Target equilibrium local balance ratio (%) for dynamic sizing
target_local_ratio = 50
```

---

## 4. Usage & Command-Line Flags

```bash
# Basic run with interactive arrow-key selection
python3 Other/swap_out-loop.py

# Specify an explicit swap amount (e.g., 2,500,000 sats)
python3 Other/swap_out-loop.py --amt 2500000

# Dry-run simulation (probes routes and fetches live quotes without spending funds)
python3 Other/swap_out-loop.py --dry-run

# Custom thresholds (minimum 5M capacity, max 50 ppm local fee, 70% min local ratio)
python3 Other/swap_out-loop.py --capacity 5000000 --fee-limit 50 --min-ratio 70

# Sweeping to an external cold-storage address with custom 12-block confirmation target
python3 Other/swap_out-loop.py --amt 3000000 --conf-target 12 --dest-addr bc1q...

# Non-interactive / headless automation (automatically selects top-ranked candidate)
python3 Other/swap_out-loop.py --amt 2000000 --auto-approve

# View historical loop-out operations from SQLite database
python3 Other/swap_out-loop.py --history
```

### CLI Flag Reference

| Flag | Type | Description |
|---|---|---|
| `--amt` | `int` | Specific amount in sats to loop out (`250,000` to `240,000,000`). If omitted, dynamically calculated. |
| `-c`, `--capacity` | `int` | Minimum channel capacity in sats (default: `3,000,000`). |
| `-f`, `--fee-limit` | `int` | Maximum local outbound fee rate in ppm (default: `100`). |
| `-r`, `--min-ratio` | `float` | Minimum local balance ratio % (default: `60.0%`). |
| `--conf-target` | `int` | Confirmation target for on-chain sweep (default: `9`, minimum `6`). |
| `--max-routing-fee`| `int` | Upper limit on off-chain routing fees in sats (default: auto + buffer). |
| `--dest-addr` | `str` | Destination address for swept on-chain funds (default: LND wallet). |
| `--dry-run` | `flag` | Simulates candidate selection, live quotes, and route probes without executing. |
| `--auto-approve` | `flag` | Automatically executes the top-ranked candidate without interactive prompt. |
| `--history` | `flag` | Prints table of historical swaps from SQLite database. |
| `-p`, `--pubkey` | `flag` | Shows remote node pubkeys in candidate table. |

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

## 6. Accounting & Data Persistence

Records of initiated and completed swaps are stored locally in:
- SQLite Database: `data/loop_out_history.db`
- CSV Export: `data/loop_out_history.csv`

To inspect the SQLite database directly:
```bash
sqlite3 data/loop_out_history.db "SELECT initiation_time, swap_id, peer_alias, amount, total_cost, effective_ppm, status FROM loop_outs;"
```
