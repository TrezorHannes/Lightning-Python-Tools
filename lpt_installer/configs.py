"""Configuration template copying, permission hardening, and validation."""

from __future__ import annotations

import configparser
import json
import os
import shlex
import shutil
from datetime import datetime
from pathlib import Path

from lpt_installer.adapters import SystemEnv
from lpt_installer.registry import ToolSpec

CONFIG_TEMPLATES: dict[str, str] = {
    "config.ini": "config.ini.example",
    "Magma/magma_config.ini": "Magma/magma_config.ini.example",
    "feeConfig.json": "feeConfig.json.example",
    "wallets.json": "wallets.json.example",
}

PLACEHOLDER_VALUES: dict[tuple[str, str], set[str]] = {
    ("LNBits", "base_url"): {"https://yourdomain.com", "yourdomain.com"},
    ("LNBits", "admin_key"): {"admin_key_1"},
    ("lnd", "rpcserver"): {"your_lnd_host:port"},
    ("lnd", "tlscertpath"): {"/path/to/your/lnd/tls.cert"},
    ("lnd", "macaroonpath"): {"/path/to/your/lnd/admin.macaroon"},
}

VALID_DAYS_OF_WEEK = {
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
}


def required_configs_for_tools(tools: list[ToolSpec]) -> list[str]:
    """Return deduplicated config paths required by the selected tools."""
    ordered: list[str] = []
    for tool in tools:
        for cfg in tool.config_templates:
            if cfg not in ordered:
                ordered.append(cfg)
    return ordered


def ensure_configs(
    repo_root: Path,
    tools: list[ToolSpec] | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, list[str]]:
    """Copy missing configs from *.example (never clobber) and enforce 0600/0700 perms."""
    targets = (
        required_configs_for_tools(tools)
        if tools is not None
        else list(CONFIG_TEMPLATES.keys())
    )
    copied: list[str] = []
    preserved: list[str] = []
    hardened: list[str] = []

    for rel_path in targets:
        example_rel = CONFIG_TEMPLATES[rel_path]
        src = repo_root / example_rel
        dst = repo_root / rel_path
        if not src.is_file():
            raise FileNotFoundError(f"Missing template file: {src}")

        if dst.exists():
            preserved.append(rel_path)
            if not dry_run:
                os.chmod(dst, 0o600)
                hardened.append(rel_path)
        else:
            copied.append(rel_path)
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                os.chmod(dst, 0o600)
                hardened.append(rel_path)

    for dir_name in ("logs", "data"):
        dir_path = repo_root / dir_name
        if not dry_run:
            dir_path.mkdir(parents=True, exist_ok=True)
            os.chmod(dir_path, 0o700)

    return {"copied": copied, "preserved": preserved, "hardened": hardened}


def harden_existing_configs(
    repo_root: Path,
    *,
    dry_run: bool = False,
) -> dict[str, list[str]]:
    """Enforce 0600/0700 permissions on existing config files and logs/data without copying templates."""
    hardened: list[str] = []
    hardened_dirs: list[str] = []

    for rel_path in CONFIG_TEMPLATES:
        dst = repo_root / rel_path
        if dst.is_file():
            hardened.append(rel_path)
            if not dry_run:
                os.chmod(dst, 0o600)

    for dir_name in ("logs", "data"):
        dir_path = repo_root / dir_name
        if dir_path.is_dir():
            hardened_dirs.append(dir_name)
            if not dry_run:
                os.chmod(dir_path, 0o700)

    return {"hardened": hardened, "hardened_dirs": hardened_dirs}


