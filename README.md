# Peerswap-Python-Tools

## First off, some scripts
You'll find a couple of python scripts I'm working on to simplify and integrate peerswap into my system. Currently un-organised, but shared publicly to ensure collaboration and peer-review.
Check out the dev-branch with things which are not fully working yet and want to get your hands on.

## Secondly, wonder how to install Elements & Peerswap on your Node?

For a couple of weeks, I'm [runnning Elements](https://github.com/ElementsProject/elements) on one of my nodes, to allow for swapping between Lightning Sats and L-BTC via [Peerswap](https://www.peerswap.dev/). This wasn't as straightforward for a few reasons, so this guide should offer some install- and best-practices in case you're interested too.

## Purpose
This is not to educate or convince you to try out [Peerswap](https://www.peerswap.dev/). There are a[^1] couple[^2] of links[^3] in the footnotes[^4] for you to dig into the *Why*, this guide here as said is more on the *How*.

If your Lightning-Node runs on Umbrel or Umbrel-OS, stop right here. You have to deal with enough stuff, but this guide is not one of them. Umbrel makes it easy to install **elementsd** (and a 1'000 dating apps) with a mouse-click. Follow [zapomatic's guide for Umbrel](https://github.com/zapomatic/zapomatic/blob/main/PeerSwap.md) and you'll be fine. You may catch up on "Tools" further below if you like.

For you **baremetal, raspibolt, raspiblitz** users out there, this guide is for you. My setup might trigger some curiosity, so follow along and let me know how to improve in your replies. Off we go!

## Disclaimer
Boring lawyer cat, but it's imminent that you're aware, this guide covers the usage of beta software, and it's important to note that beta versions may contain bugs or other issues. As of the creation of this guide, the software discussed is considered beta. Exercise caution and be aware that using beta software may carry inherent risks.

Additionally, if you are interacting with a mainnet that involves real satoshis (the smallest unit of Bitcoin), it is crucial to be intentional and well-informed about your actions. Real financial assets are involved, and any mistakes or unintended consequences may result in financial loss.

The information provided in this guide is for educational purposes only and should not be considered financial or legal advice. The author takes no liability for any actions, losses, or damages incurred as a result of following the guide. Users are encouraged to conduct their own research, seek professional advice, and proceed with caution when engaging with beta software and mainnet transactions.

By continuing with the steps outlined in this guide, you acknowledge and accept the risks involved, and you are solely responsible for your actions. There'll be dragons.

## Preambel
I have the following pre-setting, which makes it a little more complex, but since we want to tinker and learn, it's my personal preference:

 - Raspiblitz Pi5 with 2TB NVMe on Debian 12 to run **elementd**, the blockchain for L-BTC, as well as **peerswapd** for testing things earlier.
 - My main node 2TB raid just running bitcoind and LND, and installed **peerswapd** but connecting to the Pi5 elementd via RPC.

The benefit is: I try to keep other stuff running on my main as much as possible. This reduces attack vectors, as well as offering redundancy options: You could easily add another Pi running bitcoind + elements, but adding another NUC as my main would be a heavy cost factor.

Now let's finally get into the command-line. I'll indicate *Terminal 1 on my **Pi5*** as such, and *Terminal 2 on my main as **NUC***. `$` indicates a command, don't copy it in the codeblocks below.
I'll  show you how to run it on bitcoin / L-BTC mainnet, since we want to set it up for Lightning swaps. 
If you want to try out testnet, it's easier you follow [this guide from The Liquid Network](https://docs.liquid.net/docs/building-on-liquid) or [here for a code-tutorial](https://elementsproject.org/elements-code-tutorial/working-environment) directly.

## Install elements
#### Pi5
Let's create a new user, it's better for security reasons, and you can easier deinstall by just deleting the user `elements` in case we don't need it anymore. We'll also add it to the group bitcoin to ensure we can connect to it 
```
$ sudo adduser --disabled-password --gecos "" elements
$ sudo adduser admin elements
$ sudo adduser elements bitcoin
```
Create a directory for the blockchain. ~20GB should be sufficient for now, best to create it next to where your bitcoind blockchain is located too:
```
# raspiblitz has it here
$ sudo mkdir /mnt/hdd/elements
$ sudo chown elements:elements /mnt/hdd/elements

# or minibolt users here
$ sudo mkdir /data/elements
$ sudo chown elements:elements /data/elements
```
Download the [binaries](https://github.com/ElementsProject/elements/releases) or compile it by yourself ([detailed guide](https://elementsproject.org/elements-code-tutorial/installing-elements)). 
Change the `Version` to the most recent one:
```
$ cd /tmp
$ VERSION=23.2.1

# Raspi use this
$ wget https://github.com/ElementsProject/elements/releases/download/elements-$VERSION/elements-$VERSION-aarch64-linux-gnu.tar.gz
# For AMD64 use this instead
$ wget https://github.com/ElementsProject/elements/releases/download/elements-$VERSION/elements-$VERSION-x86_64-linux-gnu.tar.gz

$ wget https://github.com/ElementsProject/elements/releases/download/elements-$VERSION/SHA256SUMS.asc 
```
Let's checksum and verify
```
$ sha256sum --ignore-missing --check SHA256SUMS.asc
$ gpg --keyserver keyserver.ubuntu.com --recv-keys BD0F3062F87842410B06A0432F656B0610604482
$ gpg --verify SHA256SUMS.asc 
```
Extract and run elementsd to validate it's successful
```
$ tar  -xvf elements-$VERSION-*.tar.gz
$ sudo install -m 0755 -o root -g root -t /usr/local/bin elements-$VERSION/bin/elementsd elements-$VERSION/bin/elements-cli
$ elementsd -version
Elements Core version v23.2.1
Copyright (C) 2009-2023 The Elements Project developers
Copyright (C) 2009-2023 The Bitcoin Core developers
```
Let's make some notes before we switch to user elements. Since we  run next to your bitcoind, let's check the RPC details (handle them carefully), and check your local LAN details if you want another node to use elementsd

### Raspiblitz
```
$ cat .bitcoin/bitcoin.conf | grep rpc
rpcuser=raspibolt
rpcpassword=EXISTINGBITCOINDRPCPASSWD
main.rpcport=8332
rpcallowip=127.0.0.1
main.rpcbind=127.0.0.1:8332
```
### Minibolt
We should be able to connect to bitcoind via cookie-authentication. Check if your cookie has read-access for the group:
```
$ ls -la .bitcoin/.cookie 
-rw-r----- 1 bitcoin bitcoin 75 Jan 27 22:22 .bitcoin/.cookie
```
Now let's switch users and create the elements-config for mainnet
```
$ sudo su - elements
$ ln -s /data/elements /home/elements/.elements
# or this for the blitzusers
$ ln -s /mnt/hdd/elements /home/elements/.elements
```

`$ nano .elements/elements.conf`
```
# Avoid Memory drain for RaspberryPis
trim_headers=1

# Don't allow public incoming
listen=0

# bitcoind for raspibolt / raspiblitz
mainchainrpcport=8332
mainchainrpcuser=raspibolt
mainchainrpcpassword=EXISTINGBITCOINDRPCPASSWD

# bitcoind RPC access via cookie-auth.
# Either use the above or below, not both!
mainchainrpccookiefile=/data/bitcoin/.cookie

# General settings:
server=1
txindex=1
validatepegin=1
fallbackfee=0.00000100
# daemon=1

# RPC Access
port=7042
rpcport=7041
rpcuser=elements
rpcpassword=NEWPASSWDTOCONNECTRPCELEMENTS
# Allow your local Peerswap Service running on the same server to connect
rpcallowip=127.0.0.1
# Allow your local LAN only to connect to the RPC Port, in case you want to share the resource with other nodes
rpcallowip=192.168.1.0/24
rpcbind=0.0.0.0
```
Let's ensure only this user can read the config
`$ chmod 600 .elements/elements.conf`

Now comes the test, let's run elements daemon and check if the bitcoin-connection works
```
$ elementsd
2024-01-27T22:29:53Z Elements Core version v23.2.1 (release build)
2024-01-27T22:29:53Z InitParameterInteraction: parameter interaction: -listen=0 -> setting -upnp=0
2024-01-27T22:29:53Z InitParameterInteraction: parameter interaction: -listen=0 -> setting -natpmp=0
2024-01-27T22:29:53Z InitParameterInteraction: parameter interaction: -listen=0 -> setting -discover=0
2024-01-27T22:29:53Z InitParameterInteraction: parameter interaction: -listen=0 -> setting -listenonion=0
2024-01-27T22:29:53Z InitParameterInteraction: parameter interaction: -listen=0 -> setting -i2pacceptincoming=0
2024-01-27T22:29:53Z Validating signatures for all blocks.
2024-01-27T22:29:53Z Setting nMinimumChainWork=0000000000000000000000000000000000000000000000000000000000000000
2024-01-27T22:29:53Z Configured for header-trimming mode. This will reduce memory usage substantially, but we will be unable to serve as a full P2P peer, and certain header fields may be missing from JSON RPC output.
2024-01-27T22:29:53Z Using the 'x86_shani(1way,2way)' SHA256 implementation
2024-01-27T22:29:53Z Using RdSeed as additional entropy source
2024-01-27T22:29:53Z Using RdRand as an additional entropy source
2024-01-27T22:29:53Z Default data directory /home/elements/.elements
2024-01-27T22:29:53Z Using data directory /home/elements/.elements/liquidv1
2024-01-27T22:29:53Z Config file: /home/elements/.elements/elements.conf
2024-01-27T22:29:53Z Config file arg: fallbackfee="0.00000100"
2024-01-27T22:29:53Z Config file arg: listen="0"
2024-01-27T22:29:53Z Config file arg: mainchainrpccookiefile="/data/bitcoin/.cookie"
2024-01-27T22:29:53Z Config file arg: port="7042"
2024-01-27T22:29:53Z Config file arg: rpcallowip="127.0.0.1"
2024-01-27T22:29:53Z Config file arg: rpcbind=****
2024-01-27T22:29:53Z Config file arg: rpcpassword=****
2024-01-27T22:29:53Z Config file arg: rpcport="7041"
2024-01-27T22:29:53Z Config file arg: rpcuser=****
2024-01-27T22:29:53Z Config file arg: server="1"
```
If that works, we can stop it with CTRL-C and better run it in the background. 

For this, we exit user `elements` with `exit` and create a systemd file with `admin` to run elements-daemon as system-service and auto-start on reboots. 
`$ sudo nano /etc/systemd/system/elementsd.service`
```
[Unit]
Description=Elements daemon on mainnet

Requires=bitcoind.service
After=bitcoind.service

[Service]
# Raspibolt
ExecStart=/usr/local/bin/elementsd -datadir=/mnt/hdd/elements/
# Minibolt
# ExecStart=/usr/local/bin/elementsd -datadir=/data/elements/
PermissionsStartOnly=true

# Process management
####################
Restart=on-failure
TimeoutStartSec=infinity
TimeoutStopSec=600

# Directory creation and permissions
####################################
User=elements
Group=elements

StandardOutput=null
StandardError=journal

# Hardening measures
####################
# Provide a private /tmp and /var/tmp.
PrivateTmp=true
# Mount /usr, /boot/ and /etc read-only for the process.
ProtectSystem=full
# Deny access to /home, /root and /run/user
# ProtectHome=true
# Disallow the process and all of its children to gain
# new privileges through execve().
NoNewPrivileges=true
# Use a new /dev namespace only populated with API pseudo devices
# such as /dev/null, /dev/zero and /dev/random.
PrivateDevices=true
# Deny the creation of writable and executable memory mappings.
MemoryDenyWriteExecute=true

[Install]
WantedBy=multi-user.target
```
Now we enable the service with
```
$ sudo systemctl enable elementsd
$ sudo systemctl start elementsd
$ sudo systemctl status elementsd
# check the debug.log of elementsd to see how the sync process works with
$ tail -f .elements/liquidv1/debug.log
```
## Install Peerswap
While the L-BTC Blockchain syncs, which will take a day or so depending on your hardware, we can install Peerswap. We could follow [Zap-o-Matic's guide here](https://github.com/zapomatic/zapomatic/blob/main/PeerSwap.md#install-peerswap) if we plan to run all of it on the same node. But since I actually access elements from another server running my main node, we'll add the same blops here, but with small adjustments to establish this remote access.

### Pre-Setup
#### NUC
We need **go** installed to build Peerswap. Check if you have it `$ go version`, if not:

 - For a Pi User, follow [this guide](https://raspibolt.org/guide/bonus/raspberry-pi/go.html)
 - For an AMD64 user, amend the sources of the guide above, or [follow this one](https://go.dev/doc/install)

Add `build-essentials`, a bunch of tools necessary to do the next step
```
$ sudo apt update
$ sudo apt install build-essential -y
```
We need the user elements here as well if you run this on another server. This time, it needs to have access to your tls.cert and admin.macaroon, since it'll be able to invoice and send sats. This is an important security aspect, so always ensure you proof-read the scripts and guides like this one here not doing any shenigens. 
```
$ sudo adduser --disabled-password --gecos "" elements
$ sudo adduser admin elements
$ sudo adduser elements bitcoin
```
Switch to user elements with `$ sudo su - elements`, clone the public repo and build it. We set the $GOPATH to /usr/local/go/bin, since I want to use pscli as admin user later on and not switch to elements all the time I use it.
```
$ echo 'export PATH=$PATH:/usr/local/go/bin' >> ~/.profile
$ echo 'export GOPATH=/usr/local/go/bin' >> ~/.profile
$ . .profile
$ git clone https://github.com/ElementsProject/peerswap.git && \
$ cd peerswap && \
$ make lnd-release
``` 
Before creating the peerswap-config file, let's check whether we have access to `tls.cert` and `admin.macaroon`
```
# For Raspiblitz
$ ln -s /mnt/hdd/app-data/lnd ~/.lnd
# For Raspibolt / Minibolt
$ ln -s /data/lnd ~/.lnd

$ cat .lnd/tls.cert
$ cat .lnd/data/chain/bitcoin/mainnet/admin.macaroon
```
Okay, we have this sorted, let's ensure that `pscli` can access our elements server (either locally, or via Local Area Network / LAN)
```
$ mkdir -p ~/.peerswap
$ nano ~/.peerswap/peerswap.conf
```
This is the content of `peerswap.conf`. Ensure to adjust to your local setup and use a secure password
```
lnd.tlscertpath=/home/elements/.lnd/tls.cert
lnd.macaroonpath=/home/elements/.lnd/data/chain/bitcoin/mainnet/admin.macaroon
elementsd.rpcuser=elements
# Replace the password with what you set in elements.conf up above. Don't use this one, it's not secure.
elementsd.rpcpass=NEWPASSWDTOCONNECTRPCELEMENTS
# Set the below to 127.0.0.1 if you're connecting to elements running on the same server
# My config connects to my Pi5 with elementsd.rpchost=http://raspiblitz5
elementsd.rpchost=http://127.0.0.1
elementsd.rpcport=7041
elementsd.rpcwallet=peerswap
elementsd.liquidswaps=true
# set the below to true if you're fine doing swaps with L-BTC as well as BTC
bitcoinswaps=false
```
Stay with me, we're almost done. Try to run it with your config saved:
```
$ peerswapd 
2024/01/28 19:09:47 [INFO] PeerSwap LND starting up with commit 616764cdedaf429e0647bb25b7dd8a4abdcfd838 and cfg: Host localhost:42069, ConfigFile /home/elements/.peerswap/peerswap.conf, Datadir /home/elements/.peerswap, Bitcoin enabled: true, Lnd Config: host: localhost:10009, macaroonpath /home/elements/.lnd/data/chain/bitcoin/mainnet/admin.macaroon, tlspath /home/elements/.lnd/tls.cert, elements: elements: rpcuser: elements, rpchost: http://127.0.0.1, rpcport 7041, rpcwallet: peerswap, liquidswaps: true
```
CTRL-C to stop the service and `exit` to go back to admin.
To access this server from another server in your LAN, you'll need to ensure TCP-port 7041 is allowed. With UFW, it'd work like this
```
$ sudo ufw allow from 192.168.86.0/24 to any port 7041 proto tcp comment 'Elementsd RPC LAN'
```
Now since we checked peerswapsd running fine, we can create the background service as well
`$ sudo nano /etc/systemd/system/peerswapd.service`

```
[Unit]
Description=Peer Swap Daemon
[Service]
ExecStart=/usr/local/bin/peerswapd
User=peerswap
Type=simple
KillMode=process
TimeoutSec=180
Restart=always
RestartSec=60
[Install]
WantedBy=multi-user.target
```
and start it
```
$ sudo systemctl enable peerswapd 
$ sudo systemctl start peerswapd 
$ sudo systemctl status peerswapd
```
Now it's running, and with `$ pscli` the world of rebalancing via L-BTC (and BTC) opens up to you. Follow [this link](https://github.com/zapomatic/zapomatic/blob/main/PeerSwap.md#test-and-initiate-swap) for a couple of pointers how to initiate your first swap.

## Wrap-up
There is plenty of more stuff to do, it's super early days, but if you want to continue to tag along, reach out to the peerswappers. We have a Telegram Group (invite only) and a [Discord](https://discord.com/invite/wpNv3PG8G2) where we exchange how to build up for the future. 
For now, I leave you with a couple more important items

**Backup**
There is no seed-list on L-BTC. Make a backup of your wallet as user `elements`, pack it, encode it, and place `elements-wallet.tar.gz.enc` somewhere safely:
```
$ elements-cli backupwallet "/home/elements/elements-backup.dat"
$ tar -czvf elements-wallet.tar.gz elements-backup.dat
$ openssl enc -aes-256-cbc -salt -in elements-wallet.tar.gz -out elements-wallet.tar.gz.enc
$ rm elements-backup.dat elements-wallet.tar.gz
```
**Frontend**
There is [a frontend](https://github.com/Impa10r/peerswap-web), which I haven't installed since I prefer the commandline. But it looks great and continously gets improved

**Tools**
I'm working on a couple of python tools to make things easier to manage. This guide, as well as those scripts, [can be found here on my GH repo **[Peerswap-Python-Tools]**](https://github.com/TrezorHannes/Peerswap-Python-Tools).

Lastly, I'm not in any way affiliated. I just think it's another tool in our set of armory to ensure a healthy and profitable ecosystem in lightning is possible for every runner out there. If this guide was of any help, I'd appreciate if you share the article with others, give me a follow on X [![Twitter URL](https://img.shields.io/twitter/url/https/twitter.com/HandsdownI.svg?style=social&label=Follow%20%40HodlmeTight1337)](https://twitter.com/HodlmeTight1337) or [nostr](https://njump.me/npub1ch25m5lkk8kfepr63f0jnpd9te8l9f585pfpr2g2ma4pre9rmlrqlu0yjy), perhaps even donating some sats to [hakuna@getalby.com](https://getalby.com/p/hakuna)

I'm also always grateful for incoming channels to my node: [HODLmeTight](https://amboss.space/node/037f66e84e38fc2787d578599dfe1fcb7b71f9de4fb1e453c5ab85c05f5ce8c2e3)

[^1]: [Liquid Rebalancing of Lightning Channels](https://medium.com/@goryachev/liquid-rebalancing-of-lightning-channels-2dadf4b2397a) by Vlad Goryachev
[^2]: [PeerSwap](https://www.peerswap.dev/) - P2P BTC LN Balancing Protocol
[^3]: [PeerSwap Economics: Comparison with LOOP](https://stacker.news/items/382449/r/Hakuna) (and the problem with Rebalancing) by zapomatic
[^4]:[Launching Liquid Swaps - Unfairly Cheap Rebalancing of Your Lightning Node!](https://blog.boltz.exchange/p/launching-liquid-swaps-unfairly-cheap) ⚡🌊
 
