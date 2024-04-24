## Collection of Lightning Scripts

Those are most likely so highly custom tailored, that these are not really 1by1 useful to you.
However, sharing some of the code repositories might help you overcome some coding challenges, 
or inspire you to build on top of it.

### Current Scripts
LNDg: 
- `amboss_pull`: [cronjob, one-off] automatically gather your Magma Sell Orders and write those channel details into the GUI of LNDg. Optionally populate a file configuration for charge-lnd. Also optionally, trigger other settings in LNDg eg switch on AutoFee once maturity reached
- `channel_base-fee`: [cronjob, one-off] modify channel-settings in LNDg based on other LNDg fields. Eg change base-fee once a certain fee-condition is met.
- `channel_fee_pull`: [cronjob, one-off] retrieve LNDg channel details such as fee, base-fee and write a file for other systems to pick it up
- `swap_out_candidates`: [command-line output] pull current active channels with `-c CAPACITY` threshold locally and low fee on your side to provide good swap-out candidates. Export to .bos tags possible
- `mempool_rebalancer_trigger`: [systemd] check current mempool fee-rate for half-hour estimate, and if above your defined threshold (eg > 150sats/vByte), disable Auto-Fee setting in LNDg. Automatically activates again once threshold is below your limit.
- `disabled_fee-accelerator`: [cronjob] AutoFees in LNDg increase fees by incoming HTLCs. Dip below fee risk in LND encourage you to disable channels with you as opener and less than 5% liquidity on your side. Run this cronjob to automatically increase fee for disabled channels

Peerswap
- `peerswap-lndg_push`: [command-line output, cronjob] writes your existing PeerSwap peers info, the SUM of Sats and Swaps into LNDg Dashboard and Channel Card
- `ps_peers`: [command-line output] quick overview of existing L-BTC Balance and Peerswap Peers + Liquidity in a table format

Entries with [command-line output] provide further help-text with `-h` or `--help`

### === Installation Instructions ===
To run this script, you need a Python virtual environment. Follow the steps below:
1. Clone the repo with `git clone https://github.com/TrezorHannes/Lightning-Python-Tools.git`
2. `cd Lightning-Python-Tools/`
3. Install virtualenv (if not already installed):
   `$ sudo apt install virtualenv`
4. Create a virtual environment in the current directory:
   `$ virtualenv -p python3 .venv`
5. Activate the virtual environment:
   `$ source .venv/bin/activate`
6. Install the required dependencies using pip:
   `$ pip install -r requirements.txt`
7. Adjust your config file with `cp config.ini.example config.ini && nano config.ini`

### === Usage ===
To execute the script, make sure the virtual environment is activated:

   `$ source .venv/bin/activate`

or use the python binary in your nested .venv directory

   `$ .venv/bin/python3 LNDg/swap_out_candidates.py -h`

Then run the script using the following command:
- `$ .venv/bin/python3 Peerswap/ps_peers.py`
- `$ .venv/bin/python3 LNDg/amboss_pull.py`

### === Background services ===
Schedule services as a cronjob
- `$ crontab -e`
- `0 * * * * INSTALLDIR/Lightning-Python-Tools/.venv/bin/python3 INSTALLDIR/Lightning-Python-Tools/LNDg/amboss_pull.py && INSTALLDIR/Lightning-Python-Tools/.venv/bin/python3 INSTALLDIR/Lightning-Python-Tools/Peerswap/peerswap-lndg_push.py -a >> /home/admin/cron.log 2>&1`

Or if indicated in the script list above, create a systemd-unit file
```
[Unit]
Description=Mempool Rebalancer Trigger
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /path/to/mempool_rebalancer_trigger.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```
Save the file with a .service extension (e.g., `mempool_rebalancer_trigger.service`) and copy it to the /etc/systemd/system directory. Then, run the following commands:
```
sudo systemctl daemon-reload
sudo systemctl enable mempool_rebalancer_trigger.service
sudo systemctl start mempool_rebalancer_trigger.service
```

### === Optional: Create an Alias ===
To create an alias for convenient usage, add the following line to your `nano ~/.bash_aliases` file:
- `alias ps_list="INSTALLDIR/Lightning-Python-Tools/.venv/bin/python3 INSTALLDIR/Lightning-Python-Tools/Peerswap/ps_peers.py"`
- `alias lndg_amboss="INSTALLDIR/Lightning-Python-Tools/.venv/bin/python3 INSTALLDIR/Lightning-Python-Tools/LNDg/amboss_pull.py"`

### === How to obtain Telegram Infos ===
- Contact the @myidbot bot and send the /getid command to get your personal chat ID, or invite it into a group and use the /getgroupid command to get the group chat ID.
- Or get the chat ID directly from the bot API. Send your bot a command in the chat you want to use, then check https://api.telegram.org/bot{YourBotToken}/getUpdates, eg. https://api.telegram.org/botabcdefg:kjfsjf58FJkdjf49fj922fjkF/getUpdates

If you have any questions or need support, feel free to reach out:

Contact: https://njump.me/hakuna@tunnelsats.com