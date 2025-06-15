# Running Magma Auto-Sale Script with systemd

This document outlines how to run the `magma_sale_process.py` script as a persistent background service using `systemd` on a Linux system. This ensures the script automatically starts on boot and restarts if it encounters issues, utilizing a Python virtual environment for dependency management.

## Prerequisites

1.  **Python 3 & virtualenv:** Ensure Python 3 and the `virtualenv` package are installed on your system.
    ```bash
    sudo apt update
    sudo apt install python3 python3-pip virtualenv
    ```
2.  **Project Setup:**
    *   Clone or download the `Lightning-Python-Tools` repository if you haven't already.
        ```bash
        # Example:
        # git clone https://github.com/TrezorHannes/Lightning-Python-Tools.git
        # cd Lightning-Python-Tools
        ```
    *   Navigate to the root of the `Lightning-Python-Tools` directory.

3.  **Create and Activate Virtual Environment:**
    It's highly recommended to use a Python virtual environment.
    ```bash
    # In the Lightning-Python-Tools directory
    virtualenv -p python3 .venv
    source .venv/bin/activate
    ```
    Your shell prompt should change to indicate the virtual environment is active (e.g., `(.venv) your-user@host:...$`).

4.  **Install Required Python Packages:**
    With the virtual environment active, install the necessary packages:
    ```bash
    # Ensure your virtual environment is active
    pip install pyTelegramBotAPI requests schedule
    ```
    (Note: If there's a `requirements.txt` specific to Magma or the broader project that includes these, you might use `pip install -r requirements.txt` instead).

5.  **Script Location:** The `magma_sale_process.py` script is expected to be in the `Magma/` subdirectory (e.g., `/opt/lightning-tools/Magma/magma_sale_process.py`).

6.  **Configuration File:**
    *   Copy `config.ini.example` to `config.ini` in the root of the `Lightning-Python-Tools` directory (e.g. `/opt/lightning-tools/config.ini`).
    *   Fill in all required details in `config.ini` (API tokens, Telegram bot info, LND/BOS paths, etc.).

7.  **LND & BOS (Optional):** Ensure `lncli` (and `bos` if used) are accessible, either in the system's PATH or by providing the full path in `config.ini`.
8.  **Log Directory:** The script will attempt to create a `logs` directory in its parent directory (e.g., `/opt/lightning-tools/logs/`). Ensure the user running the script has write permissions there.

## systemd Service File

Create a systemd service file to manage the script.

1.  **Create the service file:**
    Open a new file named `magma-sale.service` in `/etc/systemd/system/`:
    ```bash
    sudo nano /etc/systemd/system/magma-sale.service
    ```

2.  **Paste the following content:**
    Adjust `User`, `Group`, `WorkingDirectory`, and `ExecStart` paths according to your setup. The `ExecStart` path **must** point to the Python interpreter inside your virtual environment.

    ```ini
    [Unit]
    Description=Magma Channel Auto-Sale Service
    # Add other dependencies if LND runs as a service
    After=network.target LND.service

    [Service]
    Type=simple
    # Replace with the user that should run the script
    User=your_username
    # Replace with the group for the user
    Group=your_groupname

    # Set the working directory to the root of your Lightning-Python-Tools project
    # This ensures relative paths like config.ini and logs/ work correctly,
    # and that Magma/magma_sale_process.py can be found.
    # Example: if your project is at /opt/lightning-tools/
    WorkingDirectory=/opt/lightning-tools/

    # Path to the Python interpreter IN YOUR VIRTUAL ENVIRONMENT and the script
    # Example: /opt/lightning-tools/.venv/bin/python3 /opt/lightning-tools/Magma/magma_sale_process.py
    ExecStart=/opt/lightning-tools/.venv/bin/python3 /opt/lightning-tools/Magma/magma_sale_process.py

    Restart=always
    RestartSec=10
    StandardOutput=journal
    StandardError=journal
    SyslogIdentifier=magma-sale

    [Install]
    WantedBy=multi-user.target
    ```

    **Notes on the service file:**
    *   `User` and `Group`: It's recommended to run services under a dedicated non-root user.
    *   `WorkingDirectory`: Crucial for the script to find `config.ini` (in this directory) and the `logs` directory (also in this directory), and the script itself in the `Magma/` subdirectory. If your project is at `/home/admin/tools/Lightning-Python-Tools/`, then `WorkingDirectory` should be `/home/admin/tools/Lightning-Python-Tools/`.
    *   `ExecStart`: **Critically important.** This must be the absolute path to the `python3` executable *within your `.venv` directory*, followed by the absolute path to `Magma/magma_sale_process.py`.
    *   `After=network.target LND.service`: If your LND node runs as a systemd service (e.g., `LND.service`), adding it here ensures Magma starts after LND. Adjust service name if needed.
    *   `Restart=always`: Ensures the script restarts automatically.
    *   `StandardOutput=journal` and `StandardError=journal`: Directs script output to the systemd journal.

3.  **Save and close the file.**

## Enabling and Managing the Service

1.  **Reload systemd daemon:**
    This makes systemd aware of the new service file.
    ```bash
    sudo systemctl daemon-reload
    ```

2.  **Enable the service:**
    This makes the service start automatically on boot.
    ```bash
    sudo systemctl enable magma-sale.service
    ```

3.  **Start the service:**
    ```bash
    sudo systemctl start magma-sale.service
    ```

4.  **Check the status of the service:**
    ```bash
    sudo systemctl status magma-sale.service
    ```
    You should see output indicating the service is active (running).

5.  **View logs:**
    To view the logs generated by the script (which are now directed to the journal):
    ```bash
    sudo journalctl -u magma-sale.service -f
    ```
    (Use `-f` to follow the logs in real-time. Remove `-f` to see all past logs. Add `-n LINES` (e.g., `-n 100`) to see the last N lines.)

## Stopping or Restarting the Service

*   **To stop the service:**
    ```bash
    sudo systemctl stop magma-sale.service
    ```
*   **To restart the service (e.g., after updating the script or config.ini):**
    ```bash
    sudo systemctl restart magma-sale.service
    ```

## Troubleshooting

### Critical Error Flag Halts Bot

The script uses a critical error flag file (`logs/magma_sale_process-critical-error.flag` relative to `WorkingDirectory`) to halt operations if a systemic or unrecoverable error occurs. This requires manual intervention.

**Symptoms:**
*   The bot sends a Telegram message like "☠️ Magma Bot STARTUP FAILED: Critical error flag found..."
*   The `magma-sale.service` status or logs (via `journalctl -u magma-sale.service`) will indicate that the flag file exists and the bot has suspended operations.

**Resolution:**
1.  **Investigate the Logs:** Check the script's log file (`logs/magma-sale-process.log`) and the systemd journal (`journalctl -u magma-sale.service`) for details about the error that caused the flag to be created.
2.  **Resolve the Underlying Issue:** Address the root cause of the error. This might involve code changes, configuration updates, or fixing external dependencies.
3.  **Delete the Flag File:** Once the issue is believed to be resolved, manually delete the flag file.
    ```bash
    # Example, adjust path if your WorkingDirectory is different
    rm /opt/lightning-tools/logs/magma_sale_process-critical-error.flag
    # Or from within the WorkingDirectory:
    # rm logs/magma_sale_process-critical-error.flag
    ```
4.  **Restart the Service:**
    ```bash
    sudo systemctl restart magma-sale.service
    ```

This setup provides a robust way to run your Magma auto-sale script using best practices for Python environments. Remember to adjust paths and usernames in the example service file to match your specific environment.

## Telegram Bot Setup

To allow manual triggering of the Magma sale process via Telegram, you need to configure your bot with BotFather.

1.  **Open Telegram and find BotFather:** If you haven't already, start a chat with `@BotFather`.
2.  **Edit your bot's commands:**
    *   Send `/mybots` to BotFather and select your Magma bot from the list.
    *   Click on "Edit Bot".
    *   Click on "Edit Commands".

3.  **Set the command:**
    BotFather will ask you to send the list of commands. For this script, you only need to register one command. Send the following text to BotFather:

    ```
    processmagmaorders - Manually checks for new Magma offers and processes paid orders.
    ```

    *   `processmagmaorders` is the command itself (without the leading `/`).
    *   The text after the hyphen is the description that users will see in Telegram when they type `/`.

Once set, users (specifically the `telegram_user_id` configured in your `config.ini`) will be able to use the `/runnow` command to trigger the script's main processing logic on demand. The script also uses inline keyboard buttons for approving or rejecting new offers; these do not need to be registered as commands with BotFather as they are contextually generated by the bot.
