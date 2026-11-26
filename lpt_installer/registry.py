"""Single-source-of-truth registry for all 19 Lightning-Python-Tools scripts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolSpec:
    """Metadata describing an installable/manageable Lightning-Python-Tools script."""

    id: str
    script_path: str
    category: str
    summary: str
    risk_tier: str  # "FUNDS", "FEES/POLICY", "READ-ONLY"
    host_profiles: tuple[str, ...]  # subset of ("node", "vps")
    supported_modes: tuple[str, ...]  # ("systemd-service", "systemd-timer", "cron", "manual")
    default_mode: str
    unattended_args: tuple[str, ...] = ()
    default_on_calendar: str | None = None
    default_cron: str | None = None
    persistent_timer: bool = False
    sandbox_level: str = "full"  # "strict" (HTTP-only) or "full"
    after_units: tuple[str, ...] = ()
    wants_units: tuple[str, ...] = ()
    config_templates: tuple[str, ...] = ()
    required_ini_keys: tuple[tuple[str, str], ...] = ()
    optional_extras: tuple[str, ...] = ()
    dry_run_args: tuple[str, ...] | None = None
    legacy_units: tuple[str, ...] = ()

    @property
    def is_schedulable(self) -> bool:
        return any(
            mode in ("systemd-service", "systemd-timer", "cron")
            for mode in self.supported_modes
        )


TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        id="pocketmoney",
        script_path="LNBits/pocketmoney.py",
        category="LNBits",
        summary="Scheduled fiat-to-sat child allowances via LNbits + Telegram alerts",
        risk_tier="FUNDS",
        host_profiles=("node", "vps"),
        supported_modes=("systemd-timer", "cron", "systemd-service"),
        default_mode="systemd-timer",
        unattended_args=("--run-once",),
        default_on_calendar="dynamic",
        default_cron="dynamic",
        persistent_timer=True,
        sandbox_level="strict",
        config_templates=("config.ini", "wallets.json"),
        required_ini_keys=(
            ("LNBits", "base_url"),
            ("LNBits", "admin_key"),
            ("telegram", "lnbits_bot_token"),
            ("telegram", "telegram_user_id"),
        ),
        legacy_units=("pocketmoney.service", "pocketmoney.timer"),
    ),
    ToolSpec(
        id="magma-sale",
        script_path="Magma/magma_sale_process.py",
        category="Magma",
        summary="Auto-pilot daemon for Amboss Magma channel sale orders",
        risk_tier="FUNDS",
        host_profiles=("node",),
        supported_modes=("systemd-service",),
        default_mode="systemd-service",
        after_units=("lnd.service",),
        config_templates=("config.ini",),
        required_ini_keys=(
            ("credentials", "amboss_authorization"),
            ("telegram", "magma_bot_token"),
            ("telegram", "telegram_user_id"),
            ("paths", "lncli_path"),
        ),
        legacy_units=("magma-sale.service", "magma-autosale-bot.service"),
    ),
    ToolSpec(
        id="magma-market-fee",
        script_path="Magma/magma_market_fee.py",
        category="Magma",
        summary="Analyzes Magma offers and updates seller offer pricing/status",
        risk_tier="FEES/POLICY",
        host_profiles=("node",),
        supported_modes=("systemd-timer", "cron", "manual"),
        default_mode="systemd-timer",
        unattended_args=("--force",),
        default_on_calendar="daily",
        default_cron="0 6 * * *",
        dry_run_args=("--dry-run",),
        config_templates=("config.ini", "Magma/magma_config.ini"),
        required_ini_keys=(
            ("credentials", "amboss_authorization"),
            ("paths", "lncli_path"),
        ),
    ),
    ToolSpec(
        id="fee-adjuster",
        script_path="Other/fee_adjuster.py",
        category="Other",
        summary="Dynamic 5-band + stuck-channel + inbound-discount fee optimizer",
        risk_tier="FEES/POLICY",
        host_profiles=("node", "vps"),
        supported_modes=("systemd-timer", "cron", "manual"),
        default_mode="systemd-timer",
        default_on_calendar="*:0/20:00",
        default_cron="*/20 * * * *",
        persistent_timer=True,
        dry_run_args=("--debug",),
        config_templates=("config.ini", "feeConfig.json"),
        required_ini_keys=(
            ("credentials", "amboss_authorization"),
            ("credentials", "lndg_username"),
            ("credentials", "lndg_password"),
            ("lndg", "lndg_api_url"),
        ),
        legacy_units=("fee_adjuster.service", "fee_adjuster.timer"),
    ),
    ToolSpec(
        id="rebalance-guard",
        script_path="Other/rebalance_guard.py",
        category="Other",
        summary="Locks ar_out_target=100% on discounted channels with hysteresis",
        risk_tier="FEES/POLICY",
        host_profiles=("node", "vps"),
        supported_modes=("systemd-timer", "cron", "manual"),
        default_mode="systemd-timer",
        default_on_calendar="*:05/20:00",
        default_cron="5,25,45 * * * *",
        dry_run_args=("--dry-run",),
        config_templates=("config.ini", "feeConfig.json"),
        required_ini_keys=(
            ("credentials", "lndg_username"),
            ("credentials", "lndg_password"),
            ("lndg", "lndg_api_url"),
        ),
    ),
    ToolSpec(
        id="amboss-pull",
        script_path="LNDg/amboss_pull.py",
        category="LNDg",
        summary="Syncs Magma leases to LNDg notes/auto-fees and charge-lnd caps",
        risk_tier="FEES/POLICY",
        host_profiles=("node", "vps"),
        supported_modes=("systemd-timer", "cron", "manual"),
        default_mode="systemd-timer",
        default_on_calendar="hourly",
        default_cron="0 * * * *",
        persistent_timer=True,
        config_templates=("config.ini",),
        required_ini_keys=(
            ("credentials", "amboss_authorization"),
            ("credentials", "lndg_username"),
            ("credentials", "lndg_password"),
            ("lndg", "lndg_api_url"),
        ),
    ),
    ToolSpec(
        id="channel-base-fee",
        script_path="LNDg/channel_base-fee.py",
        category="LNDg",
        summary="Resets base_fee to 0 on open active LNDg channels",
        risk_tier="FEES/POLICY",
        host_profiles=("node", "vps"),
        supported_modes=("systemd-timer", "cron", "manual"),
        default_mode="systemd-timer",
        default_on_calendar="*:59:00",
        default_cron="59 * * * *",
        config_templates=("config.ini",),
        required_ini_keys=(
            ("credentials", "lndg_username"),
            ("credentials", "lndg_password"),
            ("lndg", "lndg_api_url"),
        ),
    ),
    ToolSpec(
        id="channel-fee-pull",
        script_path="LNDg/channel_fee-pull.py",
        category="LNDg",
        summary="Exports 0 ppm open channel IDs to ~/.chargelnd/.config/0_fee.txt",
        risk_tier="FEES/POLICY",
        host_profiles=("node",),
        supported_modes=("systemd-timer", "cron", "manual"),
        default_mode="systemd-timer",
        default_on_calendar="*:58:00",
        default_cron="58 * * * *",
        config_templates=("config.ini",),
        required_ini_keys=(
            ("credentials", "lndg_username"),
            ("credentials", "lndg_password"),
            ("lndg", "lndg_api_url"),
        ),
    ),
    ToolSpec(
        id="disabled-fee-accelerator",
        script_path="LNDg/disabled_fee-accelerator.py",
        category="LNDg",
        summary="Bumps local_fee_rate on depleted channels disabled by charge-lnd",
        risk_tier="FEES/POLICY",
        host_profiles=("node", "vps"),
        supported_modes=("systemd-timer", "cron", "manual"),
        default_mode="systemd-timer",
        default_on_calendar="*:02:00",
        default_cron="2 * * * *",
        config_templates=("config.ini",),
        required_ini_keys=(
            ("credentials", "lndg_username"),
            ("credentials", "lndg_password"),
            ("lndg", "lndg_api_url"),
        ),
    ),
    ToolSpec(
        id="mempool-rebalancer-trigger",
        script_path="LNDg/mempool_rebalancer_trigger.py",
        category="LNDg",
        summary="Polls mempool.space and toggles LNDg AR-Enabled on fee spikes",
        risk_tier="FEES/POLICY",
        host_profiles=("node", "vps"),
        supported_modes=("systemd-service",),
        default_mode="systemd-service",
        config_templates=("config.ini",),
        required_ini_keys=(
            ("credentials", "lndg_username"),
            ("credentials", "lndg_password"),
            ("lndg", "lndg_api_url"),
        ),
        legacy_units=("mempool_rebalancer_trigger.service",),
    ),
    ToolSpec(
        id="offline-summary",
        script_path="LNDg/offline_summary.py",
        category="LNDg",
        summary="Monthly peer offline-duration report from LNDg PostgreSQL",
        risk_tier="READ-ONLY",
        host_profiles=("node",),
        supported_modes=("manual",),
        default_mode="manual",
        optional_extras=("db",),
        config_templates=("config.ini",),
        required_ini_keys=(("database", "name"),),
    ),
    ToolSpec(
        id="swap-out-candidates",
        script_path="LNDg/swap_out-candidates.py",
        category="LNDg",
        summary="Lists high-local low-PPM swap-out candidates (+ optional bos export)",
        risk_tier="READ-ONLY",
        host_profiles=("node",),
        supported_modes=("manual", "systemd-timer", "cron"),
        default_mode="manual",
        default_on_calendar="daily",
        default_cron="0 4 * * *",
        config_templates=("config.ini",),
        required_ini_keys=(
            ("credentials", "lndg_username"),
            ("credentials", "lndg_password"),
            ("lndg", "lndg_api_url"),
        ),
    ),
    ToolSpec(
        id="swap-out-loop",
        script_path="Other/swap_out-loop.py",
        category="Other",
        summary="Economically scored Loop Out optimizer with live route probing",
        risk_tier="FUNDS",
        host_profiles=("node",),
        supported_modes=("manual",),
        default_mode="manual",
        dry_run_args=("--dry-run",),
        config_templates=("config.ini",),
        required_ini_keys=(
            ("credentials", "lndg_username"),
            ("credentials", "lndg_password"),
            ("lndg", "lndg_api_url"),
            ("paths", "lncli_path"),
        ),
    ),
    ToolSpec(
        id="boltz-swap-out",
        script_path="Other/boltz_swap-out.py",
        category="Other",
        summary="Reverse submarine swaps (LN -> Liquid L-BTC) via boltzcli",
        risk_tier="FUNDS",
        host_profiles=("node",),
        supported_modes=("manual",),
        default_mode="manual",
        dry_run_args=("--debug",),
        config_templates=("config.ini",),
        required_ini_keys=(
            ("credentials", "lndg_username"),
            ("credentials", "lndg_password"),
            ("lndg", "lndg_api_url"),
            ("paths", "boltzcli_path"),
        ),
    ),
    ToolSpec(
        id="swap-wallet",
        script_path="Other/swap_wallet.py",
        category="Other",
        summary="Interactive batched LN payments to Lightning Address via bos send",
        risk_tier="FUNDS",
        host_profiles=("node",),
        supported_modes=("manual",),
        default_mode="manual",
        config_templates=("config.ini",),
        required_ini_keys=(
            ("paths", "lncli_path"),
            ("system", "full_path_bos"),
        ),
    ),
    ToolSpec(
        id="lnd-utxo-consolidator",
        script_path="Other/lnd_utxo_consolidator.py",
        category="Other",
        summary="Interactive UTXO selector printing a lncli sendcoins command",
        risk_tier="READ-ONLY",
        host_profiles=("node",),
        supported_modes=("manual",),
        default_mode="manual",
        config_templates=("config.ini",),
        required_ini_keys=(("paths", "lncli_path"),),
    ),
    ToolSpec(
        id="peerswap-bot",
        script_path="Peerswap/peerswap-bot.py",
        category="Peerswap",
        summary="Telegram bot daemon wrapping pscli swaps and L-BTC sends",
        risk_tier="FUNDS",
        host_profiles=("node",),
        supported_modes=("systemd-service",),
        default_mode="systemd-service",
        after_units=("peerswapd.service",),
        wants_units=("peerswapd.service",),
        config_templates=("config.ini",),
        required_ini_keys=(
            ("telegram", "peerswap_bot_token"),
            ("telegram", "telegram_user_id"),
        ),
        legacy_units=("peerswap-bot.service",),
    ),
    ToolSpec(
        id="peerswap-lndg-push",
        script_path="Peerswap/peerswap-lndg_push.py",
        category="Peerswap",
        summary="Pushes PeerSwap swap stats from pscli into LNDg channel notes",
        risk_tier="READ-ONLY",
        host_profiles=("node",),
        supported_modes=("systemd-timer", "cron", "manual"),
        default_mode="systemd-timer",
        unattended_args=("-a",),
        default_on_calendar="hourly",
        default_cron="0 * * * *",
        dry_run_args=("-a", "--dry-run"),
        config_templates=("config.ini",),
        required_ini_keys=(
            ("credentials", "lndg_username"),
            ("credentials", "lndg_password"),
            ("lndg", "lndg_api_url"),
            ("paths", "pscli_path"),
        ),
    ),
    ToolSpec(
        id="ps-peers",
        script_path="Peerswap/ps_peers.py",
        category="Peerswap",
        summary="Terminal table of L-BTC balance and PeerSwap peer channel policies",
        risk_tier="READ-ONLY",
        host_profiles=("node",),
        supported_modes=("manual",),
        default_mode="manual",
        config_templates=(),
        required_ini_keys=(),
    ),
)

TOOLS_BY_ID: dict[str, ToolSpec] = {tool.id: tool for tool in TOOLS}


def get_tool(tool_id: str) -> ToolSpec:
    """Return a ToolSpec by ID or raise KeyError with helpful context."""
    if tool_id not in TOOLS_BY_ID:
        raise KeyError(f"Unknown tool ID: {tool_id!r}")
    return TOOLS_BY_ID[tool_id]


def filter_tools(profile: str = "all") -> list[ToolSpec]:
    """Return tools matching the requested host profile ('node', 'vps', 'all')."""
    if profile == "all":
        return list(TOOLS)
    if profile not in ("node", "vps"):
        raise ValueError(f"Invalid host profile: {profile!r}")
    return [tool for tool in TOOLS if profile in tool.host_profiles]


def schedulable_tools(profile: str = "all") -> list[ToolSpec]:
    """Return tools that support systemd or cron scheduling."""
    return [tool for tool in filter_tools(profile) if tool.is_schedulable]


def render_overview_table(
    tools: list[ToolSpec], statuses: dict[str, str] | None = None
) -> str:
    """Render an aligned plain-text overview table for CLI navigation."""
    headers = ("#", "ID", "Risk", "Default Mode", "Hosts", "Status", "Description")
    rows: list[tuple[str, ...]] = []
    for idx, tool in enumerate(tools, start=1):
        status_str = (statuses or {}).get(tool.id, "unmanaged")
        hosts = ",".join(tool.host_profiles)
        rows.append(
            (
                str(idx),
                tool.id,
                tool.risk_tier,
                tool.default_mode,
                hosts,
                status_str,
                tool.summary,
            )
        )

    widths = [
        max(len(headers[col]), *(len(row[col]) for row in rows)) if rows else len(headers[col])
        for col in range(len(headers))
    ]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers), fmt.format(*("-" * w for w in widths))]
    lines.extend(fmt.format(*row) for row in rows)
    return "\n".join(lines)


def render_markdown_overview_table(tools: tuple[ToolSpec, ...] = TOOLS) -> str:
    """Render a Markdown overview table kept in sync with registry.TOOLS."""
    lines = [
        "| ID | Script | Risk | Modes | Hosts | Description |",
        "| :--- | :--- | :--- | :--- | :--- | :--- |",
    ]
    for tool in tools:
        modes = ", ".join(tool.supported_modes)
        hosts = ", ".join(tool.host_profiles)
        lines.append(
            f"| `{tool.id}` | `{tool.script_path}` | **{tool.risk_tier}** | "
            f"{modes} | {hosts} | {tool.summary} |"
        )
    return "\n".join(lines)
