from __future__ import annotations

import argparse
import io
import json
import sys
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from xian_cli.commands.catalog import _load_template
from xian_cli.commands.common import _pick_template_value
from xian_cli.commands.network import _handle_network_create, _handle_network_join
from xian_cli.commands.node import _handle_node_health, _handle_node_start
from xian_cli.config_repo import resolve_network_manifest_path
from xian_cli.models import read_network_manifest

PRESET_TEMPLATES = {
    "basic": "single-node-dev",
    "indexed": "single-node-indexed",
}

BLOCK_POLICY_CHOICES = [
    ("on_demand", "wait for transactions; no idle empty blocks"),
    ("idle_interval", "emit an empty block after the chain has been idle for the interval"),
    ("periodic", "emit scheduled empty blocks after the empty-block interval"),
]

RUNTIME_ARG_DEFAULTS = {
    "enable_bds": None,
    "enable_pruning": None,
    "blocks_to_keep": None,
    "block_policy_mode": None,
    "block_policy_interval": None,
    "transaction_trace_logging": None,
    "app_log_level": None,
    "app_log_json": None,
    "app_log_rotation_hours": None,
    "app_log_retention_days": None,
    "simulation_enabled": None,
    "simulation_max_concurrency": None,
    "simulation_timeout_ms": None,
    "simulation_max_chi": None,
    "tx_fee_mode": None,
    "free_tx_max_chi": None,
    "free_block_max_chi": None,
    "parallel_execution_enabled": None,
    "parallel_execution_workers": None,
    "parallel_execution_min_transactions": None,
    "enable_dashboard": None,
    "enable_monitoring": None,
    "dashboard_host": "127.0.0.1",
    "dashboard_port": None,
    "enable_intentkit": None,
    "intentkit_network_id": None,
    "intentkit_host": None,
    "intentkit_port": None,
    "intentkit_api_port": None,
    "enable_dex_automation": None,
    "dex_automation_host": None,
    "dex_automation_port": None,
    "dex_automation_config": None,
    "enable_shielded_relayer": None,
    "shielded_relayer_host": None,
    "shielded_relayer_port": None,
}

RUNTIME_ARG_FLAGS = {
    "enable_bds": "--enable-bds",
    "enable_pruning": "--enable-pruning",
    "blocks_to_keep": "--blocks-to-keep",
    "block_policy_mode": "--block-policy-mode",
    "block_policy_interval": "--block-policy-interval",
    "transaction_trace_logging": "--transaction-trace-logging",
    "app_log_level": "--app-log-level",
    "app_log_json": "--app-log-json",
    "app_log_rotation_hours": "--app-log-rotation-hours",
    "app_log_retention_days": "--app-log-retention-days",
    "simulation_enabled": "--simulation-enabled",
    "simulation_max_concurrency": "--simulation-max-concurrency",
    "simulation_timeout_ms": "--simulation-timeout-ms",
    "simulation_max_chi": "--simulation-max-chi",
    "tx_fee_mode": "--tx-fee-mode",
    "free_tx_max_chi": "--free-tx-max-chi",
    "free_block_max_chi": "--free-block-max-chi",
    "parallel_execution_enabled": "--parallel-execution-enabled",
    "parallel_execution_workers": "--parallel-execution-workers",
    "parallel_execution_min_transactions": "--parallel-execution-min-transactions",
    "enable_dashboard": "--enable-dashboard",
    "enable_monitoring": "--enable-monitoring",
    "dashboard_host": "--dashboard-host",
    "dashboard_port": "--dashboard-port",
    "enable_intentkit": "--enable-intentkit",
    "intentkit_network_id": "--intentkit-network-id",
    "intentkit_host": "--intentkit-host",
    "intentkit_port": "--intentkit-port",
    "intentkit_api_port": "--intentkit-api-port",
    "enable_dex_automation": "--enable-dex-automation",
    "dex_automation_host": "--dex-automation-host",
    "dex_automation_port": "--dex-automation-port",
    "dex_automation_config": "--dex-automation-config",
    "enable_shielded_relayer": "--enable-shielded-relayer",
    "shielded_relayer_host": "--shielded-relayer-host",
    "shielded_relayer_port": "--shielded-relayer-port",
}


