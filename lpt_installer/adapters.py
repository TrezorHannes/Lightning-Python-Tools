"""Injectable OS/subprocess/systemd/cron adapters for lpt_installer."""

from __future__ import annotations

import grp
import os
import pwd
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    """Result of an external command invocation."""

    returncode: int
    stdout: str
    stderr: str


class SystemEnv:
    """Concrete OS adapter wrapping subprocess, filesystem, systemd, and cron."""

    CA_BUNDLE_CANDIDATES: tuple[str, ...] = (
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
        "/etc/ssl/cert.pem",
    )

    def __init__(
        self,
        *,
        systemd_dir: Path | None = None,
        user_systemd_dir: Path | None = None,
    ) -> None:
        self.systemd_dir = systemd_dir or Path("/etc/systemd/system")
        self.user_systemd_dir = user_systemd_dir or (
            Path.home() / ".config" / "systemd" / "user"
        )

    def run(
        self,
        argv: list[str],
        *,
        input_text: str | None = None,
        check: bool = False,
        env_vars: dict[str, str] | None = None,
        interactive: bool = False,
    ) -> CommandResult:
        merged_env = os.environ.copy()
        if env_vars:
            merged_env.update(env_vars)
        if interactive:
            proc = subprocess.run(argv, env=merged_env, check=False)
            result = CommandResult(proc.returncode, "", "")
        else:
            proc = subprocess.run(
                argv,
                input=input_text,
                capture_output=True,
                text=True,
                env=merged_env,
                check=False,
            )
            result = CommandResult(proc.returncode, proc.stdout, proc.stderr)
        if check and result.returncode != 0:
            raise RuntimeError(
                f"Command {' '.join(argv)} failed (exit {result.returncode}): {result.stderr.strip()}"
            )
        return result

    def which(self, binary: str) -> str | None:
        return shutil.which(binary)

    def current_uid(self) -> int:
        return os.geteuid()

    def current_user_group(self) -> tuple[str, str]:
        uid = os.getuid()
        pw = pwd.getpwuid(uid)
        gr = grp.getgrgid(pw.pw_gid)
        return pw.pw_name, gr.gr_name

    def home_dir(self) -> Path:
        return Path.home()

    def env_var(self, name: str, default: str = "") -> str:
        return os.environ.get(name, default)

    def detect_ca_bundle(self) -> str | None:
        for candidate in self.CA_BUNDLE_CANDIDATES:
            path = Path(candidate)
            if path.is_file():
                return candidate
        return None

    def systemd_unit_exists(self, unit_name: str) -> bool:
        return (self.systemd_dir / unit_name).exists()

    def list_systemd_units(self, pattern: str = "*.service") -> list[Path]:
        if not self.systemd_dir.is_dir():
            return []
        return sorted(self.systemd_dir.glob(pattern))

    def list_user_systemd_units(self, pattern: str = "*.service") -> list[Path]:
        if not self.user_systemd_dir.is_dir():
            return []
        return sorted(self.user_systemd_dir.glob(pattern))

    def sudo_install_file(
        self, content: str, dest: Path, mode: str = "0644"
    ) -> CommandResult:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, prefix="lpt_unit_"
        ) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        try:
            return self.run(
                [
                    "sudo",
                    "install",
                    "-m",
                    mode,
                    "-o",
                    "root",
                    "-g",
                    "root",
                    str(tmp_path),
                    str(dest),
                ],
                check=True,
            )
        finally:
            tmp_path.unlink(missing_ok=True)

    def sudo_remove_file(self, dest: Path) -> CommandResult:
        return self.run(["sudo", "rm", "-f", str(dest)], check=True)

    def systemctl(
        self, *args: str, use_sudo: bool = True, check: bool = True
    ) -> CommandResult:
        cmd = ["sudo", "systemctl", *args] if use_sudo else ["systemctl", *args]
        return self.run(cmd, check=check)

    def read_crontab(self) -> str:
        res = self.run(["crontab", "-l"], check=False)
        if res.returncode != 0:
            # No crontab installed yet for current user
            return ""
        return res.stdout

    def write_crontab(self, content: str) -> CommandResult:
        if not content.strip():
            # Keep user crontab empty cleanly without failing if none existed
            res = self.run(["crontab", "-r"], check=False)
            if res.returncode == 0 or "no crontab" in res.stderr.lower():
                return CommandResult(0, "", "")
            return self.run(["crontab", "-"], input_text="", check=True)
        normalized = content if content.endswith("\n") else f"{content}\n"
        return self.run(["crontab", "-"], input_text=normalized, check=True)

    def running_processes(self) -> list[str]:
        res = self.run(["ps", "-eo", "pid=,args="], check=False)
        if res.returncode != 0:
            return []
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]
