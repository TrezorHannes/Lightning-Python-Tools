"""Interactive CLI menu and non-interactive subcommands for lpt_installer."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from pathlib import Path

from lpt_installer import (
    DISCLAIMER_TEXT,
    DISCLAIMER_VERSION,
    DONATION_FOOTER,
    VERSION,
)
from lpt_installer.adapters import SystemEnv
from lpt_installer.configs import (
    CONFIG_TEMPLATES,
    ensure_configs,
    harden_existing_configs,
    open_in_editor,
    validate_configs,
)
from lpt_installer.env import run_doctor, setup_venv
from lpt_installer.registry import (
    TOOLS,
    ToolSpec,
    filter_tools,
    get_tool,
    render_overview_table,
)
from lpt_installer.scheduler import collect_statuses, install_tool, uninstall_tools
from lpt_installer.state import load_state, save_state

InputFn = Callable[[str], str]
OutputFn = Callable[[str], None]


def resolve_repo_root(cli_root: str | None = None) -> Path:
    if cli_root:
        return Path(cli_root).resolve()
    env_root = os.environ.get("LPT_REPO_ROOT")
    if env_root:
        return Path(env_root).resolve()
    return Path(__file__).resolve().parent.parent


def ensure_disclaimer_accepted(
    repo_root: Path,
    *,
    accept_flag: bool,
    interactive: bool,
    input_fn: InputFn = input,
    output_fn: OutputFn = print,
) -> bool:
    """Require one-time disclaimer acknowledgment persisted in .lpt-state.json."""
    state = load_state(repo_root)
    if state.get("disclaimer_version") == DISCLAIMER_VERSION:
        return True

    output_fn("")
    output_fn(DISCLAIMER_TEXT)
    output_fn("")

    if accept_flag:
        state["disclaimer_version"] = DISCLAIMER_VERSION
        save_state(repo_root, state)
        return True

    if not interactive:
        output_fn(
            "Error: Non-interactive install requires --accept-disclaimer to acknowledge the warning above."
        )
        return False

    answer = input_fn("Type 'yes' or press Enter after reviewing to accept (q to abort): ").strip().lower()
    if answer in ("q", "quit", "no", "n"):
        output_fn("Aborted by user.")
        return False
    if answer not in ("", "y", "yes"):
        output_fn("Aborted: disclaimer not accepted.")
        return False

    state["disclaimer_version"] = DISCLAIMER_VERSION
    save_state(repo_root, state)
    return True


def build_parser() -> argparse.ArgumentParser:
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--repo-root", default=argparse.SUPPRESS, help="Override repository root path.")
    shared.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Emit JSON output.")

    parser = argparse.ArgumentParser(
        prog="install.sh",
        description="Unified installer & operations manager for Lightning-Python-Tools.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--repo-root", default=None, help="Override repository root path.")
    parser.add_argument(
        "--profile",
        choices=("all", "node", "vps"),
        default="all",
        help="Filter scripts by host profile (node, vps, or all).",
    )
    parser.add_argument("--json", action="store_true", default=False, help="Emit JSON output.")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", parents=[shared], help="Show overview table of all 19 scripts and statuses.")
    sub.add_parser("status", parents=[shared], help="Show installation status of all scripts.")

    env_p = sub.add_parser("env", parents=[shared], help="Create or update the repo .venv environment.")
    env_p.add_argument("--db", action="store_true", help="Install optional PostgreSQL/pandas extras.")
    env_p.add_argument("--recreate", action="store_true", help="Recreate .venv from scratch.")
    env_p.add_argument("--dry-run", action="store_true", help="Preview commands without running.")

    cfg_p = sub.add_parser(
        "config",
        parents=[shared],
        help="Copy missing *.example configs, harden 0600 perms, and validate.",
    )
    cfg_p.add_argument("--tools", nargs="*", help="Limit config check/copy to specific tool IDs.")
    cfg_p.add_argument("--edit", metavar="FILE", help="Open config file in $EDITOR after copying.")
    cfg_p.add_argument(
        "--harden",
        action="store_true",
        help="Only enforce 0600/0700 permissions on existing configs and logs/data without copying templates or validating.",
    )
    cfg_p.add_argument("--dry-run", action="store_true", help="Preview without modifying files.")

    inst_p = sub.add_parser("install", parents=[shared], help="Install and schedule one or more scripts.")
    inst_p.add_argument("tools", nargs="+", help="Tool IDs to install (see 'list').")
    inst_p.add_argument(
        "--scheduler",
        choices=("auto", "systemd", "cron", "manual"),
        default="auto",
        help="Scheduler backend (default: auto chooses each tool's default).",
    )
    inst_p.add_argument("--skip-env", action="store_true", help="Skip .venv creation/update.")
    inst_p.add_argument("--skip-config-check", action="store_true", help="Skip config placeholder check.")
    inst_p.add_argument("--accept-disclaimer", action="store_true", help="Acknowledge operator disclaimer.")
    inst_p.add_argument("--dry-run", action="store_true", help="Render and preview without applying.")

    uninst_p = sub.add_parser("uninstall", parents=[shared], help="Remove managed systemd units / cron entries.")
    uninst_p.add_argument("tools", nargs="*", help="Tool IDs to uninstall.")
    uninst_p.add_argument("--all", action="store_true", help="Uninstall all managed tools.")
    uninst_p.add_argument("--remove-venv", action="store_true", help="Also delete .venv directory.")
    uninst_p.add_argument(
        "--purge-configs",
        action="store_true",
        help="Delete user config files (requires --yes; preserved by default).",
    )
    uninst_p.add_argument("--yes", action="store_true", help="Confirm destructive flags.")
    uninst_p.add_argument("--dry-run", action="store_true", help="Preview without modifying system.")

    sub.add_parser("doctor", parents=[shared], help="Run environment, CA trust, permission, and conflict diagnostics.")
    return parser


def _resolve_mode(tool: ToolSpec, scheduler: str) -> str:
    if scheduler == "auto":
        return tool.default_mode
    if scheduler == "manual":
        if "manual" in tool.supported_modes:
            return "manual"
        raise ValueError(f"Tool {tool.id!r} cannot run in 'manual' mode.")
    if scheduler == "cron":
        if "cron" in tool.supported_modes:
            return "cron"
        raise ValueError(
            f"Tool {tool.id!r} does not support cron (supported: {', '.join(tool.supported_modes)})."
        )
    if scheduler == "systemd":
        for candidate in ("systemd-timer", "systemd-service"):
            if candidate in tool.supported_modes:
                return candidate
        raise ValueError(
            f"Tool {tool.id!r} does not support systemd (supported: {', '.join(tool.supported_modes)})."
        )
    raise ValueError(f"Unknown scheduler option: {scheduler!r}")


def run_interactive_menu(
    repo_root: Path,
    env: SystemEnv,
    *,
    profile: str = "all",
    input_fn: InputFn = input,
    output_fn: OutputFn = print,
) -> int:
    """Interactive numbered CLI menu for operators."""
    if not ensure_disclaimer_accepted(
        repo_root,
        accept_flag=False,
        interactive=True,
        input_fn=input_fn,
        output_fn=output_fn,
    ):
        return 1

    while True:
        tools = filter_tools(profile)
        statuses = collect_statuses(repo_root, env)
        output_fn("")
        output_fn(f"=== Lightning-Python-Tools Manager v{VERSION} (profile: {profile}) ===")
        output_fn(render_overview_table(tools, statuses))
        output_fn("")
        output_fn("Actions:")
        output_fn("  1) Setup / update Python .venv")
        output_fn("  2) Copy & validate config files (chmod 600)")
        output_fn("  3) Install / schedule tools (systemd or cron)")
        output_fn("  4) Uninstall managed tools (preserves configs)")
        output_fn("  5) Run diagnostics (doctor)")
        output_fn("  6) Switch host profile (all / node / vps)")
        output_fn("  q) Quit")

        choice = input_fn("Select action: ").strip().lower()
        if choice in ("q", "quit", "exit"):
            output_fn("")
            output_fn(DONATION_FOOTER)
            return 0
        if choice == "1":
            db_ans = input_fn("Include PostgreSQL/pandas extras for offline_summary? [y/N]: ").strip().lower()
            extras = ("db",) if db_ans in ("y", "yes") else ()
            res = setup_venv(repo_root, env, extras=extras)
            output_fn(f"Environment ready via {res['backend']} at {res['venv_dir']}")
        elif choice == "2":
            summary = ensure_configs(repo_root)
            output_fn(
                f"Configs copied: {summary['copied'] or 'none'} | preserved: {summary['preserved'] or 'none'}"
            )
            edit_ans = input_fn(
                "Edit a config file now? Enter path (e.g. config.ini) or press Enter to skip: "
            ).strip()
            if edit_ans:
                if edit_ans in CONFIG_TEMPLATES:
                    open_in_editor(repo_root / edit_ans, env)
                else:
                    output_fn(f"Unknown config file: {edit_ans}")
            errors = validate_configs(repo_root, tools)
            if errors:
                output_fn("Config warnings:")
                for err in errors:
                    output_fn(f"  - {err}")
            else:
                output_fn("Config validation passed.")
        elif choice == "3":
            raw_sel = input_fn(
                "Enter tool numbers or IDs (comma-separated, e.g. 1,4 or fee-adjuster): "
            ).strip()
            if not raw_sel:
                continue
            selected: list[ToolSpec] = []
            try:
                for token in [t.strip() for t in raw_sel.split(",") if t.strip()]:
                    if token.isdigit():
                        idx = int(token)
                        if not (1 <= idx <= len(tools)):
                            raise ValueError(f"Index out of range: {idx}")
                        selected.append(tools[idx - 1])
                    else:
                        selected.append(get_tool(token))
            except (KeyError, ValueError) as exc:
                output_fn(f"Selection error: {exc}")
                continue

            if any(t.risk_tier == "FUNDS" for t in selected):
                output_fn(
                    "[FUNDS WARNING] Selected funds-moving tool(s). Double-check config.ini and run dry-run previews!"
                )
            sched_choice = (
                input_fn("Scheduler [auto / systemd / cron / manual] (default: auto): ")
                .strip()
                .lower()
                or "auto"
            )
            cfg_res = ensure_configs(repo_root, selected)
            if cfg_res["copied"]:
                output_fn(f"Copied templates: {', '.join(cfg_res['copied'])} (mode 0600)")
            cfg_errors = validate_configs(repo_root, selected)
            if cfg_errors:
                output_fn("Please resolve configuration errors before enabling automation:")
                for err in cfg_errors:
                    output_fn(f"  - {err}")
                continue
            setup_venv(
                repo_root,
                env,
                extras=tuple(sorted({e for t in selected for e in t.optional_extras})),
            )
            for tool in selected:
                try:
                    mode = _resolve_mode(tool, sched_choice)
                    res = install_tool(tool, repo_root, env, mode=mode)
                    output_fn(f"Installed {res['tool']} ({res['mode']})")
                except Exception as exc:
                    output_fn(f"Failed installing {tool.id}: {exc}")
        elif choice == "4":
            raw_ids = input_fn(
                "Enter tool IDs to uninstall (comma-separated, or 'all'): "
            ).strip()
            if not raw_ids:
                continue
            target_ids = (
                [t.id for t in TOOLS]
                if raw_ids.lower() == "all"
                else [t.strip() for t in raw_ids.split(",") if t.strip()]
            )
            try:
                res = uninstall_tools(target_ids, repo_root, env)
                output_fn(
                    f"Removed units={res['removed_units']} cron={res['removed_cron']} (configs preserved)."
                )
            except Exception as exc:
                output_fn(f"Uninstall failed: {exc}")
        elif choice == "5":
            for check in run_doctor(repo_root, env):
                output_fn(f"[{check['status'].upper()}] {check['name']}: {check['detail']}")
        elif choice == "6":
            new_prof = input_fn("Profile [all / node / vps]: ").strip().lower()
            if new_prof in ("all", "node", "vps"):
                profile = new_prof
            else:
                output_fn(f"Invalid profile: {new_prof}")
        else:
            output_fn(f"Unknown option: {choice!r}")


def main(
    argv: list[str] | None = None,
    *,
    env: SystemEnv | None = None,
    input_fn: InputFn = input,
    output_fn: OutputFn = print,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    sys_env = env or SystemEnv()
    repo_root = resolve_repo_root(args.repo_root)

    if args.command is None:
        return run_interactive_menu(
            repo_root,
            sys_env,
            profile=args.profile,
            input_fn=input_fn,
            output_fn=output_fn,
        )

    if args.command in ("list", "status"):
        tools = filter_tools(args.profile)
        statuses = collect_statuses(repo_root, sys_env)
        if args.json:
            payload = [
                {
                    "id": t.id,
                    "script_path": t.script_path,
                    "risk_tier": t.risk_tier,
                    "default_mode": t.default_mode,
                    "supported_modes": list(t.supported_modes),
                    "host_profiles": list(t.host_profiles),
                    "status": statuses[t.id],
                }
                for t in tools
            ]
            output_fn(json.dumps(payload, indent=2))
        else:
            output_fn(render_overview_table(tools, statuses))
        return 0

    if args.command == "doctor":
        checks = run_doctor(repo_root, sys_env)
        if args.json:
            output_fn(json.dumps(checks, indent=2))
        else:
            for item in checks:
                output_fn(f"[{item['status'].upper()}] {item['name']}: {item['detail']}")
        has_fail = any(item["status"] == "fail" for item in checks)
        return 1 if has_fail else 0

    if args.command == "env":
        extras = ("db",) if args.db else ()
        res = setup_venv(
            repo_root,
            sys_env,
            extras=extras,
            dry_run=args.dry_run,
            recreate=args.recreate,
        )
        if args.json:
            output_fn(json.dumps(res, indent=2))
        else:
            output_fn(f"Environment ready via {res['backend']} ({res['python']})")
            output_fn(DONATION_FOOTER)
        return 0

    if args.command == "config":
        if args.harden:
            harden_summary = harden_existing_configs(repo_root, dry_run=args.dry_run)
            if args.json:
                output_fn(json.dumps(harden_summary, indent=2))
            else:
                output_fn(
                    f"Hardened configs: {harden_summary['hardened']} | dirs: {harden_summary['hardened_dirs']}"
                )
            return 0
        try:
            selected = [get_tool(tid) for tid in args.tools] if args.tools else list(TOOLS)
        except KeyError as exc:
            output_fn(f"Error: {exc}")
            return 1
        summary = ensure_configs(repo_root, selected, dry_run=args.dry_run)
        if args.edit and not args.dry_run:
            if args.edit not in CONFIG_TEMPLATES:
                output_fn(f"Unknown config file: {args.edit}")
                return 1
            open_in_editor(repo_root / args.edit, sys_env)
        errors = [] if args.dry_run else validate_configs(repo_root, selected)
        if args.json:
            output_fn(json.dumps({"summary": summary, "errors": errors}, indent=2))
        else:
            output_fn(
                f"Configs copied: {summary['copied']} | preserved: {summary['preserved']} | hardened: {summary['hardened']}"
            )
            for err in errors:
                output_fn(f"[ERROR] {err}")
        return 1 if errors else 0

    if args.command == "install":
        try:
            selected = [get_tool(tid) for tid in args.tools]
        except KeyError as exc:
            output_fn(f"Error: {exc}")
            return 1
        if not args.dry_run and not ensure_disclaimer_accepted(
            repo_root,
            accept_flag=args.accept_disclaimer,
            interactive=False,
            input_fn=input_fn,
            output_fn=output_fn,
        ):
            return 1

        if any(t.risk_tier == "FUNDS" for t in selected) and not args.json:
            output_fn(
                "[FUNDS WARNING] Installing funds-moving tool(s). Verify config.ini and run dry-run previews!"
            )

        if not args.dry_run:
            ensure_configs(repo_root, selected)
            if not args.skip_config_check:
                cfg_errors = validate_configs(repo_root, selected)
                if cfg_errors:
                    for err in cfg_errors:
                        output_fn(f"[CONFIG ERROR] {err}")
                    return 1
            if not args.skip_env:
                extras = tuple(sorted({e for t in selected for e in t.optional_extras}))
                setup_venv(repo_root, sys_env, extras=extras)

        results = []
        for tool in selected:
            mode = _resolve_mode(tool, args.scheduler)
            results.append(
                install_tool(tool, repo_root, sys_env, mode=mode, dry_run=args.dry_run)
            )

        if args.json:
            output_fn(json.dumps(results, indent=2))
        else:
            for item in results:
                output_fn(f"Installed {item['tool']} ({item['mode']})")
            output_fn(DONATION_FOOTER)
        return 0

    if args.command == "uninstall":
        if args.purge_configs and not args.yes:
            output_fn("Error: --purge-configs requires --yes to confirm deleting user configs.")
            return 1
        target_ids = [t.id for t in TOOLS] if args.all else list(args.tools)
        if not target_ids:
            output_fn("Error: specify tool IDs or --all.")
            return 1
        try:
            res = uninstall_tools(
                target_ids,
                repo_root,
                sys_env,
                remove_venv=args.remove_venv,
                purge_configs=args.purge_configs,
                dry_run=args.dry_run,
            )
        except KeyError as exc:
            output_fn(f"Error: {exc}")
            return 1
        if args.json:
            output_fn(json.dumps(res, indent=2))
        else:
            output_fn(
                f"Uninstalled units={res['removed_units']} cron={res['removed_cron']} "
                f"(configs preserved: {res['configs_preserved']})"
            )
            output_fn(DONATION_FOOTER)
        return 0

    return 1
