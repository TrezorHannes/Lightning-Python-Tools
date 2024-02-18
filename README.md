## Collection of Lightning Scripts

Those are most likely so highly custom tailored, that these are not really 1by1 useful to you.
However, sharing some of the code repositories might help you overcome some coding challenges, 
or inspire you to build on top of it.

### Current Scripts
- LNDg: A couple of scripts which help further automation. For instance, a script which automatically gathers your Magma Sell Orders and write those channel details into the GUI of LNDg
- Peerswap: A few scripts which either help you navigate PS offers via CLI or also integrate PS infos into LNDg via  Django API

### === Installation Instructions ===
To run this script, you need a Python virtual environment. Follow the steps below:
1. Install virtualenv (if not already installed):
   `$ sudo apt install virtualenv`
2. Create a virtual environment in the current directory:
   `$ virtualenv -p python3 .venv`
3. Activate the virtual environment:
   `$ source .venv/bin/activate`
4. Install the required dependencies using pip:
   `$ pip install -r requirements.txt`

### === Usage ===
To execute the script, make sure the virtual environment is activated:
   `$ source .venv/bin/activate`

Then run the script using the following command:
- `$ .venv/bin/python3 Peerswap/ps_peers.py`
- `$ .venv/bin/python3 LNDg/amboss_pull.py`

### === Optional: Create an Alias ===
To create an alias for convenient usage, add the following line to your .bash_aliases file:
- `alias ps_list="INSTALLDIR/.venv/bin/python3 INSTALLDIR/Peerswap/ps_peers.py"`
- `alias lndg_amboss="INSTALLDIR/.venv/bin/python3 INSTALLDIR/LNDg/amboss_pull.py"`
