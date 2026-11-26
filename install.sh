#!/usr/bin/env bash
set -euo pipefail

if [ "${EUID:-$(id -u)}" -eq 0 ]; then
  echo "Error: Do not run ./install.sh as root or via sudo." >&2
  echo "Run as your regular node operator user (e.g. admin)." >&2
  echo "The installer elevates via sudo only when writing system-wide systemd units." >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

PYTHON_BIN=""
for candidate in python3 python3.13 python3.12 python3.11; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info < (3, 14) else 1)' 2>/dev/null; then
      PYTHON_BIN="$candidate"
      break
    fi
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  echo "Error: Python 3.11, 3.12, or 3.13 is required to run Lightning-Python-Tools." >&2
  echo "On Debian/Ubuntu, install it with: sudo apt-get update && sudo apt-get install -y python3 python3-venv" >&2
  exit 1
fi

export LPT_REPO_ROOT="$SCRIPT_DIR"
export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"

exec "$PYTHON_BIN" -m lpt_installer "$@"
