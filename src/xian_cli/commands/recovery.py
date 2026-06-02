from __future__ import annotations

import argparse
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from xian_cli.abci_bridge import get_node_admin_module
from xian_cli.commands.common import _stack_runtime_profile_kwargs
from xian_cli.commands.node_context import (
    _load_profile_and_network,
    _resolve_home,
    _resolve_stack_dir_from_profile,
)
from xian_cli.models import read_recovery_plan
from xian_cli.runtime import (
    fetch_json,
    start_xian_stack_node,
    stop_xian_stack_node,
)


def _resolve_recovery_rpc_status(
    *,
    rpc_url: str,
) -> dict | None:
    try:
        return fetch_json(rpc_url, timeout=5.0)
    except Exception:
        return None


def _build_recovery_backup(
    *,
    home: Path,
    backup_dir: Path,
    plan_name: str,
    node_name: str,
) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    archive_base = backup_dir / f"{plan_name}-{node_name}-{timestamp}"
    archive_path = Path(
        shutil.make_archive(
            str(archive_base),
            "gztar",
            root_dir=home.parent,
            base_dir=home.name,
        )
    )
    return archive_path


def _validate_recovery_context(
    *,
    plan: dict,
    profile: dict,
    network: dict,
    home: Path,
    rpc_url: str | None,
) -> dict:
    config_path = home / "config" / "config.toml"
    if not config_path.exists():
        raise FileNotFoundError(
            f"{config_path} does not exist; run `xian node init {profile['name']}` first"
        )

    if network["chain_id"] != plan["chain_id"]:
        raise ValueError("recovery plan chain_id does not match the node network manifest")

    rpc_status = None
    rpc_checked = False
    if rpc_url:
        rpc_status = _resolve_recovery_rpc_status(rpc_url=rpc_url)
        rpc_checked = rpc_status is not None
        if rpc_status is not None:
            network_id = rpc_status.get("result", {}).get("node_info", {}).get("network")
            if network_id and network_id != plan["chain_id"]:
                raise ValueError("live RPC chain_id does not match the recovery plan")
            latest_height = (
                rpc_status.get("result", {}).get("sync_info", {}).get("latest_block_height")
            )
            if latest_height is not None:
                try:
                    latest_height_int = int(latest_height)
                except (TypeError, ValueError):
                    latest_height_int = None
                if latest_height_int is not None and latest_height_int < plan["target_height"]:
                    raise ValueError("live RPC height is below the recovery target height")

    return {
        "home": str(home),
        "config_path": str(config_path),
        "rpc_checked": rpc_checked,
        "rpc_status": rpc_status,
        "requires_manual_hash_confirmation": True,
    }


def _prepare_recovery_payload(
    *,
    plan: dict,
    profile: dict,
    network: dict,
    home: Path,
    validation: dict,
) -> dict:
    return {
        "plan": plan,
        "node": {
            "name": profile["name"],
            "network": profile["network"],
            "home": str(home),
        },
        "validation": validation,
        "manual_confirmation": {
            "trusted_block_hash": plan["trusted_block_hash"],
            "trusted_app_hash": plan["trusted_app_hash"],
        },
    }


def _handle_recovery_validate(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    plan_path = args.plan.resolve()
    plan = read_recovery_plan(plan_path)
    profile_path, profile, _, network = _load_profile_and_network(
        base_dir=base_dir,
        name=args.name,
        profile_arg=args.profile,
        network_arg=args.network,
        configs_dir=args.configs_dir,
    )
    stack_dir = _resolve_stack_dir_from_profile(
        base_dir=base_dir,
        profile=profile,
        explicit_stack_dir=args.stack_dir,
    )
    home = _resolve_home(
        base_dir=base_dir,
        profile=profile,
        profile_path=profile_path,
        stack_dir=stack_dir,
        explicit_home=args.home,
    )
    validation = _validate_recovery_context(
        plan=plan,
        profile=profile,
        network=network,
        home=home,
        rpc_url=args.rpc_url,
    )
    payload = _prepare_recovery_payload(
        plan=plan,
        profile=profile,
        network=network,
        home=home,
        validation=validation,
    )
    payload["dry_run"] = True
    print(json.dumps(payload, indent=2))
    return 0


def _handle_recovery_apply(args: argparse.Namespace) -> int:
    if not args.yes:
        raise ValueError("recovery apply is destructive; pass --yes after reviewing the plan")

    base_dir = args.base_dir.resolve()
    plan_path = args.plan.resolve()
    plan = read_recovery_plan(plan_path)
    profile_path, profile, _, network = _load_profile_and_network(
        base_dir=base_dir,
        name=args.name,
        profile_arg=args.profile,
        network_arg=args.network,
        configs_dir=args.configs_dir,
    )
    stack_dir = _resolve_stack_dir_from_profile(
        base_dir=base_dir,
        profile=profile,
        explicit_stack_dir=args.stack_dir,
    )
    home = _resolve_home(
        base_dir=base_dir,
        profile=profile,
        profile_path=profile_path,
        stack_dir=stack_dir,
        explicit_home=args.home,
    )
    validation = _validate_recovery_context(
        plan=plan,
        profile=profile,
        network=network,
        home=home,
        rpc_url=args.rpc_url,
    )
    payload = _prepare_recovery_payload(
        plan=plan,
        profile=profile,
        network=network,
        home=home,
        validation=validation,
    )

    if args.dry_run:
        payload["dry_run"] = True
        print(json.dumps(payload, indent=2))
        return 0

    stop_result = None
    if not args.skip_stop:
        if stack_dir is None:
            raise ValueError(
                "recovery apply requires a resolved xian-stack directory unless --skip-stop is used"
            )
        stop_result = stop_xian_stack_node(
            stack_dir=stack_dir,
            cometbft_home=home,
            **_stack_runtime_profile_kwargs(profile, network),
        )

    backup_archive = None
    if not args.skip_backup:
        backup_dir = args.backup_dir
        if backup_dir is None:
            backup_dir = base_dir / "recovery-backups"
        if not backup_dir.is_absolute():
            backup_dir = (base_dir / backup_dir).resolve()
        backup_archive = _build_recovery_backup(
            home=home,
            backup_dir=backup_dir,
            plan_name=plan["name"],
            node_name=profile["name"],
        )

    node_admin = get_node_admin_module()
    snapshot_archive_name = node_admin.apply_snapshot_archive(
        plan["artifact"]["uri"],
        home,
        expected_sha256=plan["artifact"].get("sha256"),
    )

    start_result = None
    if args.start_node:
        if stack_dir is None:
            raise ValueError("--start-node requires a resolved xian-stack directory")
        start_result = start_xian_stack_node(
            stack_dir=stack_dir,
            cometbft_home=home,
            **_stack_runtime_profile_kwargs(profile, network),
            wait_for_rpc=not args.no_wait,
            rpc_timeout_seconds=args.rpc_timeout_seconds,
        )

    payload.update(
        {
            "dry_run": False,
            "stopped_node": stop_result is not None,
            "stop_result": stop_result,
            "backup_archive": (None if backup_archive is None else str(backup_archive)),
            "snapshot_restore": {
                "artifact_kind": plan["artifact"]["kind"],
                "artifact_uri": plan["artifact"]["uri"],
                "snapshot_archive_name": snapshot_archive_name,
            },
            "started_node": start_result is not None,
            "start_result": start_result,
        }
    )
    print(json.dumps(payload, indent=2))
    return 0