@dataclass(frozen=True)
class SetupNodePlan:
    mode: str
    name: str
    network: str
    chain_id: str | None
    template: str
    key_mode: str
    validator_key_ref: Path | None
    validator_key_dir: Path | None
    restore_snapshot: bool
    start: bool
    base_dir: Path
    configs_dir: Path | None
    stack_dir: Path | None
    home: Path | None
    network_manifest: Path | None
    snapshot_url: str | None
    snapshot_signing_keys: list[str]
    seed: list[str]
    genesis_source: str | None
    genesis_bundle: str
    node_image_mode: str | None
    node_integrated_image: str | None
    node_split_image: str | None
    moniker: str | None
    force: bool
    rpc_url: str
    rpc_timeout_seconds: float
    skip_disk_check: bool
    block_policy_mode: str
    block_policy_interval: str
    block_policy_source: str
    runtime_args: dict[str, Any]

    @property
    def profile_path(self) -> Path:
        return self.base_dir / "nodes" / f"{self.name}.json"

    @property
    def key_dir(self) -> Path | None:
        if self.key_mode != "generate":
            return None
        if self.validator_key_dir is not None:
            if not self.validator_key_dir.is_absolute():
                return self.base_dir / self.validator_key_dir
            return self.validator_key_dir
        return self.base_dir / "keys" / self.name

    @property
    def local_manifest_path(self) -> Path:
        return self.base_dir / "networks" / self.network / "manifest.json"


def _path_str(path: Path | None) -> str | None:
    return None if path is None else str(path)


def _add_path_option(command: list[str], flag: str, value: Path | None) -> None:
    if value is not None:
        command.extend([flag, str(value)])


def _add_value_option(command: list[str], flag: str, value: str | float | int | None) -> None:
    if value is not None:
        command.extend([flag, str(value)])


def _add_repeated_option(command: list[str], flag: str, values: list[str]) -> None:
    for value in values:
        command.extend([flag, value])


def _add_runtime_options(command: list[str], values: dict[str, Any]) -> None:
    for key in RUNTIME_ARG_DEFAULTS:
        if key not in values:
            continue
        flag = RUNTIME_ARG_FLAGS[key]
        value = values[key]
        if isinstance(value, bool):
            command.append(flag if value else f"--no-{flag[2:]}")
        else:
            command.extend([flag, str(value)])


def _default_chain_id(network: str) -> str:
    if network in {"local", "local-dev"}:
        return "xian-local-1"
    normalized = "".join(ch if ch.isalnum() else "-" for ch in network.lower()).strip("-")
    return f"xian-{normalized or 'local'}-1"


def _template_for_preset(preset: str) -> str:
    return PRESET_TEMPLATES[preset]


def _block_policy_source(
    *,
    args: argparse.Namespace,
    template: dict | None,
    network: dict | None,
) -> str:
    if args.block_policy_mode is not None or args.block_policy_interval is not None:
        return "arguments"
    if template is not None and (
        template.get("block_policy_mode") is not None
        or template.get("block_policy_interval") is not None
    ):
        return "template"
    if network is not None and (
        network.get("block_policy_mode") is not None
        or network.get("block_policy_interval") is not None
    ):
        return "network"
    return "default"


def _has_block_policy_defaults(source: dict | None) -> bool:
    return source is not None and (
        source.get("block_policy_mode") is not None
        or source.get("block_policy_interval") is not None
    )


def _describe_block_policy(mode: str, interval: str) -> str:
    if mode == "on_demand":
        return "on_demand (blocks are produced when transactions arrive)"
    if mode == "idle_interval":
        return f"idle_interval ({interval} idle empty-block interval)"
    return f"periodic ({interval} scheduled empty-block interval)"


def _prompt_interval_default(current_interval: str) -> str:
    return current_interval if current_interval != "0s" else "1s"


