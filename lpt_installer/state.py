"""State manifest (.lpt-state.json) persistence for Lightning-Python-Tools."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

STATE_FILENAME = ".lpt-state.json"


def state_file_path(repo_root: Path) -> Path:
    return repo_root / STATE_FILENAME


def load_state(repo_root: Path) -> dict[str, Any]:
    """Load installer state manifest, returning a clean default dict if absent."""
    path = state_file_path(repo_root)
    if not path.is_file():
        return {"disclaimer_version": None, "installed": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"disclaimer_version": None, "installed": {}}
    if not isinstance(data, dict):
        return {"disclaimer_version": None, "installed": {}}
    installed = data.get("installed")
    if not isinstance(installed, dict):
        installed = {}
    return {
        "disclaimer_version": data.get("disclaimer_version"),
        "installed": installed,
    }


def save_state(repo_root: Path, state: dict[str, Any]) -> Path:
    """Atomically persist installer state manifest with 0o600 permissions."""
    path = state_file_path(repo_root)
    payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".lpt-state_", suffix=".tmp", dir=repo_root)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return path
