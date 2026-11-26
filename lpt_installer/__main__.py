"""Entrypoint for `python3 -m lpt_installer`."""

from __future__ import annotations

import sys

from lpt_installer.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
