"""Systemd unit & cron scheduling, duplicate detection, migration, and uninstall."""

from __future__ import annotations

import configparser
import os
import re
import shlex
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from lpt_installer.adapters import SystemEnv
from lpt_installer.env import venv_python_path
from lpt_installer.registry import TOOLS, ToolSpec, get_tool
from lpt_installer.state import load_state, save_state

TOOL_ID_RE = re.compile(r"^[a-z0-9-]+$")
DEFAULT_CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"

CRON_BEGIN = "# >>> Lightning-Python-Tools managed block (do not edit manually)"
CRON_END = "# <<< Lightning-Python-Tools managed block"

WEEKDAY_MAP: dict[str, tuple[str, int]] = {
    "sunday": ("Sun", 0),
    "monday": ("Mon", 1),
    "tuesday": ("Tue", 2),
    "wednesday": ("Wed", 3),
    "thursday": ("Thu", 4),
    "friday": ("Fri", 5),
    "saturday": ("Sat", 6),
}


@dataclass(frozen=True)
class ConflictReport:
    """Existing unmanaged or legacy executions discovered for a tool."""

    legacy_units: tuple[str, ...]
    unmanaged_units: tuple[str, ...]
    unmanaged_cron: tuple[str, ...]
    running_processes: tuple[str, ...]

    @property
    def has_unmanaged(self) -> bool:
        return bool(
            self.unmanaged_units or self.unmanaged_cron or self.running_processes
        )


def validate_tool_id(tool_id: str) -> str:
    if not TOOL_ID_RE.fullmatch(tool_id):
        raise ValueError(f"Unsafe tool id: {tool_id!r}")
    return tool_id


def unit_prefix(tool_id: str) -> str:
    return f"lpt-{validate_tool_id(tool_id)}"


def build_runtime_path(home_dir: Path | None = None) -> str:
    home = home_dir or Path.home()
    candidates = [
        str(home / ".local" / "bin"),
        str(home / "go" / "bin"),
        "/usr/local/sbin",
        "/usr/local/bin",
        "/usr/sbin",
        "/usr/bin",
        "/sbin",
        "/bin",
    ]
    deduped: list[str] = []
    for entry in candidates:
        if entry not in deduped:
            deduped.append(entry)
    return ":".join(deduped)


def resolve_pocketmoney_schedule(repo_root: Path) -> tuple[str, str]:
    """Read [Schedule] from config.ini and return (systemd OnCalendar, cron expr)."""
    cfg = configparser.ConfigParser()
    cfg_path = repo_root / "config.ini"
    if cfg_path.is_file():
        cfg.read(cfg_path, encoding="utf-8")

    recurrence = (
        cfg.get("Schedule", "recurrence", fallback="weekly")
        .split("#")[0]
        .strip()
        .lower()
    )
    time_str = cfg.get("Schedule", "time", fallback="09:00").split("#")[0].strip()
    day_of_week = (
        cfg.get("Schedule", "day_of_week", fallback="friday")
        .split("#")[0]
        .strip()
        .lower()
    )

    parsed_time = datetime.strptime(time_str, "%H:%M")
    hh = parsed_time.strftime("%H")
    mm = parsed_time.strftime("%M")
    minute_int = int(mm)
    hour_int = int(hh)

    if recurrence == "daily":
        return f"*-*-* {hh}:{mm}:00", f"{minute_int} {hour_int} * * *"
    if recurrence == "weekly":
        if day_of_week not in WEEKDAY_MAP:
            raise ValueError(
                f"Invalid [Schedule] day_of_week={day_of_week!r} in config.ini"
            )
        systemd_day, cron_day = WEEKDAY_MAP[day_of_week]
        return (
            f"{systemd_day} *-*-* {hh}:{mm}:00",
            f"{minute_int} {hour_int} * * {cron_day}",
        )
    raise ValueError(f"Invalid [Schedule] recurrence={recurrence!r} in config.ini")


def resolve_schedule(tool: ToolSpec, repo_root: Path) -> tuple[str | None, str | None]:
    """Return (OnCalendar, cron_expr) for a tool."""
    if tool.id == "pocketmoney":
        return resolve_pocketmoney_schedule(repo_root)
    return tool.default_on_calendar, tool.default_cron


