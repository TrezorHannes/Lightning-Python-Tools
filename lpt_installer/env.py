"""Python virtual environment setup and operator diagnostics (doctor)."""

from __future__ import annotations

import sys
from pathlib import Path

from lpt_installer.adapters import SystemEnv
from lpt_installer.configs import CONFIG_TEMPLATES
from lpt_installer.registry import TOOLS

MIN_CERTIFI_VERSION = (2026, 10, 19)


def _parse_version(raw: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in raw.strip().split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def venv_python_path(repo_root: Path) -> Path:
    return repo_root / ".venv" / "bin" / "python3"


def setup_venv(
    repo_root: Path,
    env: SystemEnv,
    *,
    extras: tuple[str, ...] = (),
    dry_run: bool = False,
    recreate: bool = False,
) -> dict[str, object]:
    """Create/update repo .venv using uv fast-path or stdlib venv + pip."""
    if env.current_uid() == 0:
        raise PermissionError("Refusing to create or modify .venv as root.")

    req_file = repo_root / "requirements.txt"
    if not req_file.is_file():
        raise FileNotFoundError(f"Missing requirements.txt at {req_file}")

    venv_dir = repo_root / ".venv"
    venv_py = venv_python_path(repo_root)
    uv_bin = env.which("uv")
    backend = "uv" if uv_bin else "venv"
    commands: list[list[str]] = []

    if recreate or not venv_py.exists():
        if uv_bin:
            cmd = [uv_bin, "venv", str(venv_dir), "--python", sys.executable]
            if recreate:
                cmd.append("--clear")
            commands.append(cmd)
        else:
            cmd = [sys.executable, "-m", "venv"]
            if recreate:
                cmd.append("--clear")
            cmd.append(str(venv_dir))
            commands.append(cmd)

    if uv_bin:
        commands.append(
            [uv_bin, "pip", "install", "--python", str(venv_py), "-r", str(req_file)]
        )
        if "db" in extras:
            commands.append(
                [
                    uv_bin,
                    "pip",
                    "install",
                    "--python",
                    str(venv_py),
                    "psycopg2-binary",
                    "pandas",
                ]
            )
    else:
        commands.append([str(venv_py), "-m", "pip", "install", "--upgrade", "pip"])
        commands.append([str(venv_py), "-m", "pip", "install", "-r", str(req_file)])
        if "db" in extras:
            commands.append(
                [str(venv_py), "-m", "pip", "install", "psycopg2-binary", "pandas"]
            )

    if not dry_run:
        for cmd in commands:
            env.run(cmd, check=True)

    return {
        "backend": backend,
        "venv_dir": str(venv_dir),
        "python": str(venv_py),
        "extras": list(extras),
        "commands": [" ".join(c) for c in commands],
    }


def run_doctor(repo_root: Path, env: SystemEnv) -> list[dict[str, str]]:
    """Run non-destructive environment, TLS/CA trust, permission, and conflict checks."""
    checks: list[dict[str, str]] = []

    # 1. Host Python check
    ver = sys.version_info[:3]
    ver_str = f"{ver[0]}.{ver[1]}.{ver[2]}"
    if (3, 11) <= sys.version_info < (3, 14):
        checks.append({"name": "python", "status": "ok", "detail": f"Python {ver_str}"})
    else:
        checks.append(
            {
                "name": "python",
                "status": "fail",
                "detail": f"Python {ver_str} outside supported 3.11-3.13 range",
            }
        )

    # 2. .venv & certifi check
    venv_py = venv_python_path(repo_root)
    if venv_py.exists():
        res = env.run(
            [str(venv_py), "-c", "import certifi; print(certifi.__version__)"],
            check=False,
        )
        if res.returncode == 0:
            certifi_ver = res.stdout.strip()
            if _parse_version(certifi_ver) >= MIN_CERTIFI_VERSION:
                checks.append(
                    {
                        "name": "venv-certifi",
                        "status": "ok",
                        "detail": f".venv active (certifi {certifi_ver})",
                    }
                )
            else:
                checks.append(
                    {
                        "name": "venv-certifi",
                        "status": "warn",
                        "detail": f"certifi {certifi_ver} < 2026.10.19; run env setup to upgrade",
                    }
                )
        else:
            checks.append(
                {
                    "name": "venv-certifi",
                    "status": "warn",
                    "detail": ".venv exists but certifi could not be imported",
                }
            )
    else:
        checks.append(
            {
                "name": "venv-certifi",
                "status": "warn",
                "detail": ".venv not created yet (run './install.sh env')",
            }
        )

    # 3. OS CA trust bundle check
    ca_bundle = env.detect_ca_bundle()
    if ca_bundle:
        checks.append(
            {"name": "ca-bundle", "status": "ok", "detail": f"OS CA bundle: {ca_bundle}"}
        )
    else:
        checks.append(
            {
                "name": "ca-bundle",
                "status": "fail",
                "detail": "No OS CA bundle found in standard /etc/ssl or /etc/pki paths",
            }
        )

    # 4. Config file permissions check (0600)
    for rel_path in CONFIG_TEMPLATES:
        cfg_path = repo_root / rel_path
        if not cfg_path.exists():
            continue
        mode = cfg_path.stat().st_mode & 0o777
        if mode & 0o077:
            checks.append(
                {
                    "name": f"perms:{rel_path}",
                    "status": "warn",
                    "detail": f"{rel_path} mode {oct(mode)} is group/world readable; run './install.sh config --harden'",
                }
            )
        else:
            checks.append(
                {
                    "name": f"perms:{rel_path}",
                    "status": "ok",
                    "detail": f"{rel_path} permissions {oct(mode)}",
                }
            )

    # 5. Root-owned files inside logs/ or data/
    if env.current_uid() != 0:
        root_owned: list[str] = []
        for sub in ("logs", "data"):
            sub_dir = repo_root / sub
            if not sub_dir.is_dir():
                continue
            for item in sorted(sub_dir.iterdir()):
                try:
                    if item.stat().st_uid == 0:
                        root_owned.append(str(item.relative_to(repo_root)))
                except OSError:
                    continue
        if root_owned:
            checks.append(
                {
                    "name": "ownership",
                    "status": "warn",
                    "detail": (
                        "Root-owned runtime files found ("
                        + ", ".join(root_owned)
                        + "); fix with sudo chown before switching services from root"
                    ),
                }
            )
        else:
            checks.append(
                {
                    "name": "ownership",
                    "status": "ok",
                    "detail": "No root-owned files in logs/ or data/",
                }
            )

    # 6. Legacy unit hints
    legacy_found: list[str] = []
    for tool in TOOLS:
        for unit in tool.legacy_units:
            if env.systemd_unit_exists(unit):
                legacy_found.append(unit)
    if legacy_found:
        checks.append(
            {
                "name": "legacy-units",
                "status": "warn",
                "detail": (
                    "Legacy systemd units detected ("
                    + ", ".join(sorted(set(legacy_found)))
                    + "); installing via ./install.sh will back up and migrate them"
                ),
            }
        )

    return checks