def _validate_block_policy_pair(mode: str, interval: str) -> tuple[str, str]:
    if mode == "on_demand":
        return mode, "0s"
    if interval == "0s":
        raise ValueError(
            "--block-policy-interval must be non-zero when "
            "--block-policy-mode is idle_interval or periodic"
        )
    return mode, interval


def _resolve_block_policy(
    *,
    args: argparse.Namespace,
    base_dir: Path,
    mode: str,
    network_name: str,
    template_name: str,
    prompt: bool,
    runtime_args: dict[str, Any],
) -> tuple[str, str, str]:
    template = _load_template(
        base_dir=base_dir,
        template_name=template_name,
        configs_dir=args.configs_dir,
    )
    network = None
    if mode == "join" and not _has_block_policy_defaults(template):
        manifest_path = resolve_network_manifest_path(
            base_dir=base_dir,
            network_name=network_name,
            explicit_manifest=args.network_manifest,
            configs_dir=args.configs_dir,
        )
        network = read_network_manifest(manifest_path)

    default_mode = str(
        _pick_template_value(
            args.block_policy_mode,
            template.get("block_policy_mode") if template is not None else None,
            network.get("block_policy_mode", "on_demand") if network is not None else "on_demand",
        )
    )
    default_interval = str(
        _pick_template_value(
            args.block_policy_interval,
            template.get("block_policy_interval") if template is not None else None,
            (network.get("block_policy_interval", "0s") if network is not None else "0s"),
        )
    )
    source = _block_policy_source(args=args, template=template, network=network)

    if (
        args.block_policy_interval is not None
        and args.block_policy_mode is None
        and default_mode == "on_demand"
        and args.block_policy_interval != "0s"
    ):
        raise ValueError(
            "--block-policy-interval has no effect with on_demand; pass "
            "--block-policy-mode idle_interval or --block-policy-mode periodic"
        )
    if (
        args.block_policy_mode == "on_demand"
        and args.block_policy_interval is not None
        and args.block_policy_interval != "0s"
    ):
        raise ValueError("--block-policy-interval must be 0s with --block-policy-mode on_demand")

    if prompt and args.block_policy_mode is None and args.block_policy_interval is None:
        print(
            "Effective block production policy: "
            f"{_describe_block_policy(default_mode, default_interval)}",
            file=sys.stderr,
        )
        if _prompt_bool("Customize block production policy", default=False):
            chosen_mode = _prompt_choice(
                "Block production policy",
                BLOCK_POLICY_CHOICES,
                default=default_mode,
            )
            if chosen_mode == "on_demand":
                chosen_interval = "0s"
            else:
                chosen_interval = _prompt_text(
                    "Empty-block interval",
                    default=_prompt_interval_default(default_interval),
                )
            chosen_mode, chosen_interval = _validate_block_policy_pair(
                chosen_mode,
                chosen_interval,
            )
            runtime_args["block_policy_mode"] = chosen_mode
            runtime_args["block_policy_interval"] = chosen_interval
            return chosen_mode, chosen_interval, "wizard"

    if prompt and args.block_policy_mode in {"idle_interval", "periodic"}:
        if args.block_policy_interval is None and default_interval == "0s":
            default_interval = _prompt_text("Empty-block interval", default="1s")
            runtime_args["block_policy_interval"] = default_interval
            source = "wizard"

    default_mode, default_interval = _validate_block_policy_pair(
        default_mode,
        default_interval,
    )
    if default_mode == "on_demand" and (
        args.block_policy_mode is not None or args.block_policy_interval is not None
    ):
        runtime_args["block_policy_interval"] = "0s"

    return default_mode, default_interval, source


def _prompt_line(prompt: str) -> str:
    print(prompt, end="", file=sys.stderr, flush=True)
    return sys.stdin.readline().strip()


def _prompt_text(label: str, *, default: str) -> str:
    value = _prompt_line(f"{label} [{default}]: ")
    return value or default


