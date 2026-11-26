"""Tests for unified installer/manager package (lpt_installer)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from lpt_installer import DISCLAIMER_VERSION, DONATION_URL
from lpt_installer.adapters import CommandResult, SystemEnv
from lpt_installer.cli import main as cli_main
from lpt_installer.configs import ensure_configs, validate_configs
from lpt_installer.env import run_doctor, setup_venv
from lpt_installer.registry import (
    TOOLS,
    TOOLS_BY_ID,
    filter_tools,
    get_tool,
    render_markdown_overview_table,
    render_overview_table,
    schedulable_tools,
)
from lpt_installer.scheduler import (
    CRON_BEGIN,
    CRON_END,
    detect_conflicts,
    install_tool,
    remove_cron_tools,
    render_cron_entry,
    render_systemd_service,
    render_systemd_timer,
    resolve_pocketmoney_schedule,
    uninstall_tools,
    upsert_cron_block,
)
from lpt_installer.state import load_state, save_state

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeEnv(SystemEnv):
    """In-memory / tmp_path SystemEnv double so production installer code runs without root."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        uid: int = 1000,
        uv_available: bool = True,
        ca_bundle: str | None = "/etc/ssl/certs/ca-certificates.crt",
        crontab: str = "",
        processes: list[str] | None = None,
    ) -> None:
        super().__init__(
            systemd_dir=tmp_path / "etc_systemd",
            user_systemd_dir=tmp_path / "user_systemd",
        )
        self.systemd_dir.mkdir(parents=True, exist_ok=True)
        self.user_systemd_dir.mkdir(parents=True, exist_ok=True)
        self._uid = uid
        self._uv_available = uv_available
        self._ca_bundle = ca_bundle
        self.crontab = crontab
        self._processes = processes or []
        self.commands: list[list[str]] = []
        self.installed_files: dict[Path, str] = {}

    def run(
        self,
        argv: list[str],
        *,
        input_text: str | None = None,
        check: bool = False,
        env_vars: dict[str, str] | None = None,
        interactive: bool = False,
    ) -> CommandResult:
        self.commands.append(list(argv))
        if len(argv) >= 3 and argv[1] == "-c" and "certifi.__version__" in argv[2]:
            return CommandResult(0, "2026.11.20\n", "")
        return CommandResult(0, "", "")

    def which(self, binary: str) -> str | None:
        if binary == "uv":
            return "/usr/local/bin/uv" if self._uv_available else None
        if binary in ("nano", "vi"):
            return f"/usr/bin/{binary}"
        return None

    def current_uid(self) -> int:
        return self._uid

    def current_user_group(self) -> tuple[str, str]:
        return ("admin", "admin")

    def home_dir(self) -> Path:
        return self.systemd_dir.parent / "home"

    def detect_ca_bundle(self) -> str | None:
        return self._ca_bundle

    def sudo_install_file(
        self, content: str, dest: Path, mode: str = "0644"
    ) -> CommandResult:
        self.commands.append(["sudo", "install", "-m", mode, str(dest)])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        self.installed_files[dest] = content
        return CommandResult(0, "", "")

    def sudo_remove_file(self, dest: Path) -> CommandResult:
        self.commands.append(["sudo", "rm", "-f", str(dest)])
        dest.unlink(missing_ok=True)
        self.installed_files.pop(dest, None)
        return CommandResult(0, "", "")

    def systemctl(
        self, *args: str, use_sudo: bool = True, check: bool = True
    ) -> CommandResult:
        cmd = ["sudo", "systemctl", *args] if use_sudo else ["systemctl", *args]
        self.commands.append(cmd)
        return CommandResult(0, "", "")

    def read_crontab(self) -> str:
        return self.crontab

    def write_crontab(self, content: str) -> CommandResult:
        self.crontab = content
        return CommandResult(0, "", "")

    def running_processes(self) -> list[str]:
        return list(self._processes)