def render_systemd_service(
    tool: ToolSpec,
    repo_root: Path,
    *,
    user: str,
    group: str,
    ca_bundle: str = DEFAULT_CA_BUNDLE,
    mode: str | None = None,
    python_bin: Path | None = None,
    home_dir: Path | None = None,
    unit_exists: Any | None = None,
) -> str:
    """Render a hardened systemd .service unit for the given tool."""
    validate_tool_id(tool.id)
    chosen_mode = mode or tool.default_mode
    if chosen_mode not in ("systemd-service", "systemd-timer"):
        raise ValueError(
            f"Mode {chosen_mode!r} does not generate a systemd unit for {tool.id}."
        )
    if chosen_mode not in tool.supported_modes:
        raise ValueError(f"Tool {tool.id!r} does not support mode {chosen_mode!r}.")

    py_exe = python_bin or venv_python_path(repo_root)
    script_abs = repo_root / tool.script_path
    cli_args = tool.unattended_args if chosen_mode == "systemd-timer" else ()
    exec_parts = [str(py_exe), str(script_abs), *cli_args]
    exec_start = " ".join(shlex.quote(part) for part in exec_parts)

    after_list = ["network-online.target"]
    wants_list = ["network-online.target"]
    for unit in tool.after_units:
        if unit_exists is None or unit_exists(unit):
            after_list.append(unit)
    for unit in tool.wants_units:
        if unit_exists is None or unit_exists(unit):
            wants_list.append(unit)

    lines = [
        "[Unit]",
        f"Description=Lightning-Python-Tools: {tool.id} ({tool.risk_tier})",
        f"After={' '.join(after_list)}",
        f"Wants={' '.join(wants_list)}",
        "",
        "[Service]",
    ]

    if chosen_mode == "systemd-timer":
        lines.extend(
            [
                "Type=oneshot",
                "TimeoutStartSec=300",
            ]
        )
    else:
        lines.extend(
            [
                "Type=simple",
                "Restart=on-failure",
                "RestartSec=10",
            ]
        )

    runtime_path = build_runtime_path(home_dir)
    lines.extend(
        [
            f"User={user}",
            f"Group={group}",
            f"WorkingDirectory={repo_root}",
            f'Environment="SSL_CERT_FILE={ca_bundle}"',
            f'Environment="REQUESTS_CA_BUNDLE={ca_bundle}"',
            f'Environment="PATH={runtime_path}"',
            f"ExecStart={exec_start}",
            "StandardOutput=journal",
            "StandardError=journal",
            f"SyslogIdentifier={unit_prefix(tool.id)}",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectKernelTunables=true",
            "ProtectKernelModules=true",
            "ProtectControlGroups=true",
        ]
    )

    if tool.sandbox_level == "strict":
        logs_dir = repo_root / "logs"
        data_dir = repo_root / "data"
        lines.extend(
            [
                "ProtectSystem=strict",
                f"ReadWritePaths=-{logs_dir} -{data_dir}",
            ]
        )
    else:
        lines.append("ProtectSystem=full")

    if chosen_mode == "systemd-service":
        lines.extend(
            [
                "",
                "[Install]",
                "WantedBy=multi-user.target",
            ]
        )

    return "\n".join(lines) + "\n"