def _prompt_choice(label: str, choices: list[tuple[str, str]], *, default: str) -> str:
    print(label, file=sys.stderr)
    for index, (value, description) in enumerate(choices, start=1):
        marker = " default" if value == default else ""
        print(f"  {index}. {value} - {description}{marker}", file=sys.stderr)

    valid_values = {value for value, _ in choices}
    while True:
        raw = _prompt_line(f"Choose [{default}]: ")
        if not raw:
            return default
        if raw.isdigit():
            index = int(raw)
            if 1 <= index <= len(choices):
                return choices[index - 1][0]
        if raw in valid_values:
            return raw
        print(f"Choose one of: {', '.join(sorted(valid_values))}", file=sys.stderr)


def _prompt_bool(label: str, *, default: bool) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        raw = _prompt_line(f"{label} [{suffix}]: ").lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("Answer yes or no.", file=sys.stderr)


def _should_prompt(args: argparse.Namespace) -> bool:
    if args.plan or args.yes:
        return False
    return sys.stdin.isatty()


def _require_confirmation_path(args: argparse.Namespace) -> None:
    if args.plan or args.yes or sys.stdin.isatty():
        return
    raise ValueError("xian setup node requires --yes or --plan in non-interactive mode")


def _resolve_key_mode(args: argparse.Namespace, *, prompt: bool) -> str:
    if args.key_mode is not None:
        key_mode = args.key_mode
    elif args.validator_key_ref is not None:
        key_mode = "existing"
    elif prompt:
        key_mode = _prompt_choice(
            "Validator key",
            [
                ("generate", "generate new validator key material"),
                ("existing", "use an existing validator key file"),
            ],
            default="generate",
        )
    else:
        key_mode = "generate"

    if key_mode == "generate" and args.validator_key_ref is not None:
        raise ValueError("pass either --validator-key-ref or --key-mode generate, not both")
    return key_mode


def _resolve_plan(args: argparse.Namespace) -> SetupNodePlan:
    _require_confirmation_path(args)
    prompt = _should_prompt(args)
    base_dir = args.base_dir.resolve()

    mode = args.mode
    if mode is None:
        mode = (
            _prompt_choice(
                "Setup path",
                [
                    ("join", "join an existing manifest-backed network"),
                    ("local", "create a fresh local single-node network"),
                ],
                default="join",
            )
            if prompt
            else "join"
        )

    if mode == "local" and args.network_manifest is not None:
        raise ValueError("--network-manifest is only valid when --mode join")
    if mode == "local" and (args.restore_snapshot or args.bootstrap_mode == "snapshot"):
        raise ValueError("snapshot restore is only valid when --mode join")
    if args.restore_snapshot and args.bootstrap_mode == "genesis":
        raise ValueError("pass either --bootstrap-mode genesis or --restore-snapshot, not both")

    default_network = "local-dev" if mode == "local" else "testnet"
    network = args.network or (
        _prompt_text("Network name", default=default_network) if prompt else default_network
    )
    default_name = "validator-1"
    name = args.name or (
        _prompt_text("Node name", default=default_name) if prompt else default_name
    )
    chain_id = args.chain_id
    if mode == "local" and chain_id is None:
        default_chain_id = _default_chain_id(network)
        chain_id = (
            _prompt_text("Chain ID", default=default_chain_id) if prompt else default_chain_id
        )

    default_preset = "basic" if mode == "local" else "indexed"
    preset = args.preset or (
        _prompt_choice(
            "Runtime preset",
            [
                ("basic", "single node with minimal sidecars"),
                ("indexed", "single node with BDS, dashboard, and monitoring"),
            ],
            default=default_preset,
        )
        if prompt
        else default_preset
    )
    template = args.template or _template_for_preset(preset)
    key_mode = _resolve_key_mode(args, prompt=prompt)

    validator_key_ref = args.validator_key_ref
    if key_mode == "existing" and validator_key_ref is None:
        if prompt:
            validator_key_ref = Path(
                _prompt_text(
                    "Validator key file",
                    default=f"./keys/{name}/validator_key_info.json",
                )
            )
        else:
            raise ValueError("--key-mode existing requires --validator-key-ref")

    restore_snapshot = False
    if mode == "join":
        if args.restore_snapshot:
            restore_snapshot = True
        elif args.bootstrap_mode is not None:
            restore_snapshot = args.bootstrap_mode == "snapshot"
        elif prompt:
            restore_snapshot = _prompt_bool("Restore configured snapshot after init", default=False)

    if args.start is None:
        start = _prompt_bool("Start the node after setup", default=True) if prompt else False
    else:
        start = bool(args.start)

    runtime_args = {
        key: value
        for key in RUNTIME_ARG_DEFAULTS
        if (value := getattr(args, key, None)) is not None
    }
    block_policy_mode, block_policy_interval, block_policy_source = _resolve_block_policy(
        args=args,
        base_dir=base_dir,
        mode=mode,
        network_name=network,
        template_name=template,
        prompt=prompt,
        runtime_args=runtime_args,
    )

    return SetupNodePlan(
        mode=mode,
        name=name,
        network=network,
        chain_id=chain_id,
        template=template,
        key_mode=key_mode,
        validator_key_ref=validator_key_ref,
        validator_key_dir=args.validator_key_dir,
        restore_snapshot=restore_snapshot,
        start=start,
        base_dir=base_dir,
        configs_dir=args.configs_dir,
        stack_dir=args.stack_dir,
        home=args.home,
        network_manifest=args.network_manifest,
        snapshot_url=args.snapshot_url,
        snapshot_signing_keys=args.snapshot_signing_key or [],
        seed=args.seed or [],
        genesis_source=args.genesis_source,
        genesis_bundle=args.genesis_bundle,
        node_image_mode=args.node_image_mode,
        node_integrated_image=args.node_integrated_image,
        node_split_image=args.node_split_image,
        moniker=args.moniker,
        force=bool(args.force),
        rpc_url=args.rpc_url,
        rpc_timeout_seconds=args.rpc_timeout_seconds,
        skip_disk_check=bool(args.skip_disk_check),
        block_policy_mode=block_policy_mode,
        block_policy_interval=block_policy_interval,
        block_policy_source=block_policy_source,
        runtime_args=runtime_args,
    )


