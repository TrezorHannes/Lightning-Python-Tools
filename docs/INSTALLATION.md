# Installation & Scheduling Guide

This guide covers setting up **Lightning-Python-Tools** with the unified `./install.sh` manager (`lpt_installer/`).

## Prerequisites

- **Operating System:** Linux (Debian, Ubuntu, RaspiBlitz / Raspberry Pi OS) for `systemd` and `cron` scheduling; macOS/Linux for CLI development and tests.
- **Python:** `3.11`, `3.12`, or `3.13` (with `python3-venv` on Debian/Ubuntu if `uv` is not installed).
- **Non-root operator user:** Run `./install.sh` as your regular operator account (for example `admin`), **never** as `root` or under `sudo ./install.sh`. The installer invokes `sudo` only when writing/reloading `/etc/systemd/system/lpt-*` units.
- **Optional binaries** (depending on selected scripts): `lncli` (LND), `pscli` (PeerSwap), `litloop` / `loop` (Lightning Loop), `boltzcli` (Boltz), `bos` (Balance of Satoshis), `charge-lnd`.

## Interactive Setup

```bash
chmod +x install.sh
./install.sh
```

On first run, the installer displays the **Operator Disclaimer** (self-verification, MIT no-liability terms, and `SECURITY.md` update guidance) and persists acknowledgment in gitignored `.lpt-state.json`. From the interactive numbered menu you can:

1. Create or update the unified `.venv` (`uv` fast-path if installed, otherwise `python3 -m venv .venv` + `pip`).
2. Copy missing config templates (`*.example`), enforce `chmod 600` on configs and `chmod 700` on `logs/` and `data/`, edit files in `$VISUAL`/`$EDITOR`/`nano`, and validate placeholders.
3. Install and enable selected tools via `systemd` or managed `cron`.
4. Uninstall managed tools cleanly while keeping user configs, logs, and data intact.
5. Run `./install.sh doctor` diagnostics.
6. Switch between host profiles (`all`, `node`, `vps`).

## CLI Subcommands

Every action is scriptable for SSH automation and remote VPS management:

| Command | Purpose |
| :--- | :--- |
| `./install.sh list` | Print the 19-script overview table with risk tier, default mode, host profiles, and runtime status. |
| `./install.sh status` | Same as `list`; supports `--profile {all,node,vps}` and `--json`. |
| `./install.sh env [--db] [--recreate] [--dry-run]` | Create or refresh `.venv` (`--db` adds `psycopg2-binary` and `pandas` for `LNDg/offline_summary.py`). |
| `./install.sh config [--tools ID ...] [--edit FILE] [--dry-run]` | Copy missing templates, enforce `chmod 600` / `chmod 700`, optionally edit a file, and run placeholder/schema checks. |
| `./install.sh install ID ... [--scheduler auto\|systemd\|cron\|manual] [--accept-disclaimer] [--dry-run]` | Validate configs, ensure `.venv`, migrate legacy units, and install/start `lpt-*` units or cron entries. |
| `./install.sh uninstall [ID ... \| --all] [--remove-venv] [--purge-configs --yes] [--dry-run]` | Stop/disable `lpt-*` units and remove managed cron entries (preserves configs/logs/data unless `--purge-configs --yes` is passed). |
| `./install.sh doctor [--json]` | Run read-only checks for Python, `.venv` `certifi`, OS CA bundle, `0600` permissions, root-owned runtime files, and legacy units. |

## Host Profiles (`node` vs `vps`)

- **`--profile node` (e.g. a full LND/LNDg node):** Shows all 19 scripts, including tools needing local `lncli`, `pscli`, `litloop`, `boltzcli`, `bos`, or PostgreSQL.
- **`--profile vps` (e.g. a companion VPS):** Filters to HTTP-only tools and remote-LNDg-capable scripts (`pocketmoney`, `fee-adjuster`, `rebalance-guard`, `amboss-pull`, `channel-base-fee`, `disabled-fee-accelerator`, `mempool-rebalancer-trigger`).

## Scheduling Backends: `systemd` vs `cron`

| Feature | `systemd` Service / Timer (Recommended) | Managed `cron` Block |
| :--- | :--- | :--- |
| **Supported tools** | Daemons (`Type=simple`) & periodic tasks (`Type=oneshot` + `.timer`) | Periodic tasks only (not long-running daemons) |
| **Logging** | `journalctl -u lpt-<id> -n 50 --no-pager` | `logs/cron_<id>.log` |
| **Overlap protection** | Native systemd single-instance serialization | `flock -n logs/.lpt-<id>.lock` |
| **Hang protection** | `TimeoutStartSec=300` on every `Type=oneshot` unit | `flock -n` skips overlapping runs |
| **Catch-up after downtime** | `Persistent=true` on `pocketmoney`, `fee-adjuster`, `amboss-pull` | Runs at next scheduled wall-clock slot |
| **OS CA trust** | `SSL_CERT_FILE` & `REQUESTS_CA_BUNDLE` injected automatically | `SSL_CERT_FILE` & `REQUESTS_CA_BUNDLE` set in managed block header |
| **Sandboxing** | `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectKernel*`, `ProtectSystem=full` (`strict` for HTTP-only `pocketmoney`) | Runs as current user |

### `pocketmoney` Schedule Behavior

`LNBits/pocketmoney.py` reads `[Schedule]` from `config.ini` (`recurrence = daily|weekly`, `time = HH:MM`, `day_of_week = friday`). When installed as `systemd-timer` or `cron`, the installer translates `[Schedule]` into `OnCalendar=` / cron syntax and passes `--run-once`. If installed as `systemd-service` (`Type=simple`), `--run-once` is omitted so `pocketmoney.py` runs its internal `schedule` daemon loop.

## Migrating Legacy Units & Duplicate-Execution Guard

Before installing any tool, `lpt_installer` scans `/etc/systemd/system/*.service`, `~/.config/systemd/user/*.service`, `crontab -l` (outside the managed block), and running processes for the target script filename:

- **Known legacy systemd units** (`pocketmoney.{service,timer}`, `fee_adjuster.{service,timer}`, `magma-sale.service`, `magma-autosale-bot.service`, `peerswap-bot.service`, `mempool_rebalancer_trigger.service`) are backed up to `.lpt-backups/<timestamp>/` (`0700`), stopped, disabled, removed, and replaced by `lpt-<id>.*`.
- **Unmanaged crontabs, custom units, or stray processes** cause the installer to **fail closed** so funds-moving or fee-modifying scripts never run twice simultaneously. Remove or stop the conflicting entry manually first, then re-run `./install.sh install ...`.
- **Migrating `fee_adjuster` off `User=root`:** All `lpt-*` units run as your unprivileged operator user (such as `admin`), never `root`. If an older `fee_adjuster.service` previously ran as `root`, reclaim ownership on any root-created runtime files once:

```bash
sudo chown admin:admin logs/fee-adjuster.log data/rebalance_targets_baseline.json ~/.chargelnd/.config/fee_adjuster.txt
```

## Uninstalling & Rollback

- **Per-tool or full uninstall:** `./install.sh uninstall fee-adjuster` or `./install.sh uninstall --all` stops/disables `lpt-*` units, cleans the managed `cron` block (leaving your own crontab lines verbatim), and preserves `config.ini`, `Magma/magma_config.ini`, `feeConfig.json`, `wallets.json`, `logs/`, and `data/`.
- **Manual rollback:** Pre-change snapshots of `crontab.bak` and migrated legacy unit files are stored in `.lpt-backups/<timestamp>/`.
