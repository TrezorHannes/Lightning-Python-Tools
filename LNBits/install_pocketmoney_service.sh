#!/bin/bash

# --- Documentation ---
# Script Name: install_pocketmoney_service.sh
# Description: Installs a systemd timer and service for the pocketmoney.py script.
# Author: @TrezorHannes
# Date: 2025-01-11
#
# Requirements:
# - sudo privileges
# - Python 3 virtual environment (../.venv)
# - pocketmoney.py script
# - config.ini in the parent directory
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
TIMER_NAME="pocketmoney.timer"
SERVICE_FILE="$SYSTEMD_DIR/$SERVICE_NAME"
TIMER_FILE="$SYSTEMD_DIR/$TIMER_NAME"
CONFIG_FILE="$SCRIPT_DIR/../config.ini"

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

# Check if config.ini exists
if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "Error: config.ini not found at $CONFIG_FILE." 1>&2
  exit 1
fi

# Extract scheduling variables using awk (handles basic ini formatting)
RECURRENCE=$(awk -F '=' '/^recurrence/ {print $2}' "$CONFIG_FILE" | tr -d ' ' | head -n 1)
TIME=$(awk -F '=' '/^time/ {print $2}' "$CONFIG_FILE" | tr -d ' ' | head -n 1)
DAY_OF_WEEK=$(awk -F '=' '/^day_of_week/ {print $2}' "$CONFIG_FILE" | tr -d ' ' | head -n 1)

# Format OnCalendar string based on recurrence
if [ "$RECURRENCE" = "daily" ]; then
    ON_CALENDAR="*-*-* $TIME:00"
elif [ "$RECURRENCE" = "weekly" ]; then
    # Capitalize first letter of day for systemd
    DAY_CAP=$(echo "$DAY_OF_WEEK" | awk '{print toupper(substr($0,1,1))tolower(substr($0,2))}')
    ON_CALENDAR="$DAY_CAP *-*-* $TIME:00"
else
    echo "Error: Invalid recurrence '$RECURRENCE' in config.ini. Must be 'daily' or 'weekly'."
    exit 1
fi

echo "Configuring timer for: $ON_CALENDAR"

# Create the systemd service file (Type=oneshot for timer-triggered scripts)
cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Pocket Money Service
After=network.target

[Service]
Type=oneshot
User=$SUDO_USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$VENV_PYTHON $POCKETMONEY_SCRIPT --run-once
EOF

# Create the systemd timer file
cat > "$TIMER_FILE" << EOF
[Unit]
Description=Timer for Pocket Money Service

[Timer]
OnCalendar=$ON_CALENDAR
Persistent=true

[Install]
WantedBy=timers.target
EOF

# Stop and disable the old service if it was running as a daemon
systemctl stop "$SERVICE_NAME" 2>/dev/null
systemctl disable "$SERVICE_NAME" 2>/dev/null

# Reload systemd to recognize the new units
systemctl daemon-reload

# Enable and start the timer
systemctl enable "$TIMER_NAME"
systemctl start "$TIMER_NAME"

echo "Successfully installed and started the pocketmoney timer."

# --- End Script ---