def _network_command(plan: SetupNodePlan) -> list[str]:
    if plan.mode == "local":
        command = [
            "xian",
            "network",
            "create",
            plan.network,
            "--chain-id",
            str(plan.chain_id),
            "--template",
            plan.template,
            "--bootstrap-node",
            plan.name,
            "--init-node",
        ]
        if plan.key_mode == "generate":
            command.append("--generate-validator-key")
            _add_path_option(command, "--validator-key-dir", plan.validator_key_dir)
        else:
            _add_path_option(command, "--validator-key-ref", plan.validator_key_ref)
        _add_value_option(command, "--genesis-source", plan.genesis_source)
        _add_value_option(command, "--genesis-bundle", plan.genesis_bundle)
    else:
        command = [
            "xian",
            "network",
            "join",
            plan.name,
            "--network",
            plan.network,
            "--template",
            plan.template,
            "--init-node",
        ]
        if plan.key_mode == "generate":
            command.append("--generate-validator-key")
            _add_path_option(command, "--validator-key-dir", plan.validator_key_dir)
        else:
            _add_path_option(command, "--validator-key-ref", plan.validator_key_ref)
        _add_path_option(command, "--network-manifest", plan.network_manifest)
        if plan.restore_snapshot:
            command.append("--restore-snapshot")
        _add_value_option(command, "--genesis-source", plan.genesis_source)

    command.extend(["--base-dir", str(plan.base_dir)])
    _add_path_option(command, "--configs-dir", plan.configs_dir)
    _add_path_option(command, "--stack-dir", plan.stack_dir)
    _add_path_option(command, "--home", plan.home)
    _add_value_option(command, "--moniker", plan.moniker)
    _add_value_option(command, "--snapshot-url", plan.snapshot_url)
    _add_repeated_option(command, "--snapshot-signing-key", plan.snapshot_signing_keys)
    _add_repeated_option(command, "--seed", plan.seed)
    _add_value_option(command, "--node-image-mode", plan.node_image_mode)
    _add_value_option(command, "--node-integrated-image", plan.node_integrated_image)
    _add_value_option(command, "--node-split-image", plan.node_split_image)
    _add_runtime_options(command, plan.runtime_args)
    if plan.force:
        command.append("--force")
    return command