def _seed_repo(tmp_path: Path, *, valid_configs: bool = True) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    for rel in (
        "config.ini.example",
        "feeConfig.json.example",
        "wallets.json.example",
        "Magma/magma_config.ini.example",
        "requirements.txt",
    ):
        src = REPO_ROOT / rel
        dst = repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    venv_py = repo / ".venv" / "bin" / "python3"
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("#!/bin/sh\n", encoding="utf-8")

    if valid_configs:
        (repo / "config.ini").write_text(
            "\n".join(
                [
                    "[credentials]",
                    "amboss_authorization = real-amboss-token",
                    "lndg_username = lndgadmin",
                    "lndg_password = secretpass",
                    "[lndg]",
                    "lndg_api_url = http://localhost:8889",
                    "[paths]",
                    "lncli_path = /usr/local/bin/lncli",
                    "pscli_path = /usr/local/bin/pscli",
                    "boltzcli_path = /usr/local/bin/boltzcli",
                    "[telegram]",
                    "magma_bot_token = 123:abc",
                    "peerswap_bot_token = 456:def",
                    "lnbits_bot_token = 789:ghi",
                    "telegram_user_id = 424242",
                    "[LNBits]",
                    "base_url = https://lnbits.node.local",
                    "admin_key = real-admin-key",
                    "[Schedule]",
                    "recurrence = weekly # inline comment",
                    "time = 09:30",
                    "day_of_week = friday",
                    "[database]",
                    "name = lndg",
                    "[system]",
                    "full_path_bos = /usr/bin/bos",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (repo / "feeConfig.json").write_text('{"nodes": []}\n', encoding="utf-8")
        (repo / "wallets.json").write_text(
            json.dumps(
                {
                    "Alice": {
                        "wallet_id": "real_wallet_1",
                        "invoice_key": "real_inv_1",
                        "fiat_amount": 5,
                        "fiat_currency": "EUR",
                    }
                }
            ),
            encoding="utf-8",
        )
        (repo / "Magma" / "magma_config.ini").write_text("[magma_autoprice]\n", encoding="utf-8")
    return repo


def test_registry_covers_all_19_scripts_and_files_exist():
    assert len(TOOLS) == 19
    assert len(TOOLS_BY_ID) == 19

    disk_scripts = {
        str(path.relative_to(REPO_ROOT))
        for folder in ("LNBits", "LNDg", "Magma", "Other", "Peerswap")
        for path in (REPO_ROOT / folder).glob("*.py")
    }
    registry_scripts = {tool.script_path for tool in TOOLS}
    assert registry_scripts == disk_scripts

    for tool in TOOLS:
        assert (REPO_ROOT / tool.script_path).is_file()
        assert tool.default_mode in tool.supported_modes

    vps_ids = {t.id for t in filter_tools("vps")}
    assert "pocketmoney" in vps_ids
    assert "fee-adjuster" in vps_ids
    assert "magma-sale" not in vps_ids
    assert any(t.id == "fee-adjuster" for t in schedulable_tools("node"))
    assert "pocketmoney" in render_overview_table(list(TOOLS))
    assert "| `pocketmoney` |" in render_markdown_overview_table()


def test_render_systemd_units_hardening_and_sandbox(tmp_path):
    repo = _seed_repo(tmp_path)
    pm = get_tool("pocketmoney")
    pm_service = render_systemd_service(
        pm,
        repo,
        user="admin",
        group="admin",
        mode="systemd-timer",
        unit_exists=lambda _: False,
    )
    assert "Type=oneshot" in pm_service
    assert "TimeoutStartSec=300" in pm_service
    assert "ProtectSystem=strict" in pm_service
    assert "--run-once" in pm_service
    assert "[Install]" not in pm_service

    pm_daemon = render_systemd_service(
        pm,
        repo,
        user="admin",
        group="admin",
        mode="systemd-service",
        unit_exists=lambda _: False,
    )
    assert "Type=simple" in pm_daemon
    assert "--run-once" not in pm_daemon

    pm_timer = render_systemd_timer(pm, "Fri *-*-* 09:30:00")
    assert "OnCalendar=Fri *-*-* 09:30:00" in pm_timer
    assert "Persistent=true" in pm_timer
    assert "Unit=lpt-pocketmoney.service" in pm_timer

    fa = get_tool("fee-adjuster")
    fa_service = render_systemd_service(
        fa,
        repo,
        user="admin",
        group="admin",
        mode="systemd-timer",
        unit_exists=lambda _: False,
    )
    assert "ProtectSystem=full" in fa_service

    magma = get_tool("magma-sale")
    magma_service = render_systemd_service(
        magma,
        repo,
        user="admin",
        group="admin",
        mode="systemd-service",
        unit_exists=lambda u: u == "lnd.service",
    )
    assert "Type=simple" in magma_service
    assert "After=network-online.target lnd.service" in magma_service
    assert "WantedBy=multi-user.target" in magma_service


def test_resolve_pocketmoney_schedule_daily_and_weekly(tmp_path):
    repo = _seed_repo(tmp_path)
    assert resolve_pocketmoney_schedule(repo) == ("Fri *-*-* 09:30:00", "30 9 * * 5")

    (repo / "config.ini").write_text(
        "[Schedule]\nrecurrence = daily\ntime = 07:05\n", encoding="utf-8"
    )
    assert resolve_pocketmoney_schedule(repo) == ("*-*-* 07:05:00", "5 7 * * *")


def test_cron_managed_block_idempotency_and_preservation(tmp_path):
    repo = _seed_repo(tmp_path)
    user_cron = "0 3 * * * /home/admin/tools/pull_lndg_backup.sh >/dev/null 2>&1\n"
    fa = get_tool("fee-adjuster")
    entry = render_cron_entry(fa, "*/20 * * * *", repo)

    first = upsert_cron_block(user_cron, {"fee-adjuster": entry})
    second = upsert_cron_block(first, {"fee-adjuster": entry})
    assert first == second
    assert CRON_BEGIN in first
    assert CRON_END in first
    assert "logs/.lpt-fee-adjuster.lock" in first
    assert "/tmp/lpt-" not in first
    assert "pull_lndg_backup.sh" in first

    cleaned = remove_cron_tools(first, ["fee-adjuster"])
    assert cleaned == user_cron


def test_config_copy_no_clobber_chmod_600_and_validation(tmp_path):
    repo = _seed_repo(tmp_path, valid_configs=False)
    res1 = ensure_configs(repo, [get_tool("pocketmoney")])
    assert set(res1["copied"]) == {"config.ini", "wallets.json"}
    assert stat.S_IMODE((repo / "config.ini").stat().st_mode) == 0o600
    assert stat.S_IMODE((repo / "wallets.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((repo / "logs").stat().st_mode) == 0o700

    # Placeholders must fail validation (including INVOICEID1 from wallets.json.example)
    errors = validate_configs(repo, [get_tool("pocketmoney")])
    assert any("base_url" in err for err in errors)
    assert any("admin_key" in err for err in errors)
    assert any("wallet_id" in err for err in errors)
    assert any("invoice_key" in err for err in errors)

    # Custom contents must never be clobbered on second run
    (repo / "config.ini").write_text("[LNBits]\nbase_url = https://my.node\n", encoding="utf-8")
    res2 = ensure_configs(repo, [get_tool("pocketmoney")])
    assert res2["copied"] == []
    assert "config.ini" in res2["preserved"]
    assert "https://my.node" in (repo / "config.ini").read_text(encoding="utf-8")


def test_install_migrates_legacy_units_with_backup_and_blocks_unmanaged(tmp_path):
    repo = _seed_repo(tmp_path)
    env = FakeEnv(
        tmp_path,
        crontab="0 2 * * * /home/admin/backup.sh\n",
    )
    # Seed legacy fee_adjuster units on disk
    (env.systemd_dir / "fee_adjuster.service").write_text(
        "ExecStart=/path/Other/fee_adjuster.py\n", encoding="utf-8"
    )
    (env.systemd_dir / "fee_adjuster.timer").write_text("[Timer]\n", encoding="utf-8")

    fa = get_tool("fee-adjuster")
    res = install_tool(fa, repo, env, mode="systemd-timer")
    assert set(res["migrated_legacy"]) == {"fee_adjuster.service", "fee_adjuster.timer"}
    assert (env.systemd_dir / "lpt-fee-adjuster.service").is_file()
    assert (env.systemd_dir / "lpt-fee-adjuster.timer").is_file()
    assert not (env.systemd_dir / "fee_adjuster.service").exists()
    assert not (env.systemd_dir / "fee_adjuster.timer").exists()

    backup_dir = Path(res["backup_dir"])
    assert (backup_dir / "crontab.bak").is_file()
    assert (backup_dir / "fee_adjuster.service").is_file()

    # Unmanaged duplicate crontab must fail closed
    env_conflict = FakeEnv(
        tmp_path / "conflict",
        crontab="0 * * * * python3 /repo/LNDg/amboss_pull.py\n",
    )
    report = detect_conflicts(get_tool("amboss-pull"), env_conflict)
    assert report.has_unmanaged
    with pytest.raises(RuntimeError, match="unmanaged duplicate"):
        install_tool(get_tool("amboss-pull"), repo, env_conflict, mode="cron")


def test_uninstall_preserves_configs_by_default_and_purges_on_flag(tmp_path):
    repo = _seed_repo(tmp_path)
    env = FakeEnv(tmp_path)
    install_tool(get_tool("amboss-pull"), repo, env, mode="cron")
    assert "amboss_pull.py" in env.crontab

    out = uninstall_tools(["amboss-pull"], repo, env)
    assert out["removed_cron"] == ["amboss-pull"]
    assert out["configs_preserved"] is True
    assert (repo / "config.ini").is_file()

    out_purge = uninstall_tools(["amboss-pull"], repo, env, remove_venv=True, purge_configs=True)
    assert out_purge["venv_removed"] is True
    assert "config.ini" in out_purge["removed_configs"]
    assert not (repo / "config.ini").exists()
    assert not (repo / ".venv").exists()


def test_setup_venv_uv_and_fallback_plus_doctor(tmp_path):
    repo = _seed_repo(tmp_path)
    env_uv = FakeEnv(tmp_path / "uv", uv_available=True)
    res_uv = setup_venv(repo, env_uv, extras=("db",), recreate=True)
    assert res_uv["backend"] == "uv"
    assert any("psycopg2-binary" in cmd for cmd in res_uv["commands"])

    env_pip = FakeEnv(tmp_path / "pip", uv_available=False)
    res_pip = setup_venv(repo, env_pip, recreate=True)
    assert res_pip["backend"] == "venv"
    assert any("-m venv" in cmd for cmd in res_pip["commands"])

    # Doctor flags world-readable config permissions
    (repo / "config.ini").chmod(0o644)
    checks = {c["name"]: c for c in run_doctor(repo, env_uv)}
    assert checks["venv-certifi"]["status"] == "ok"
    assert checks["ca-bundle"]["status"] == "ok"
    assert checks["perms:config.ini"]["status"] == "warn"


def test_cli_disclaimer_donation_dry_run_and_interactive_menu(tmp_path):
    repo = _seed_repo(tmp_path)
    env = FakeEnv(tmp_path)
    lines: list[str] = []

    # Non-interactive install without --accept-disclaimer fails closed
    rc = cli_main(
        ["--repo-root", str(repo), "install", "pocketmoney", "--skip-env"],
        env=env,
        output_fn=lines.append,
    )
    assert rc == 1
    assert any("MIT" in line or "DISCLAIMER" in line for line in lines)

    # With --accept-disclaimer + --dry-run succeeds and emits donation footer
    lines.clear()
    rc = cli_main(
        [
            "--repo-root",
            str(repo),
            "install",
            "pocketmoney",
            "--scheduler",
            "systemd",
            "--accept-disclaimer",
            "--dry-run",
        ],
        env=env,
        output_fn=lines.append,
    )
    assert rc == 0
    assert any(DONATION_URL in line for line in lines)

    # Interactive menu flow: accept disclaimer -> doctor (5) -> quit (q)
    save_state(repo, {"disclaimer_version": None, "installed": {}})
    answers = iter(["yes", "5", "q"])
    menu_out: list[str] = []
    rc_menu = cli_main(
        ["--repo-root", str(repo)],
        env=env,
        input_fn=lambda _prompt: next(answers),
        output_fn=menu_out.append,
    )
    assert rc_menu == 0
    assert load_state(repo)["disclaimer_version"] == DISCLAIMER_VERSION
    assert any(DONATION_URL in line for line in menu_out)

    # JSON list & doctor subcommands
    json_out: list[str] = []
    assert cli_main(["--repo-root", str(repo), "--json", "list"], env=env, output_fn=json_out.append) == 0
    parsed = json.loads("\n".join(json_out))
    assert len(parsed) == 19


def test_cli_subcommands_env_config_doctor_install_uninstall_and_menu_actions(tmp_path):
    repo = _seed_repo(tmp_path)
    env = FakeEnv(tmp_path)
    out: list[str] = []

    assert cli_main(["--repo-root", str(repo), "status"], env=env, output_fn=out.append) == 0
    assert cli_main(["--repo-root", str(repo), "env", "--db"], env=env, output_fn=out.append) == 0
    assert cli_main(["--repo-root", str(repo), "--json", "env", "--dry-run"], env=env, output_fn=out.append) == 0
    assert cli_main(["--repo-root", str(repo), "config", "--edit", "config.ini"], env=env, output_fn=out.append) == 0
    assert cli_main(["--repo-root", str(repo), "config", "--edit", "unknown.ini"], env=env, output_fn=out.append) == 1
    assert cli_main(["--repo-root", str(repo), "--json", "doctor"], env=env, output_fn=out.append) == 0
    assert cli_main(["--repo-root", str(repo), "doctor"], env=env, output_fn=out.append) == 0

    # Real non-dry-run install (systemd + cron + manual) & uninstall subcommands
    assert (
        cli_main(
            [
                "--repo-root",
                str(repo),
                "install",
                "fee-adjuster",
                "amboss-pull",
                "--scheduler",
                "systemd",
                "--accept-disclaimer",
            ],
            env=env,
            output_fn=out.append,
        )
        == 0
    )
    assert (
        cli_main(
            [
                "--repo-root",
                str(repo),
                "install",
                "channel-base-fee",
                "--scheduler",
                "cron",
                "--accept-disclaimer",
            ],
            env=env,
            output_fn=out.append,
        )
        == 0
    )
    assert (
        cli_main(
            [
                "--repo-root",
                str(repo),
                "install",
                "ps-peers",
                "--scheduler",
                "manual",
                "--accept-disclaimer",
            ],
            env=env,
            output_fn=out.append,
        )
        == 0
    )
    assert cli_main(["--repo-root", str(repo), "uninstall", "--purge-configs"], env=env, output_fn=out.append) == 1
    assert cli_main(["--repo-root", str(repo), "uninstall"], env=env, output_fn=out.append) == 1
    assert (
        cli_main(
            ["--repo-root", str(repo), "uninstall", "--all", "--json"],
            env=env,
            output_fn=out.append,
        )
        == 0
    )

    # Interactive menu actions: 1 (env), 2 (config+edit), 3 (install), 4 (uninstall), 6 (profile), bad option, quit
    menu_out: list[str] = []
    answers = iter(
        [
            "1",
            "n",
            "2",
            "config.ini",
            "3",
            "fee-adjuster",
            "cron",
            "4",
            "fee-adjuster",
            "6",
            "vps",
            "99",
            "q",
        ]
    )
    rc = cli_main(
        ["--repo-root", str(repo)],
        env=env,
        input_fn=lambda _p: next(answers),
        output_fn=menu_out.append,
    )
    assert rc == 0
    assert any("Unknown option" in line for line in menu_out)


def test_configs_validation_edge_cases_and_editor(tmp_path):
    repo = _seed_repo(tmp_path, valid_configs=False)
    env = FakeEnv(tmp_path)

    # Missing config files detected cleanly
    errs = validate_configs(repo, [get_tool("fee-adjuster"), get_tool("magma-market-fee")])
    assert any("Missing config.ini" in e for e in errs)
    assert any("Missing feeConfig.json" in e for e in errs)

    # Invalid INI / JSON / Schedule values
    (repo / "config.ini").write_text("[bad\n", encoding="utf-8")
    (repo / "feeConfig.json").write_text("{not-json", encoding="utf-8")
    (repo / "wallets.json").write_text("[]", encoding="utf-8")
    (repo / "Magma" / "magma_config.ini").write_text("[bad\n", encoding="utf-8")
    errs2 = validate_configs(
        repo,
        [get_tool("pocketmoney"), get_tool("fee-adjuster"), get_tool("magma-market-fee")],
    )
    assert any("Invalid INI syntax in config.ini" in e for e in errs2)
    assert any("Invalid JSON in feeConfig.json" in e for e in errs2)
    assert any("wallets.json" in e for e in errs2)
    assert any("Magma/magma_config.ini" in e for e in errs2)

    (repo / "config.ini").write_text(
        "[Schedule]\nrecurrence = hourly\ntime = 99:99\n", encoding="utf-8"
    )
    errs3 = validate_configs(repo, [get_tool("pocketmoney")])
    assert any("recurrence" in e for e in errs3)
    assert any("HH:MM" in e for e in errs3)

    (repo / "config.ini").write_text(
        "[Schedule]\nrecurrence = weekly\ntime = 09:00\nday_of_week = funday\n",
        encoding="utf-8",
    )
    errs4 = validate_configs(repo, [get_tool("pocketmoney")])
    assert any("day_of_week" in e for e in errs4)

    from lpt_installer.configs import open_in_editor

    assert open_in_editor(repo / "config.ini", env) is True


def test_doctor_warnings_and_system_env_adapter(tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path)
    (repo / "logs").mkdir(exist_ok=True)
    (repo / "logs" / "root.log").write_text("x", encoding="utf-8")

    # Exercise doctor when .venv is missing, ca_bundle is missing, legacy unit exists, and file uid == 0
    (repo / ".venv" / "bin" / "python3").unlink()
    env = FakeEnv(tmp_path, ca_bundle=None)
    (env.systemd_dir / "fee_adjuster.service").write_text("[Service]\n", encoding="utf-8")

    orig_stat = Path.stat

    class FakeStat:
        def __init__(self, real_st, force_root: bool):
            self.st_mode = real_st.st_mode
            self.st_uid = 0 if force_root else real_st.st_uid

    monkeypatch.setattr(
        Path,
        "stat",
        lambda self, *a, **kw: FakeStat(
            orig_stat(self, *a, **kw), self.name == "root.log"
        ),
    )
    checks = {c["name"]: c for c in run_doctor(repo, env)}
    assert checks["venv-certifi"]["status"] == "warn"
    assert checks["ca-bundle"]["status"] == "fail"
    assert checks["ownership"]["status"] == "warn"
    assert checks["legacy-units"]["status"] == "warn"

    # Exercise real SystemEnv methods with mocked subprocess.run
    real_env = SystemEnv(
        systemd_dir=tmp_path / "sys_etc",
        user_systemd_dir=tmp_path / "sys_user",
    )
    real_env.systemd_dir.mkdir()
    (real_env.systemd_dir / "demo.service").write_text("x", encoding="utf-8")
    assert real_env.systemd_unit_exists("demo.service") is True
    assert len(real_env.list_systemd_units()) == 1
    assert real_env.list_user_systemd_units() == []
    assert isinstance(real_env.current_user_group(), tuple)

    class FakeProc:
        def __init__(self, rc=0, stdout="ok", stderr=""):
            self.returncode = rc
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: FakeProc(0, "123 python\n", ""))
    assert real_env.read_crontab() == "123 python\n"
    assert real_env.running_processes() == ["123 python"]
    assert real_env.write_crontab("0 * * * * echo ok").returncode == 0
    assert real_env.sudo_install_file("[Unit]\n", tmp_path / "out.service").returncode == 0
    assert real_env.sudo_remove_file(tmp_path / "out.service").returncode == 0
    assert real_env.systemctl("daemon-reload").returncode == 0

    # Missing crontab binary must not crash read_crontab or empty write_crontab
    monkeypatch.setattr(real_env, "which", lambda _b: None)
    assert real_env.read_crontab() == ""
    assert real_env.write_crontab("").returncode == 0
    with pytest.raises(RuntimeError, match="crontab binary not found"):
        real_env.write_crontab("0 * * * * echo hi")


def test_reinstall_running_managed_daemon_and_unknown_tool_errors(tmp_path):
    repo = _seed_repo(tmp_path)

    class DaemonEnv(FakeEnv):
        def systemctl(self, *args: str, use_sudo: bool = True, check: bool = True) -> CommandResult:
            self.commands.append(["systemctl", *args])
            if args[:4] == ("show", "-p", "MainPID", "--value"):
                return CommandResult(0, "5555\n", "")
            return CommandResult(0, "", "")

    env = DaemonEnv(tmp_path, processes=[])
    magma = get_tool("magma-sale")
    # First install creates managed unit
    install_tool(magma, repo, env, mode="systemd-service")
    # Re-install with MainPID=5555 active must succeed and call try-restart
    env._processes.append("5555 /repo/.venv/bin/python3 Magma/magma_sale_process.py")
    install_tool(magma, repo, env, mode="systemd-service")
    assert any(cmd == ["systemctl", "try-restart", "lpt-magma-sale.service"] for cmd in env.commands)

    # An extra unmanaged PID must still fail closed
    env._processes.append("6666 python3 magma_sale_process.py")
    with pytest.raises(RuntimeError, match="unmanaged duplicate"):
        install_tool(magma, repo, env, mode="systemd-service")

    # Unknown tool IDs in CLI subcommands must exit 1 cleanly (no KeyError traceback)
    out: list[str] = []
    assert cli_main(["--repo-root", str(repo), "install", "no-such-tool"], env=env, output_fn=out.append) == 1
    assert cli_main(["--repo-root", str(repo), "config", "--tools", "no-such-tool"], env=env, output_fn=out.append) == 1
    assert cli_main(["--repo-root", str(repo), "uninstall", "no-such-tool"], env=env, output_fn=out.append) == 1


def test_cli_config_harden_tightens_existing_without_copying_templates(tmp_path):
    repo = _seed_repo(tmp_path, valid_configs=False)
    (repo / "config.ini").write_text("[credentials]\n", encoding="utf-8")
    (repo / "wallets.json").write_text("{}", encoding="utf-8")
    (repo / "config.ini").chmod(0o644)
    (repo / "wallets.json").chmod(0o644)
    (repo / "logs").mkdir(exist_ok=True)
    (repo / "logs").chmod(0o755)

    env = FakeEnv(tmp_path)
    out: list[str] = []

    # --dry-run must not mutate permissions or copy missing templates
    assert (
        cli_main(
            ["--repo-root", str(repo), "config", "--harden", "--dry-run"],
            env=env,
            output_fn=out.append,
        )
        == 0
    )
    assert stat.S_IMODE((repo / "config.ini").stat().st_mode) == 0o644
    assert not (repo / "Magma" / "magma_config.ini").exists()
    assert not (repo / "feeConfig.json").exists()

    # Live --harden tightens existing files/dirs to 0600/0700 without copying missing templates
    out.clear()
    assert (
        cli_main(
            ["--repo-root", str(repo), "config", "--harden"],
            env=env,
            output_fn=out.append,
        )
        == 0
    )
    assert stat.S_IMODE((repo / "config.ini").stat().st_mode) == 0o600
    assert stat.S_IMODE((repo / "wallets.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((repo / "logs").stat().st_mode) == 0o700
    assert not (repo / "Magma" / "magma_config.ini").exists()
    assert not (repo / "feeConfig.json").exists()