def render_systemd_timer(tool: ToolSpec, on_calendar: str) -> str:
    """Render a systemd .timer unit targeting lpt-<id>.service."""
    prefix = unit_prefix(tool.id)
    lines = [
        "[Unit]",
        f"Description=Schedule for {prefix}.service",
        "",
        "[Timer]",
        f"OnCalendar={on_calendar}",
        f"Unit={prefix}.service",
        "AccuracySec=1min",
    ]
    if tool.persistent_timer:
        lines.append("Persistent=true")
    lines.extend(
        [
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )
    return "\n".join(lines)


def _split_cron_blocks(existing_crontab: str) -> tuple[list[str], dict[str, str]]:
    """Split crontab into unmanaged lines and {tool_id: command_line} inside managed block."""
    unmanaged: list[str] = []
    managed: dict[str, str] = {}
    in_block = False
    pending_tool_id: str | None = None

    for raw_line in existing_crontab.splitlines():
        stripped = raw_line.strip()
        if stripped == CRON_BEGIN:
            in_block = True
            pending_tool_id = None
            continue
        if stripped == CRON_END:
            in_block = False
            pending_tool_id = None
            continue
        if not in_block:
            unmanaged.append(raw_line)
            continue
        if stripped.startswith("# lpt-tool:"):
            pending_tool_id = stripped.split(":", 1)[1].strip()
            continue
        if pending_tool_id and stripped and not stripped.startswith("#"):
            managed[pending_tool_id] = stripped
            pending_tool_id = None

    while unmanaged and unmanaged[-1] == "":
        unmanaged.pop()

    return unmanaged, managed


def render_cron_entry(
    tool: ToolSpec,
    cron_expr: str,
    repo_root: Path,
    *,
    python_bin: Path | None = None,
) -> str:
    validate_tool_id(tool.id)
    py_exe = python_bin or venv_python_path(repo_root)
    script_abs = repo_root / tool.script_path
    cmd_parts = [str(py_exe), str(script_abs), *tool.unattended_args]
    cmd_str = " ".join(shlex.quote(part) for part in cmd_parts)
    log_path = shlex.quote(str(repo_root / "logs" / f"cron_{tool.id}.log"))
    lock_path = shlex.quote(str(repo_root / "logs" / f".lpt-{tool.id}.lock"))
    repo_quoted = shlex.quote(str(repo_root))
    return (
        f"{cron_expr} cd {repo_quoted} && flock -n {lock_path} {cmd_str} >> {log_path} 2>&1"
    )


def _assemble_crontab(
    unmanaged: list[str],
    managed: dict[str, str],
    *,
    ca_bundle: str,
    home_dir: Path | None = None,
) -> str:
    lines = list(unmanaged)
    if managed:
        if lines:
            lines.append("")
        lines.extend(
            [
                CRON_BEGIN,
                f"SSL_CERT_FILE={ca_bundle}",
                f"REQUESTS_CA_BUNDLE={ca_bundle}",
                f"PATH={build_runtime_path(home_dir)}",
            ]
        )
        for tool_id in sorted(managed):
            lines.append(f"# lpt-tool: {tool_id}")
            lines.append(managed[tool_id])
        lines.append(CRON_END)

    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def upsert_cron_block(
    existing_crontab: str,
    updates: dict[str, str],
    *,
    ca_bundle: str = DEFAULT_CA_BUNDLE,
    home_dir: Path | None = None,
) -> str:
    """Idempotently upsert managed cron entries while preserving user lines verbatim."""
    unmanaged, managed = _split_cron_blocks(existing_crontab)
    for tool_id, entry in updates.items():
        validate_tool_id(tool_id)
        managed[tool_id] = entry
    return _assemble_crontab(
        unmanaged, managed, ca_bundle=ca_bundle, home_dir=home_dir
    )


def remove_cron_tools(
    existing_crontab: str,
    tool_ids: list[str],
    *,
    ca_bundle: str = DEFAULT_CA_BUNDLE,
    home_dir: Path | None = None,
) -> str:
    """Remove specified tool IDs from the managed cron block, preserving user lines."""
    unmanaged, managed = _split_cron_blocks(existing_crontab)
    for tool_id in tool_ids:
        managed.pop(tool_id, None)
    return _assemble_crontab(
        unmanaged, managed, ca_bundle=ca_bundle, home_dir=home_dir
    )


def detect_conflicts(tool: ToolSpec, env: SystemEnv) -> ConflictReport:
    """Scan systemd units, crontab, and running processes for duplicate executions."""
    script_name = Path(tool.script_path).name
    managed_service = f"{unit_prefix(tool.id)}.service"

    legacy_found: list[str] = []
    unmanaged_units: list[str] = []

    for unit_path in env.list_systemd_units("*.service"):
        if unit_path.name == managed_service:
            continue
        try:
            content = unit_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if script_name in content or unit_path.name in tool.legacy_units:
            if unit_path.name in tool.legacy_units:
                legacy_found.append(unit_path.name)
                timer_name = f"{unit_path.stem}.timer"
                if env.systemd_unit_exists(timer_name):
                    legacy_found.append(timer_name)
            else:
                unmanaged_units.append(str(unit_path))

    for unit_name in tool.legacy_units:
        if unit_name.endswith(".timer") and env.systemd_unit_exists(unit_name):
            legacy_found.append(unit_name)

    for user_unit in env.list_user_systemd_units("*.service"):
        try:
            content = user_unit.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if script_name in content:
            unmanaged_units.append(str(user_unit))

    unmanaged_cron_lines: list[str] = []
    unmanaged_lines, _ = _split_cron_blocks(env.read_crontab())
    for line in unmanaged_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if script_name in stripped:
            unmanaged_cron_lines.append(stripped)

    running: list[str] = []
    if not legacy_found:
        managed_pid: str | None = None
        if env.systemd_unit_exists(managed_service):
            pid_res = env.systemctl(
                "show",
                "-p",
                "MainPID",
                "--value",
                managed_service,
                use_sudo=False,
                check=False,
            )
            candidate_pid = pid_res.stdout.strip()
            if pid_res.returncode == 0 and candidate_pid.isdigit() and int(candidate_pid) > 0:
                managed_pid = candidate_pid

        for proc_line in env.running_processes():
            if script_name in proc_line and "lpt_installer" not in proc_line:
                proc_pid = proc_line.split(None, 1)[0]
                if managed_pid and proc_pid == managed_pid:
                    continue
                running.append(proc_line)

    return ConflictReport(
        legacy_units=tuple(sorted(set(legacy_found))),
        unmanaged_units=tuple(sorted(set(unmanaged_units))),
        unmanaged_cron=tuple(unmanaged_cron_lines),
        running_processes=tuple(running),
    )


def create_backup_snapshot(
    repo_root: Path,
    env: SystemEnv,
    legacy_units: tuple[str, ...] = (),
) -> Path:
    """Create a 0700 backup directory with current crontab and legacy unit files."""
    backups_root = repo_root / ".lpt-backups"
    backups_root.mkdir(parents=True, exist_ok=True)
    os.chmod(backups_root, 0o700)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    snapshot_dir = backups_root / stamp
    suffix = 1
    while snapshot_dir.exists():
        suffix += 1
        snapshot_dir = backups_root / f"{stamp}-{suffix}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(snapshot_dir, 0o700)

    cron_text = env.read_crontab()
    if cron_text:
        cron_bak = snapshot_dir / "crontab.bak"
        cron_bak.write_text(cron_text, encoding="utf-8")
        os.chmod(cron_bak, 0o600)

    for unit_name in legacy_units:
        src = env.systemd_dir / unit_name
        if src.is_file():
            try:
                content = src.read_text(encoding="utf-8", errors="ignore")
                dst = snapshot_dir / unit_name
                dst.write_text(content, encoding="utf-8")
                os.chmod(dst, 0o600)
            except OSError:
                continue

    return snapshot_dir


def _migrate_legacy_units(
    legacy_units: tuple[str, ...], env: SystemEnv, *, dry_run: bool
) -> list[str]:
    migrated: list[str] = []
    for unit_name in legacy_units:
        if not dry_run:
            env.systemctl("disable", "--now", unit_name, use_sudo=True, check=False)
            env.sudo_remove_file(env.systemd_dir / unit_name)
        migrated.append(unit_name)
    if legacy_units and not dry_run:
        env.systemctl("daemon-reload", use_sudo=True, check=True)
    return migrated


def install_tool(
    tool: ToolSpec,
    repo_root: Path,
    env: SystemEnv,
    *,
    mode: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Install/schedule a tool cleanly with duplicate checks, backup, and migration."""
    if env.current_uid() == 0:
        raise PermissionError("Refusing to run installation as root.")

    chosen_mode = mode or tool.default_mode
    if chosen_mode not in tool.supported_modes:
        raise ValueError(
            f"Tool {tool.id!r} does not support mode {chosen_mode!r} "
            f"(supported: {', '.join(tool.supported_modes)})."
        )

    if chosen_mode == "manual":
        state = load_state(repo_root)
        state["installed"][tool.id] = {
            "mode": "manual",
            "script": tool.script_path,
        }
        if not dry_run:
            save_state(repo_root, state)
        return {
            "tool": tool.id,
            "mode": "manual",
            "command": f"{venv_python_path(repo_root)} {repo_root / tool.script_path}",
            "dry_run": dry_run,
        }

    conflicts = detect_conflicts(tool, env)
    if conflicts.has_unmanaged:
        details = [
            *conflicts.unmanaged_units,
            *conflicts.unmanaged_cron,
            *conflicts.running_processes,
        ]
        raise RuntimeError(
            f"Refusing to install {tool.id}: unmanaged duplicate execution detected: "
            + "; ".join(details)
        )

    ca_bundle = env.detect_ca_bundle() or DEFAULT_CA_BUNDLE
    user, group = env.current_user_group()
    on_calendar, cron_expr = resolve_schedule(tool, repo_root)

    backup_dir: Path | None = None
    migrated: list[str] = []
    if not dry_run:
        backup_dir = create_backup_snapshot(repo_root, env, conflicts.legacy_units)
        if conflicts.legacy_units:
            migrated = _migrate_legacy_units(conflicts.legacy_units, env, dry_run=False)

    prefix = unit_prefix(tool.id)
    state = load_state(repo_root)

    if chosen_mode in ("systemd-service", "systemd-timer"):
        service_content = render_systemd_service(
            tool,
            repo_root,
            user=user,
            group=group,
            ca_bundle=ca_bundle,
            mode=chosen_mode,
            home_dir=env.home_dir(),
            unit_exists=env.systemd_unit_exists,
        )
        service_path = env.systemd_dir / f"{prefix}.service"
        timer_path = env.systemd_dir / f"{prefix}.timer"
        timer_content: str | None = None

        if chosen_mode == "systemd-timer":
            if not on_calendar:
                raise ValueError(f"No OnCalendar schedule defined for {tool.id}.")
            timer_content = render_systemd_timer(tool, on_calendar)

        if not dry_run:
            # Ensure no leftover managed cron entry runs alongside systemd
            existing_cron = env.read_crontab()
            if tool.id in _split_cron_blocks(existing_cron)[1]:
                updated_cron = remove_cron_tools(
                    existing_cron,
                    [tool.id],
                    ca_bundle=ca_bundle,
                    home_dir=env.home_dir(),
                )
                env.write_crontab(updated_cron)

            env.sudo_install_file(service_content, service_path)
            if timer_content is not None:
                env.sudo_install_file(timer_content, timer_path)
                env.systemctl("daemon-reload", use_sudo=True, check=True)
                env.systemctl("enable", "--now", f"{prefix}.timer", use_sudo=True, check=True)
            else:
                if timer_path.exists():
                    env.systemctl("disable", "--now", f"{prefix}.timer", use_sudo=True, check=False)
                    env.sudo_remove_file(timer_path)
                env.systemctl("daemon-reload", use_sudo=True, check=True)
                env.systemctl("enable", "--now", f"{prefix}.service", use_sudo=True, check=True)
                env.systemctl("try-restart", f"{prefix}.service", use_sudo=True, check=True)

            state["installed"][tool.id] = {
                "mode": chosen_mode,
                "unit": f"{prefix}.timer" if chosen_mode == "systemd-timer" else f"{prefix}.service",
                "backup_dir": str(backup_dir) if backup_dir else None,
            }
            save_state(repo_root, state)

        return {
            "tool": tool.id,
            "mode": chosen_mode,
            "service_path": str(service_path),
            "timer_path": str(timer_path) if timer_content else None,
            "service_content": service_content,
            "timer_content": timer_content,
            "migrated_legacy": migrated or list(conflicts.legacy_units),
            "backup_dir": str(backup_dir) if backup_dir else None,
            "dry_run": dry_run,
        }

    # chosen_mode == "cron"
    if not cron_expr:
        raise ValueError(f"No cron schedule defined for {tool.id}.")
    cron_entry = render_cron_entry(tool, cron_expr, repo_root)
    new_crontab = upsert_cron_block(
        env.read_crontab(),
        {tool.id: cron_entry},
        ca_bundle=ca_bundle,
        home_dir=env.home_dir(),
    )
    if not dry_run:
        # Stop/remove any managed systemd unit for this tool before switching to cron
        for suffix in (".timer", ".service"):
            unit_name = f"{prefix}{suffix}"
            if env.systemd_unit_exists(unit_name):
                env.systemctl("disable", "--now", unit_name, use_sudo=True, check=False)
                env.sudo_remove_file(env.systemd_dir / unit_name)
                env.systemctl("daemon-reload", use_sudo=True, check=False)
        (repo_root / "logs").mkdir(parents=True, exist_ok=True)
        env.write_crontab(new_crontab)
        state["installed"][tool.id] = {
            "mode": "cron",
            "schedule": cron_expr,
            "backup_dir": str(backup_dir) if backup_dir else None,
        }
        save_state(repo_root, state)

    return {
        "tool": tool.id,
        "mode": "cron",
        "cron_entry": cron_entry,
        "migrated_legacy": migrated or list(conflicts.legacy_units),
        "backup_dir": str(backup_dir) if backup_dir else None,
        "dry_run": dry_run,
    }


def uninstall_tools(
    tool_ids: list[str],
    repo_root: Path,
    env: SystemEnv,
    *,
    remove_venv: bool = False,
    purge_configs: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Stop/remove managed systemd units and cron entries while preserving configs by default."""
    if env.current_uid() == 0:
        raise PermissionError("Refusing to uninstall as root.")

    for tid in tool_ids:
        get_tool(tid)
        validate_tool_id(tid)

    ca_bundle = env.detect_ca_bundle() or DEFAULT_CA_BUNDLE
    removed_units: list[str] = []
    removed_cron: list[str] = []
    removed_configs: list[str] = []

    if not dry_run and tool_ids:
        create_backup_snapshot(repo_root, env)

    reload_needed = False
    for tid in tool_ids:
        prefix = unit_prefix(tid)
        for suffix in (".timer", ".service"):
            unit_name = f"{prefix}{suffix}"
            unit_path = env.systemd_dir / unit_name
            if env.systemd_unit_exists(unit_name):
                removed_units.append(unit_name)
                if not dry_run:
                    env.systemctl("disable", "--now", unit_name, use_sudo=True, check=False)
                    env.sudo_remove_file(unit_path)
                    reload_needed = True

    if reload_needed and not dry_run:
        env.systemctl("daemon-reload", use_sudo=True, check=True)

    current_cron = env.read_crontab()
    _, current_managed = _split_cron_blocks(current_cron)
    for tid in tool_ids:
        if tid in current_managed:
            removed_cron.append(tid)
    updated_cron = remove_cron_tools(
        current_cron,
        tool_ids,
        ca_bundle=ca_bundle,
        home_dir=env.home_dir(),
    )
    if removed_cron and not dry_run:
        env.write_crontab(updated_cron)

    state = load_state(repo_root)
    for tid in tool_ids:
        state["installed"].pop(tid, None)
    if not dry_run:
        save_state(repo_root, state)

    venv_removed = False
    venv_dir = repo_root / ".venv"
    if remove_venv and venv_dir.exists():
        venv_removed = True
        if not dry_run:
            import shutil

            shutil.rmtree(venv_dir)

    if purge_configs:
        for rel_cfg in ("config.ini", "Magma/magma_config.ini", "feeConfig.json", "wallets.json"):
            cfg_path = repo_root / rel_cfg
            if cfg_path.exists():
                removed_configs.append(rel_cfg)
                if not dry_run:
                    cfg_path.unlink()

    return {
        "removed_units": removed_units,
        "removed_cron": removed_cron,
        "venv_removed": venv_removed,
        "removed_configs": removed_configs,
        "configs_preserved": not purge_configs,
        "dry_run": dry_run,
    }


def collect_statuses(repo_root: Path, env: SystemEnv) -> dict[str, str]:
    """Return runtime status label for each tool in the registry."""
    state = load_state(repo_root)
    _, managed_cron = _split_cron_blocks(env.read_crontab())
    statuses: dict[str, str] = {}

    for tool in TOOLS:
        prefix = unit_prefix(tool.id)
        if env.systemd_unit_exists(f"{prefix}.timer"):
            statuses[tool.id] = "systemd-timer"
        elif env.systemd_unit_exists(f"{prefix}.service"):
            statuses[tool.id] = "systemd-service"
        elif tool.id in managed_cron:
            statuses[tool.id] = "cron"
        elif any(env.systemd_unit_exists(u) for u in tool.legacy_units):
            statuses[tool.id] = "legacy-systemd"
        elif tool.id in state.get("installed", {}):
            statuses[tool.id] = str(state["installed"][tool.id].get("mode", "installed"))
        else:
            statuses[tool.id] = "unmanaged"
    return statuses
