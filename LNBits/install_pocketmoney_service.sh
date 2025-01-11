#!/bin/bash

# --- Documentation ---
# Script Name: install_pocketmoney_service.sh
# Description: Installs a systemd service for the pocketmoney.py script.
# Author: @TrezorHannes
# Date: 2025-01-11
#
# Requirements:
# - sudo privileges
# - Python 3 virtual environment (../.venv)
# - pocketmoney.py script
#
# Usage:
# 1. Make the script executable: chmod +x install_pocketmoney_service.sh
# 2. Run the script with sudo: sudo ./install_pocketmoney_service.sh
# --- End Documentation ---

# --- Script ---

# Check for sudo privileges
if [[ $EUID -ne 0 ]]; then
   echo "Error: This script must be run with sudo. Please run 'sudo $0'" 1>&2
   exit 1
fi

# Get the absolute path of the script's directory
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

# Define paths
VENV_PYTHON="$SCRIPT_DIR/../.venv/bin/python3"
POCKETMONEY_SCRIPT="$SCRIPT_DIR/pocketmoney.py"
SYSTEMD_DIR="/etc/systemd/system"
SERVICE_NAME="pocketmoney.service"
SERVICE_FILE="$SYSTEMD_DIR/$SERVICE_NAME"

# Check if the virtual environment Python executable exists
if [[ ! -f "$VENV_PYTHON" ]]; then
  echo "Error: Python virtual environment not found at $VENV_PYTHON. Please refer to the GH repo documentation to create it and try again." 1>&2
  exit 1
fi

# Check if the pocketmoney.py script exists
if [[ ! -f "$POCKETMONEY_SCRIPT" ]]; then
  echo "Error: pocketmoney.py script not found at $POCKETMONEY_SCRIPT. Please make sure it exists." 1>&2
  exit 1
fi

# Create the systemd service file
cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Pocket Money Service
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$VENV_PYTHON $POCKETMONEY_SCRIPT
Restart=always

[Install]
WantedBy=multi-user.target
EOF

# Reload systemd to recognize the new service
systemctl daemon-reload

# Enable and start the service
systemctl enable "$SERVICE_NAME"
systemctl start "$SERVICE_NAME"

echo "Successfully installed and started the pocketmoney service."

# --- End Script ---