# Running Magma Auto-Sale Script with systemd

This document outlines how to run `Magma/magma_sale_process.py` as a persistent background service using `systemd` on Linux. Running under `systemd` starts the daemon automatically on boot and restarts it on transient failures while isolating dependencies inside the project virtual environment.

## Prerequisites

1. **Python 3.11+ & `venv`**:
   ```bash
   sudo apt update
   sudo apt install python3 python3-venv python3-pip
   ```

2. **Project Setup**:
   Clone `Lightning-Python-Tools` and enter the repository root:
   ```bash
   git clone https://github.com/TrezorHannes/Lightning-Python-Tools.git
   cd Lightning-Python-Tools
   ```

3. **Create and Activate Virtual Environment**:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

4. **Install Required Python Packages**:
   Install the pinned dependencies from `requirements.txt` (including hardened `certifi` and `urllib3` versions):
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

5. **Configuration File**:
   - Copy `config.ini.example` to `config.ini` in the root of `Lightning-Python-Tools`:
     ```bash
     cp config.ini.example config.ini
     chmod 600 config.ini
     ```
   - Populate `[credentials]` (`amboss_authorization`), `[telegram]` (`magma_bot_token`, `telegram_user_id`), `[paths]` (`lncli_path`), `[system]` (`full_path_bos`, `log_level`), `[pubkey]` (`banned_magma_pubkeys`), and `[magma]`.

6. **LND & BOS (Optional)**:
   Ensure `lncli` (and optionally `bos`) are accessible via the paths configured in `config.ini`.

7. **Log Directory**:
   The script automatically creates `logs/` in the repository root and writes rotating logs to `logs/magma-sale-process.log`. Ensure the service user has write permissions to the repository directory.

## systemd Service File

1. **Create the service file**:
   ```bash
   sudo nano /etc/systemd/system/magma-sale.service
   ```

2. **Paste the following content** (adjust `User`, `Group`, `WorkingDirectory`, `After`, and `ExecStart` to match your host):
   ```ini
   [Unit]
   Description=Magma Channel Auto-Sale Service
   After=network.target lnd.service

   [Service]
   Type=simple
   User=your_username
   Group=your_groupname

   # Repository root directory (required so config.ini and logs/ resolve cleanly)
   WorkingDirectory=/opt/lightning-tools
   Environment="SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt"
   Environment="REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt"

   # Absolute path to .venv python3 and Magma/magma_sale_process.py
   ExecStart=/opt/lightning-tools/.venv/bin/python3 /opt/lightning-tools/Magma/magma_sale_process.py

   Restart=always
   RestartSec=10
   StandardOutput=journal
   StandardError=journal
   SyslogIdentifier=magma-sale

   [Install]
   WantedBy=multi-user.target
   ```

   **Service file notes**:
   - `User` and `Group`: Run under a dedicated non-root user with access to `lncli` macaroons/TLS cert.
   - `WorkingDirectory`: Set to the root of `Lightning-Python-Tools` so `config.ini` and `logs/` resolve cleanly.
   - `Environment`: Binds `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` to the OS CA trust bundle (`/etc/ssl/certs/ca-certificates.crt`).
   - `ExecStart`: Must point to `.venv/bin/python3` inside your virtual environment.
   - `After=network.target lnd.service`: Adjust `lnd.service` if your LND unit uses a different name (for example `LND.service`).

## Enabling and Managing the Service

```bash
# Reload systemd unit definitions
sudo systemctl daemon-reload

# Enable on boot and start immediately
sudo systemctl enable --now magma-sale.service

# Check service status
sudo systemctl status magma-sale.service

# Follow live logs
sudo journalctl -u magma-sale.service -f
```

### Stopping or Restarting

```bash
sudo systemctl stop magma-sale.service
sudo systemctl restart magma-sale.service
```

## Troubleshooting

### Critical Error Flag Halts Bot

`magma_sale_process.py` writes a critical error flag file (`logs/magma_sale_process-critical-error.flag` relative to the repository root) to halt automated execution if an unrecoverable error occurs (such as a failed post-open Amboss channel point confirmation).

**Symptoms**:
- Telegram alert indicating startup or order processing halted due to a critical error flag.
- `journalctl -u magma-sale.service` reports that the critical error flag file exists and operations are suspended.

**Resolution**:
1. Inspect `logs/magma-sale-process.log` and `sudo journalctl -u magma-sale.service -n 100` to identify the root cause.
2. Resolve the underlying issue (for example, verify channel point state on Amboss or LND connectivity).
3. Remove the flag file:
   ```bash
   rm /opt/lightning-tools/logs/magma_sale_process-critical-error.flag
   ```
4. Restart the service:
   ```bash
   sudo systemctl restart magma-sale.service
   ```

## Telegram Bot Setup

To trigger order checks manually from Telegram, register the command with `@BotFather`:

1. Open Telegram, chat with `@BotFather`, send `/mybots`, and select your Magma bot.
2. Choose **Edit Bot** -> **Edit Commands** and send:
   ```text
   processmagmaorders - Manually checks for new Magma offers and processes paid orders.
   ```
3. Once set, the authorized user (`telegram_user_id` in `config.ini`) can send `/processmagmaorders` in Telegram to trigger an immediate order check cycle. Inline approval and rejection buttons for incoming orders are generated dynamically by the bot and require no extra BotFather configuration.

## Simulating a Magma Buy Order End-to-End

Because Magma sales are infrequent and Amboss does not provide a mainnet sandbox, an end-to-end simulation harness is included in `tests/simulate_magma_sale.py`.

The simulation exercises the order lifecycle without opening channels or spending real funds:
1. **Timeout Auto-Approval**: Ingests an order in `WAITING_FOR_SELLER_APPROVAL` with Amboss's unsettled zero-fee schema (`fees.seller.sats == 0`), verifies fee fallback calculation, queues confirmation, triggers timeout auto-approval, generates the seller invoice, and runs the Amboss acceptance flow.
2. **Manual Inline Callback Approval**: Simulates clicking the inline approval button, acknowledges the callback, updates message text, and runs the approval pipeline.
3. **Telegram Error Logging**: Dispatches a test exception to verify `TelegramLoggingHandler` traceback delivery.

### Offline Automated Simulation (Mock Mode)
Safe for CI and local testing without touching Telegram or external networks:

```bash
# Run standalone simulation suite
.venv/bin/python3 tests/simulate_magma_sale.py

# Or via pytest
.venv/bin/pytest tests/Magma/test_magma_simulation.py
```

### Live Interactive Telegram Simulation
To test live prompts, timeout countdowns, and error alerts in your Telegram client while keeping Amboss mutations and `lncli` calls mocked:

```bash
# Live interactive test with an 8-second timeout countdown
.venv/bin/python3 tests/simulate_magma_sale.py --live-telegram --timeout-seconds 8

# Live test of only the Telegram error logger stream
.venv/bin/python3 tests/simulate_magma_sale.py --live-telegram --scenario error
```
