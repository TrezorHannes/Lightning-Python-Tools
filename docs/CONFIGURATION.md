# Configuration Reference

All user configuration files containing credentials, pubkeys, or node rules are ignored by Git (see [`.gitignore`](../.gitignore)).

## Guided Setup & Hardening

```bash
# Copy missing templates, set chmod 600 on configs (chmod 700 on logs/ & data/), and validate
./install.sh config

# Limit to specific tools and open config.ini in $VISUAL / $EDITOR / nano
./install.sh config --tools fee-adjuster pocketmoney --edit config.ini
```

`./install.sh config` never overwrites existing config files and validates:
- Required `[section]` and key presence for the selected tools.
- Untouched template placeholders (`https://yourdomain.com`, `admin_key_1`, `WALLETID*`, `INVOICEID*`, `INVOICEKEY*`, `your_lnd_host:port`, `/path/to/your/lnd/tls.cert`).
- Valid JSON syntax in `feeConfig.json` and `wallets.json`, and valid INI syntax in `config.ini` and `Magma/magma_config.ini`.
- Valid `[Schedule]` (`daily`/`weekly`, `HH:MM`, weekday name) when `pocketmoney` is selected.

---

## Configuration Files

| File | Template | Used By |
| :--- | :--- | :--- |
| `config.ini` | [`config.ini.example`](../config.ini.example) | Core shared settings across all tools except `ps-peers` |
| `feeConfig.json` | [`feeConfig.json.example`](../feeConfig.json.example) | `Other/fee_adjuster.py`, `Other/rebalance_guard.py` |
| `Magma/magma_config.ini` | [`Magma/magma_config.ini.example`](../Magma/magma_config.ini.example) | `Magma/magma_market_fee.py` |
| `wallets.json` | [`wallets.json.example`](../wallets.json.example) | `LNBits/pocketmoney.py` |

### `config.ini` Sections

- **`[credentials]`**: `amboss_authorization` (Amboss API Bearer token), `lndg_username`, `lndg_password`.
- **`[lndg]`**: `lndg_api_url` (e.g. `http://localhost:8889`, or a reachable tunnel/private URL for remote VPS usage).
- **`[pubkey]` & `[no-swapout]`**: Comma-separated node pubkeys (`base_fee_ignore`, `banned_magma_pubkeys`, `swapout_blacklist`).
- **`[paths]` & `[system]`**: Binary paths (`lncli_path`, `pscli_path`, `boltzcli_path`, `full_path_bos`, `path_command`), `boltzd` TLS/macaroon paths, `charge_lnd_path` (defaults to `~/.chargelnd/.config`), and `log_level`.
- **`[telegram]`**: Bot tokens (`magma_bot_token`, `peerswap_bot_token`, `lnbits_bot_token`) and authorized `telegram_user_id` (required by Telegram bots and alerts).
- **`[info]`**: `node` (your local LND identity pubkey).
- **`[urls]` & `[parameters]`**: Explorer URLs (`mempool_tx`, `liquid_tx`, `mempool_fees_api`) and `disabled_fee-accelerator.py` parameters (`fee_updated_hours_ago`, `capped_ceiling`, `agg_liquidity_threshold`).
- **`[magma]`**: `magma_sale_process.py` order polling, invoice expiry, route hints, `max_fee_percentage_of_invoice`, default `channel_fee_rate_ppm`, and connect retries.
- **`[LNBits]` & `[Schedule]`**: LNbits server `base_url` (without `/api/v1`), parent `admin_key`, and allowance `recurrence` (`daily` / `weekly`), `time` (`HH:MM`), `day_of_week`.
- **`[lnd]` & `[loop]`**: LND gRPC settings and `swap_out-loop.py` / `magma_sale_process.py` Loop parameters (`loop_command`, `loop_db_path`, `rpcserver`, `tlscertpath`, fee caps, MPP headroom).
- **`[database]`**: Local PostgreSQL connection (`name`, `host`, `port`) for `LNDg/offline_summary.py`.

### Dry-Run & Verification Flags

Before scheduling **FUNDS** or **FEES/POLICY** scripts, preview behavior manually:
- `Other/fee_adjuster.py --debug` — prints full waterfall calculations without writing LNDg or `charge-lnd` files.
- `Other/rebalance_guard.py --dry-run` — previews target locks and restores without mutating LNDg.
- `Magma/magma_market_fee.py --dry-run` — simulates APR offer updates without calling Amboss mutations (`--force` is required for unattended live timers).
- `Other/swap_out-loop.py --dry-run` — scores and probes channels without executing Loop Out swaps.
- `Other/boltz_swap-out.py --debug` — runs Boltz reverse swap selection in dry-run mode.
- `Peerswap/peerswap-lndg_push.py -a --dry-run` — previews LNDg channel note appends (`-a` or `-o` is used in unattended timers).