def _start_command(plan: SetupNodePlan) -> list[str]:
    command = ["xian", "node", "start", plan.name, "--base-dir", str(plan.base_dir)]
    _add_path_option(command, "--configs-dir", plan.configs_dir)
    _add_path_option(command, "--stack-dir", plan.stack_dir)
    _add_path_option(command, "--network", plan.network_manifest)
    _add_value_option(command, "--rpc-timeout-seconds", plan.rpc_timeout_seconds)
    return command


def _health_command(plan: SetupNodePlan) -> list[str]:
    command = ["xian", "node", "health", plan.name, "--base-dir", str(plan.base_dir)]
    _add_path_option(command, "--configs-dir", plan.configs_dir)
    _add_path_option(command, "--stack-dir", plan.stack_dir)
    _add_path_option(command, "--home", plan.home)
    _add_path_option(command, "--network", plan.network_manifest)
    _add_value_option(command, "--rpc-url", plan.rpc_url)
    if plan.skip_disk_check:
        command.append("--skip-disk-check")
    return command


def _status_command(plan: SetupNodePlan) -> list[str]:
    command = ["xian", "node", "status", plan.name, "--base-dir", str(plan.base_dir)]
    _add_path_option(command, "--configs-dir", plan.configs_dir)
    _add_path_option(command, "--stack-dir", plan.stack_dir)
    _add_path_option(command, "--home", plan.home)
    _add_path_option(command, "--network", plan.network_manifest)
    _add_value_option(command, "--rpc-url", plan.rpc_url)
    return command


def _plan_steps(plan: SetupNodePlan) -> list[dict[str, object]]:
    steps = [
        {
            "name": "create-local-network" if plan.mode == "local" else "join-network",
            "command": _network_command(plan),
        }
    ]
    if plan.start:
        steps.extend(
            [
                {"name": "start-node", "command": _start_command(plan)},
                {"name": "health-check", "command": _health_command(plan)},
            ]
        )
    return steps


def _plan_payload(plan: SetupNodePlan, *, dry_run: bool = False) -> dict[str, object]:
    writes: list[str] = [str(plan.profile_path)]
    if plan.mode == "local":
        writes.append(str(plan.local_manifest_path))
    if plan.key_dir is not None:
        writes.append(str(plan.key_dir))
    if plan.home is not None:
        writes.append(str(plan.home))

    return {
        "dry_run": dry_run,
        "mode": plan.mode,
        "name": plan.name,
        "network": plan.network,
        "chain_id": plan.chain_id,
        "template": plan.template,
        "key_mode": plan.key_mode,
        "validator_key_ref": _path_str(plan.validator_key_ref),
        "validator_key_dir": _path_str(plan.validator_key_dir),
        "restore_snapshot": plan.restore_snapshot,
        "start": plan.start,
        "base_dir": str(plan.base_dir),
        "configs_dir": _path_str(plan.configs_dir),
        "stack_dir": _path_str(plan.stack_dir),
        "home": _path_str(plan.home),
        "network_manifest": _path_str(plan.network_manifest),
        "snapshot_url": plan.snapshot_url,
        "block_policy": {
            "mode": plan.block_policy_mode,
            "interval": plan.block_policy_interval,
            "source": plan.block_policy_source,
        },
        "runtime_args": plan.runtime_args,
        "writes": writes,
        "steps": _plan_steps(plan),
    }


def _format_plan(plan: SetupNodePlan) -> str:
    payload = _plan_payload(plan, dry_run=True)
    lines = ["Setup plan:"]
    lines.append(
        "Block policy: "
        f"{_describe_block_policy(plan.block_policy_mode, plan.block_policy_interval)} "
        f"({plan.block_policy_source})"
    )
    for index, step in enumerate(payload["steps"], start=1):
        lines.append(f"{index}. {step['name']}: {' '.join(step['command'])}")
    if payload["writes"]:
        lines.append("Writes:")
        for item in payload["writes"]:
            lines.append(f"- {item}")
    return "\n".join(lines)


