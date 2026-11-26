# Troubleshooting & Diagnostics

Run the built-in diagnostic command first whenever something fails to install, validate, or execute:

```bash
./install.sh doctor
```

## `doctor` Checks Explained

| Check | Meaning | How to Fix |
| :--- | :--- | :--- |
| `python` | Host Python must be `3.11`, `3.12`, or `3.13`. | Install Python 3.11–3.13 (`sudo apt-get install -y python3 python3-venv`). |
| `venv-certifi` | Checks `.venv` exists and `certifi >= 2026.10.19` (`requirements.txt` requires `>= 2026.11.20`). | Run `./install.sh env` (or `./install.sh env --recreate`). |
| `ca-bundle` | Verifies an OS CA certificate bundle exists (`/etc/ssl/certs/ca-certificates.crt`, `/etc/pki/tls/certs/ca-bundle.crt`, or `/etc/ssl/cert.pem`). | On Debian/Ubuntu, run `sudo apt-get install -y ca-certificates`. |
| `perms:<file>` | Flags config files that are group/world readable. | Run `./install.sh config` or `chmod 600 <file>`. |
| `ownership` | Flags root-owned files in `logs/` or `data/` (common after switching `fee_adjuster` from `User=root` to `User=admin`). | Fix with `sudo chown "$USER:$USER" logs/* data/*`. |
| `legacy-units` | Detects pre-existing unprefixed systemd units (`fee_adjuster.*`, `pocketmoney.*`, etc.). | Run `./install.sh install <tool>` to back up and migrate to `lpt-*`. |

---

## Common Issues

### 1. Installer Refuses with "unmanaged duplicate execution detected"
`lpt_installer` fails closed if the script already appears in:
- a custom unit under `/etc/systemd/system/` or `~/.config/systemd/user/`,
- an unmanaged entry in `crontab -l`, or
- an active process when no known legacy unit is registered for migration.

**Fix:** Inspect the reported unit/cron line/PID, stop or remove the duplicate manually (`crontab -e` or `sudo systemctl disable --now <unit>`), then re-run `./install.sh install ...`.

### 2. Inspecting Logs (`systemd` vs `cron`)
- **Systemd services & timers:**
  ```bash
  sudo systemctl status lpt-fee-adjuster.timer lpt-fee-adjuster.service
  journalctl -u lpt-fee-adjuster -n 100 --no-pager
  ```
- **Managed cron jobs:**
  ```bash
  tail -n 100 logs/cron_fee-adjuster.log
  ```

### 3. `magma_sale_process.py` Halted with `logs/magma_sale_process-critical-error.flag`
If `Magma/magma_sale_process.py` encounters an unrecoverable `litloop` UTXO query failure (so it does not accidentally spend reserved static-loop UTXOs), it writes `logs/magma_sale_process-critical-error.flag` and aborts.
1. Inspect `logs/magma-sale-process.log` and confirm `loop` / `litd` status and `[loop]` `rpcserver` / `tlscertpath` settings in `config.ini`.
2. Delete `logs/magma_sale_process-critical-error.flag` once resolved, then restart `sudo systemctl restart lpt-magma-sale.service`.

### 4. `crontab binary not found`
Minimal containers or systemd-only images without `cron` installed support `--scheduler systemd` cleanly. To use `--scheduler cron`, install `cron` first (`sudo apt-get install -y cron`).

### 5. Rolling Back a Change
Before mutating `systemd` or `cron`, `./install.sh` saves your previous `crontab.bak` and any migrated legacy unit files under `.lpt-backups/<timestamp>/` (`chmod 700`). To revert completely:

```bash
./install.sh uninstall --all
# Optionally restore an earlier crontab snapshot:
crontab .lpt-backups/<timestamp>/crontab.bak
```
