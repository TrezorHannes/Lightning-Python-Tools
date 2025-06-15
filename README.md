## Collection of Lightning Scripts

Welcome! This repository contains a collection of Python scripts designed to interact with various Lightning Network tools and services. While some scripts are tailored for specific setups, they can serve as a helpful starting point or inspiration for your own projects.

### Current Scripts

Below is a list of available scripts and their primary functions. Scripts marked with `[command-line output]` typically offer more detailed help if you run them with the `-h` or `--help` flag.

**LNDg:**
- `amboss_pull.py`: [cronjob, one-off] Automatically gathers your Amboss Magma Sell Orders and writes channel details into the LNDg GUI. Optionally populates a file configuration for charge-lnd and can trigger other settings in LNDg (e.g., activate AutoFee once maturity is reached).
- `channel_base-fee.py`: [cronjob, one-off] Modifies channel settings in LNDg based on other LNDg fields. For example, it can change the base fee once a certain fee condition is met.
- `channel_fee_pull.py`: [cronjob, one-off] Retrieves LNDg channel details such as fee rate and base fee, then writes this information to a file for other systems to use.
- `swap_out_candidates.py`: [command-line output] Identifies active channels with a local balance above a specified `--capacity` threshold and low local fees, making them good candidates for submarine swaps (swapping out). Can export to .bos tags format.
- `mempool_rebalancer_trigger.py`: [systemd service] Monitors the current mempool fee rate for a half-hour estimate. If the fee rate exceeds your defined threshold (e.g., > 150 sats/vByte), it disables the Auto-Fee setting in LNDg. It automatically re-activates Auto-Fee once the fee rate drops below your limit.
- `disabled_fee-accelerator.py`: [cronjob] LNDg's AutoFees feature increases fees based on incoming HTLCs. This script helps manage channels that LNDg might suggest disabling due to low outbound liquidity by automatically increasing their fees.

**Magma (Amboss):**
- 🆕 `magma-sale.py`: [systemd service] Provides automated monitoring of channel sales on Amboss Magma. It handles order clearance, channel opening, fee management, and writes relevant information into LNDg notes. Refactored and documented in `magma-sale.MD`.



**Peerswap:**
- `peerswap-lndg_push.py`: [command-line output, cronjob] Gathers information about your existing PeerSwap peers, including the sum of satoshis swapped and the number of swaps, and writes this data to the LNDg Dashboard and relevant Channel Cards.
- `ps_peers.py`: [command-line output] Offers a quick tabular overview of your L-BTC Balance and PeerSwap peers, including their liquidity.

**LNBits:**
- `pocketmoney.py`: [one-off, cronjob, systemd service] Enables recurring payments to child wallets within the same LNBits instance. You can define the fiat currency, recurrence schedule, and amount for each child. To configure, copy `config.ini.example` to `config.ini` and `wallets.json.example` to `wallets.json`, then edit both new files.

**Other:**
- `swap_wallet.py`: [one-off] Sends a specified amount of Lightning funds to a given LN address. Allows customization of total amount, amount per transaction, interval between transactions, maximum fee rate, and an optional message for the payments.
- `fee_adjuster.py`: [systemd service, cronjob] Automatically adjusts channel fees based on Amboss API data and user-defined settings. Requires a running LNDg instance to retrieve local channel details. Configure via `feeConfig.json` and `config.ini`. Install using `sudo ./Other/install_fee_adjuster_service.sh` or run as a cron job.
- `boltz_swap-out.py`: [command-line output, one-off] Automates Lightning Network (LN) to Liquid Bitcoin (L-BTC) swaps using Boltz for submarine swaps (swapping out).

### === Installation Instructions ===

To use these scripts, it's recommended to set up a Python virtual environment. This keeps dependencies for this project isolated from other Python projects on your system.

1.  **Clone the Repository:**
    If you haven't already, download the scripts to your machine:
    ```bash
    git clone https://github.com/TrezorHannes/Lightning-Python-Tools.git
    cd Lightning-Python-Tools/
    ```