def _namespace(**values: Any) -> argparse.Namespace:
    payload = dict(RUNTIME_ARG_DEFAULTS)
    payload.update(values)
    return argparse.Namespace(**payload)


def _network_args(plan: SetupNodePlan) -> argparse.Namespace:
    common = {
        "base_dir": plan.base_dir,
        "template": plan.template,
        "configs_dir": plan.configs_dir,
        "moniker": plan.moniker,
        "validator_key_ref": plan.validator_key_ref,
        "generate_validator_key": plan.key_mode == "generate",
        "validator_key_dir": plan.validator_key_dir,
        "node_image_mode": plan.node_image_mode,
        "node_integrated_image": plan.node_integrated_image,
        "node_split_image": plan.node_split_image,
        "stack_dir": plan.stack_dir,
        "seed": plan.seed,
        "genesis_source": plan.genesis_source,
        "snapshot_url": plan.snapshot_url,
        "snapshot_signing_key": plan.snapshot_signing_keys,
        "home": plan.home,
        "force": plan.force,
        "dry_run": False,
        **plan.runtime_args,
    }
    if plan.mode == "local":
        return _namespace(
            **common,
            name=plan.network,
            chain_id=plan.chain_id,
            genesis_bundle=plan.genesis_bundle,
            founder_private_key=None,
            validator_power=10,
            bootstrap_node=plan.name,
            validator=None,
            node_output=None,
            init_node=True,
            output=None,
        )
    return _namespace(
        **common,
        name=plan.name,
        network=plan.network,
        network_manifest=plan.network_manifest,
        init_node=True,
        restore_snapshot=plan.restore_snapshot,
        output=None,
    )


def _start_args(plan: SetupNodePlan) -> argparse.Namespace:
    return argparse.Namespace(
        name=plan.name,
        base_dir=plan.base_dir,
        profile=None,
        network=plan.network_manifest,
        stack_dir=plan.stack_dir,
        configs_dir=plan.configs_dir,
        skip_health_check=False,
        rpc_timeout_seconds=plan.rpc_timeout_seconds,
    )


def _health_args(plan: SetupNodePlan) -> argparse.Namespace:
    return argparse.Namespace(
        name=plan.name,
        base_dir=plan.base_dir,
        profile=None,
        network=plan.network_manifest,
        stack_dir=plan.stack_dir,
        configs_dir=plan.configs_dir,
        home=plan.home,
        rpc_url=plan.rpc_url,
        skip_disk_check=plan.skip_disk_check,
    )


def _run_json_handler(handler, args: argparse.Namespace) -> dict[str, object]:
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = handler(args)
    if exit_code != 0:
        raise RuntimeError(f"handler exited with status {exit_code}")
    output = stdout.getvalue().strip()
    if not output:
        return {}
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return {"output": output}


def _handle_setup_node(args: argparse.Namespace) -> int:
    plan = _resolve_plan(args)
    if args.plan:
        print(json.dumps(_plan_payload(plan, dry_run=True), indent=2))
        return 0

    if not args.yes:
        print(_format_plan(plan), file=sys.stderr)
        if not _prompt_bool("Apply this setup plan", default=False):
            print(json.dumps({"cancelled": True, "plan": _plan_payload(plan)}, indent=2))
            return 0

    network_result = _run_json_handler(
        _handle_network_create if plan.mode == "local" else _handle_network_join,
        _network_args(plan),
    )
    result: dict[str, object] = {
        "setup": "node",
        "mode": plan.mode,
        "plan": _plan_payload(plan),
        "network": network_result,
        "started": False,
    }

    if plan.start:
        result["start"] = _run_json_handler(_handle_node_start, _start_args(plan))
        result["health"] = _run_json_handler(_handle_node_health, _health_args(plan))
        result["started"] = True
    else:
        result["next_steps"] = [
            _start_command(plan),
            _status_command(plan),
            _health_command(plan),
        ]

    print(json.dumps(result, indent=2))
    return 0
