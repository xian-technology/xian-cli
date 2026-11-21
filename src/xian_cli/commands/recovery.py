from __future__ import annotations

import argparse
import json
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from xian_cli.abci_bridge import get_node_admin_module
from xian_cli.commands.common import _stack_runtime_profile_kwargs
from xian_cli.commands.node_context import (
    _load_profile_and_network,
    _require_stopped_node,
    _resolve_effective_snapshot_signing_keys,
    _resolve_home,
    _resolve_stack_dir_from_profile,
    _snapshot_store_inspector,
    _validate_rendered_cometbft_config,
)
from xian_cli.models import read_recovery_plan
from xian_cli.runtime import (
    fetch_json,
    resolve_stack_dir,
    start_xian_stack_node,
    stop_xian_stack_node,
)
from xian_cli.secret_files import ensure_secret_dir


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
    ensure_secret_dir(backup_dir)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    prefix = f"{Path(plan_name).name}-{Path(node_name).name}-{timestamp}-"
    with tempfile.NamedTemporaryFile(
        dir=backup_dir, prefix=prefix, suffix=".tar.gz", delete=False
    ) as output:
        archive_path = Path(output.name)
        try:
            with tarfile.open(fileobj=output, mode="w:gz") as archive:
                archive.add(home, arcname=home.name)
        except BaseException:
            archive_path.unlink()
            raise
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
        ignore_legacy_state_sync=True,
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


def _backup_before_recovery(args, *, base_dir: Path, home: Path, plan_name: str, node_name: str):
    if args.skip_backup:
        return None
    backup_dir = args.backup_dir
    if backup_dir is None:
        backup_dir = base_dir / "recovery-backups"
    if not backup_dir.is_absolute():
        backup_dir = (base_dir / backup_dir).resolve()
    return _build_recovery_backup(
        home=home, backup_dir=backup_dir, plan_name=plan_name, node_name=node_name
    )


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
        ignore_legacy_state_sync=not args.start_node,
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

    if args.start_node:
        _validate_rendered_cometbft_config(home / "config" / "config.toml")
    stack_dir = resolve_stack_dir(base_dir, explicit=stack_dir)
    stop_result = None
    if not args.skip_stop:
        stop_result = stop_xian_stack_node(
            stack_dir=stack_dir,
            cometbft_home=home,
            **_stack_runtime_profile_kwargs(profile, network),
        )
    _require_stopped_node(stack_dir=stack_dir, home=home, profile=profile, network=network)
    backup_archive = _backup_before_recovery(
        args, base_dir=base_dir, home=home, plan_name=plan["name"], node_name=profile["name"]
    )

    node_admin = get_node_admin_module()
    snapshot_archive_name = node_admin.apply_snapshot_archive(
        plan["artifact"]["uri"],
        home,
        expected_sha256=plan["artifact"].get("sha256"),
        trusted_manifest_public_keys=_resolve_effective_snapshot_signing_keys(
            profile=profile, network=network
        ),
        expected_chain_id=network.get("chain_id"),
        native_store_inspector=_snapshot_store_inspector(
            stack_dir=stack_dir, home=home, profile=profile, network=network
        ),
    )

    start_result = None
    if args.start_node:
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