def validate_configs(repo_root: Path, tools: list[ToolSpec]) -> list[str]:
    """Validate required config files, INI keys, schedules, and JSON syntax."""
    errors: list[str] = []
    needed = required_configs_for_tools(tools)

    for rel_path in needed:
        if not (repo_root / rel_path).is_file():
            errors.append(
                f"Missing {rel_path} (copy from {CONFIG_TEMPLATES[rel_path]} first)."
            )

    cfg = configparser.ConfigParser()
    config_ini = repo_root / "config.ini"
    if "config.ini" in needed and config_ini.is_file():
        try:
            cfg.read(config_ini, encoding="utf-8")
        except configparser.Error as exc:
            errors.append(f"Invalid INI syntax in config.ini: {exc}")
            cfg = None

        if cfg is not None:
            checked: set[tuple[str, str]] = set()
            for tool in tools:
                for section, key in tool.required_ini_keys:
                    if (section, key) in checked:
                        continue
                    checked.add((section, key))
                    if not cfg.has_section(section):
                        errors.append(f"config.ini missing section [{section}] (for {tool.id}).")
                        continue
                    raw_val = cfg.get(section, key, fallback="").strip()
                    if not raw_val:
                        errors.append(
                            f"config.ini [{section}] {key} is empty (required by {tool.id})."
                        )
                        continue
                    placeholders = PLACEHOLDER_VALUES.get((section, key), set())
                    if raw_val in placeholders:
                        errors.append(
                            f"config.ini [{section}] {key} still uses placeholder {raw_val!r}."
                        )

            if any(tool.id == "pocketmoney" for tool in tools):
                errors.extend(_validate_pocketmoney_schedule(cfg))

    if "Magma/magma_config.ini" in needed:
        magma_ini = repo_root / "Magma" / "magma_config.ini"
        if magma_ini.is_file():
            magma_cfg = configparser.ConfigParser()
            try:
                magma_cfg.read(magma_ini, encoding="utf-8")
            except configparser.Error as exc:
                errors.append(f"Invalid INI syntax in Magma/magma_config.ini: {exc}")

    if "feeConfig.json" in needed:
        fee_json = repo_root / "feeConfig.json"
        if fee_json.is_file():
            try:
                data = json.loads(fee_json.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or "nodes" not in data:
                    errors.append("feeConfig.json must be a JSON object containing 'nodes'.")
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"Invalid JSON in feeConfig.json: {exc}")

    if "wallets.json" in needed:
        wallets_json = repo_root / "wallets.json"
        if wallets_json.is_file():
            try:
                data = json.loads(wallets_json.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not data:
                    errors.append("wallets.json must be a non-empty JSON object of child wallets.")
                else:
                    for name, info in data.items():
                        if not isinstance(info, dict):
                            errors.append(f"wallets.json entry {name!r} must be an object.")
                            continue
                        wallet_id = info.get("wallet_id")
                        if not isinstance(wallet_id, str) or not wallet_id.strip():
                            errors.append(
                                f"wallets.json entry {name!r} is missing a valid wallet_id."
                            )
                        elif wallet_id.strip().startswith("WALLETID"):
                            errors.append(
                                f"wallets.json entry {name!r} still uses placeholder wallet_id."
                            )
                        invoice_key = info.get("invoice_key")
                        if not isinstance(invoice_key, str) or not invoice_key.strip():
                            errors.append(
                                f"wallets.json entry {name!r} is missing a valid invoice_key."
                            )
                        elif invoice_key.strip().startswith(("INVOICEID", "INVOICEKEY")):
                            errors.append(
                                f"wallets.json entry {name!r} still uses placeholder invoice_key."
                            )
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"Invalid JSON in wallets.json: {exc}")

    return errors


def _validate_pocketmoney_schedule(cfg: configparser.ConfigParser) -> list[str]:
    errors: list[str] = []
    recurrence = cfg.get("Schedule", "recurrence", fallback="weekly").split("#")[0].strip().lower()
    if recurrence not in ("daily", "weekly"):
        errors.append(
            f"config.ini [Schedule] recurrence={recurrence!r} must be 'daily' or 'weekly'."
        )
    time_str = cfg.get("Schedule", "time", fallback="09:00").split("#")[0].strip()
    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        errors.append(f"config.ini [Schedule] time={time_str!r} must use HH:MM format.")
    if recurrence == "weekly":
        day = cfg.get("Schedule", "day_of_week", fallback="friday").split("#")[0].strip().lower()
        if day not in VALID_DAYS_OF_WEEK:
            errors.append(
                f"config.ini [Schedule] day_of_week={day!r} must be a valid weekday name."
            )
    return errors


def open_in_editor(config_path: Path, env: SystemEnv) -> bool:
    """Launch user's preferred editor ($VISUAL/$EDITOR/nano/vi) on a config file."""
    for candidate in (
        env.env_var("VISUAL"),
        env.env_var("EDITOR"),
        "nano",
        "vi",
    ):
        if not candidate:
            continue
        parts = shlex.split(candidate)
        if parts and env.which(parts[0]):
            res = env.run([*parts, str(config_path)], check=False, interactive=True)
            return res.returncode == 0
    return False