2.  **Install `virtualenv` (if you don't have it):**
    `virtualenv` is a tool to create isolated Python environments.
    ```bash
    sudo apt update
    sudo apt install virtualenv
    ```

3.  **Create a Virtual Environment:**
    Inside the `Lightning-Python-Tools` directory, create a virtual environment (commonly named `.venv`):
    ```bash
    virtualenv -p python3 .venv
    ```
    This creates a `.venv` folder in your project directory.

4.  **Activate the Virtual Environment:**
    Before you can use the scripts or install packages, you need to activate the environment:
    ```bash
    source .venv/bin/activate
    ```
    Your shell prompt will usually change to indicate that the virtual environment is active (e.g., `(.venv) your-user@host:...$`).

5.  **Install Required Dependencies:**
    With the virtual environment active, install the necessary Python packages listed in `requirements.txt`:
    ```bash
    pip install -r requirements.txt
    ```

6.  **Configure the Scripts:**
    Many scripts rely on a `config.ini` file for settings like API keys and paths.
    Copy the example configuration and edit it with your details:
    ```bash
    cp config.ini.example config.ini
    nano config.ini
    ```
    For scripts that use other configuration files (like `wallets.json` or `feeConfig.json`), follow the same pattern: copy the `.example` file and edit the copy.

### === Basic Usage ===

1.  **Activate Virtual Environment (if not already active):**
    Each time you open a new terminal window to run these scripts, you'll need to activate the environment:
    ```bash
    cd path/to/Lightning-Python-Tools/  # Navigate to the project directory
    source .venv/bin/activate
    ```

2.  **Run a Script:**
    Execute scripts using the Python interpreter within your virtual environment. For example:
    ```bash
    python3 Peerswap/ps_peers.py
    python3 LNDg/amboss_pull.py
    python3 Other/boltz_swap-out.py --amount 100000 --capacity 2000000
    ```
    Many scripts provide help with the `-h` or `--help` flag:
    ```bash
    python3 LNDg/swap_out_candidates.py -h
    ```

### === Running Scripts as Background Services ===

Some scripts are designed to run continuously or on a schedule.

**1. Cron Jobs:**
For scripts that need to run periodically (e.g., every hour), you can use `cron`.
Edit your crontab:
```bash
crontab -e
```
Add a line similar to this, replacing `INSTALLDIR` with the absolute path to your `Lightning-Python-Tools` directory:
```cron
0 * * * * /INSTALLDIR/Lightning-Python-Tools/.venv/bin/python3 /INSTALLDIR/Lightning-Python-Tools/LNDg/amboss_pull.py >> /home/admin/cron.log 2>&1
```
This example runs `amboss_pull.py` at the start of every hour and logs its output.

**2. Systemd Services:**
For scripts intended to run as long-running background services (often indicated with `[systemd service]` in the script list), you can create a `systemd` unit file.

*   Create a `.service` file (e.g., `mempool_rebalancer_trigger.service`):
    ```ini
    [Unit]
    Description=Mempool Rebalancer Trigger Service
    After=network.target

    [Service]
    Type=simple
    User=your_username # Replace with the user that should run the script
    WorkingDirectory=/path/to/Lightning-Python-Tools/LNDg # Adjust to script's directory
    ExecStart=/path/to/Lightning-Python-Tools/.venv/bin/python3 /path/to/Lightning-Python-Tools/LNDg/mempool_rebalancer_trigger.py
    Restart=on-failure
    RestartSec=5s

    [Install]
    WantedBy=multi-user.target
    ```
    **Important:** Replace `/path/to/` with the actual absolute path to your `Lightning-Python-Tools` directory and `your_username` with the appropriate user. Adjust `WorkingDirectory` and `ExecStart` paths for the specific script.

*   Copy the file to the systemd directory:
    ```bash
    sudo cp mempool_rebalancer_trigger.service /etc/systemd/system/
    ```

*   Reload systemd, enable the service (to start on boot), and start it:
    ```bash
    sudo systemctl daemon-reload
    sudo systemctl enable mempool_rebalancer_trigger.service
    sudo systemctl start mempool_rebalancer_trigger.service
    ```

*   You can check its status with:
    ```bash
    sudo systemctl status mempool_rebalancer_trigger.service
    ```

### === Optional: Create Command Aliases ===

For easier access to frequently used scripts, you can create aliases in your shell's configuration file (e.g., `~/.bash_aliases` or `~/.bashrc`).

1.  Open the file (e.g., `nano ~/.bash_aliases`).
2.  Add lines like these, replacing `INSTALLDIR` with the absolute path to your `Lightning-Python-Tools` directory:
    ```bash
    alias ps_list="INSTALLDIR/Lightning-Python-Tools/.venv/bin/python3 INSTALLDIR/Lightning-Python-Tools/Peerswap/ps_peers.py"
    alias lndg_amboss="INSTALLDIR/Lightning-Python-Tools/.venv/bin/python3 INSTALLDIR/Lightning-Python-Tools/LNDg/amboss_pull.py"
    ```
3.  Save the file and apply the changes (e.g., `source ~/.bash_aliases` or open a new terminal).

### === How to Obtain Telegram Bot Information ===

Some scripts might use Telegram for notifications. To get the necessary IDs:
-   **Personal Chat ID:** Contact the `@myidbot` on Telegram and send the `/getid` command.
-   **Group Chat ID:** Add `@myidbot` to your Telegram group and use the `/getgroupid` command.
-   **Alternative (Bot API):** Send your bot any message in the desired chat. Then, visit `https://api.telegram.org/bot{YourBotToken}/getUpdates` (replacing `{YourBotToken}` with your actual bot token) in a web browser. You'll find the chat ID in the JSON response.

---

If you have questions or need support, feel free to reach out.
Contact: <https://njump.me/hakuna@tunnelsats.com>