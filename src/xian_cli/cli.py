from __future__ import annotations

import argparse
import base64
import json
import shutil
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from xian_cli.abci_bridge import (
    get_genesis_builder_module,
    get_node_admin_module,
    get_node_setup_module,
)
from xian_cli.config_repo import (
    list_network_template_paths,
    list_solution_pack_paths,
    resolve_configs_dir,
    resolve_network_manifest_path,
    resolve_network_template_path,
    resolve_solution_pack_path,
)
from xian_cli.models import (
    SUPPORTED_NODE_IMAGE_MODES,
    SUPPORTED_RUNTIME_BACKENDS,
    NetworkManifest,
    NodeProfile,
    read_json,
    read_network_manifest,
    read_network_template,
    read_node_profile,
    read_recovery_plan,
    read_solution_pack,
    write_json,
)
from xian_cli.parser import build_parser as _build_parser
from xian_cli.runtime import (
    default_home_for_backend,
    fetch_json,
    get_xian_stack_node_endpoints,
    get_xian_stack_node_health,
    get_xian_stack_node_status,
    resolve_stack_dir,
    start_xian_stack_node,
    stop_xian_stack_node,
)


def _write_validator_material_files(
    *,
    out_dir: Path,
    private_key: str | None = None,
    force: bool = False,
) -> Path:
    node_setup = get_node_setup_module()
    payload = node_setup.generate_validator_material(private_key)
    write_json(
        out_dir / "priv_validator_key.json",
        payload["priv_validator_key"],
        force=force,
    )
    metadata_path = out_dir / "validator_key_info.json"
    write_json(metadata_path, payload, force=force)
    return metadata_path


def _stringify_path_for_profile(path: Path, *, base_dir: Path) -> str:
    resolved_path = path
    if not resolved_path.is_absolute():
        resolved_path = (base_dir / resolved_path).resolve()
    else:
        resolved_path = resolved_path.resolve()
    try:
        return str(resolved_path.relative_to(base_dir.resolve()))
    except ValueError:
        return str(resolved_path)


def _default_network_manifest_path(base_dir: Path, network_name: str) -> Path:
    return base_dir / "networks" / network_name / "manifest.json"


def _resolve_output_path(
    *,
    base_dir: Path,
    explicit_output: Path | None,
    default_path: Path,
) -> Path:
    target = explicit_output or default_path
    if not target.is_absolute():
        target = (base_dir / target).resolve()
    return target


def _pick_template_value(
    explicit,
    template_value,
    default,
):
    if explicit is not None:
        return explicit
    if template_value is not None:
        return template_value
    return default


def _default_intentkit_network_id(network_name: str | None) -> str:
    normalized = (network_name or "").strip().lower()
    if normalized in {"mainnet", "xian-mainnet"}:
        return "xian-mainnet"
    if normalized in {"testnet", "xian-testnet"}:
        return "xian-testnet"
    if normalized in {"devnet", "xian-devnet"}:
        return "xian-devnet"
    return "xian-localnet"


def _effective_node_image_config(
    profile: dict[str, object],
    network: dict[str, object] | None = None,
) -> tuple[str, str | None, str | None]:
    network_mode = (
        None
        if network is None
        else network.get("node_image_mode") or "local_build"
    )
    return _resolve_node_image_settings(
        node_image_mode=str(
            profile.get("node_image_mode") or network_mode or "local_build"
        ),
        node_integrated_image=(
            profile.get("node_integrated_image")
            or (
                None
                if network is None
                else network.get("node_integrated_image")
            )
        ),
        node_split_image=(
            profile.get("node_split_image")
            or (None if network is None else network.get("node_split_image"))
        ),
    )


def _effective_node_release_manifest(
    profile: dict[str, object],
    network: dict[str, object] | None = None,
) -> dict[str, object] | None:
    node_image_mode, _, _ = _effective_node_image_config(profile, network)
    if node_image_mode != "registry":
        return None
    profile_manifest = profile.get("node_release_manifest")
    if isinstance(profile_manifest, dict):
        return profile_manifest
    network_manifest = (
        None if network is None else network.get("node_release_manifest")
    )
    return network_manifest if isinstance(network_manifest, dict) else None


def _stack_runtime_profile_kwargs(
    profile: dict[str, object],
    network: dict[str, object] | None = None,
) -> dict[str, object]:
    node_image_mode, node_integrated_image, node_split_image = (
        _effective_node_image_config(profile, network)
    )
    return {
        "node_image_mode": node_image_mode,
        "node_integrated_image": node_integrated_image,
        "node_split_image": node_split_image,
        "service_node": bool(profile.get("service_node")),
        "dashboard_enabled": bool(profile.get("dashboard_enabled")),
        "monitoring_enabled": bool(profile.get("monitoring_enabled")),
        "dashboard_host": str(profile.get("dashboard_host", "127.0.0.1")),
        "dashboard_port": int(profile.get("dashboard_port", 8080)),
        "intentkit_enabled": bool(profile.get("intentkit_enabled")),
        "intentkit_network_id": str(
            profile.get("intentkit_network_id")
            or _default_intentkit_network_id(profile.get("network"))
        ),
        "intentkit_host": str(profile.get("intentkit_host", "127.0.0.1")),
        "intentkit_port": int(profile.get("intentkit_port", 38000)),
        "intentkit_api_port": int(profile.get("intentkit_api_port", 38080)),
        "shielded_relayer_enabled": bool(
            profile.get("shielded_relayer_enabled")
        ),
        "shielded_relayer_host": str(
            profile.get("shielded_relayer_host", "127.0.0.1")
        ),
        "shielded_relayer_port": int(
            profile.get("shielded_relayer_port", 38180)
        ),
    }


def _network_shielded_relayer_endpoints(
    network: dict[str, object] | None,
) -> dict[str, object]:
    relayers_raw = []
    if isinstance(network, dict):
        list_value = network.get("shielded_relayers")
        if isinstance(list_value, list):
            relayers_raw = [
                item for item in list_value if isinstance(item, dict)
            ]
        elif isinstance(network.get("shielded_relayer"), dict):
            relayers_raw = [network["shielded_relayer"]]
    if not relayers_raw:
        return {}
    relayers = sorted(
        relayers_raw,
        key=lambda item: (
            int(item.get("priority", 100)),
            str(item.get("id", "")),
            str(item.get("base_url", "")),
        ),
    )
    catalog: list[dict[str, object]] = []
    for relayer in relayers:
        base_url = relayer.get("base_url")
        if not isinstance(base_url, str) or not base_url:
            continue
        normalized_base = base_url.rstrip("/")
        catalog.append(
            {
                "id": relayer.get("id"),
                "base_url": normalized_base,
                "auth_scheme": relayer.get("auth_scheme"),
                "public_info": relayer.get("public_info"),
                "public_quote": relayer.get("public_quote"),
                "public_job_lookup": relayer.get("public_job_lookup"),
                "priority": relayer.get("priority", 100),
                "submission_kinds": relayer.get("submission_kinds", []),
                "endpoints": {
                    "shielded_relayer": normalized_base,
                    "shielded_relayer_info": f"{normalized_base}/v1/info",
                    "shielded_relayer_metrics": f"{normalized_base}/metrics",
                    "shielded_relayer_quote": f"{normalized_base}/v1/quote",
                    "shielded_relayer_jobs": f"{normalized_base}/v1/jobs",
                },
            }
        )
    if not catalog:
        return {}
    primary = catalog[0]
    endpoints = dict(primary["endpoints"])
    endpoints["shielded_relayer_primary_id"] = str(primary.get("id") or "")
    endpoints["shielded_relayers"] = catalog
    return endpoints


def _resolve_node_image_settings(
    *,
    node_image_mode: str,
    node_integrated_image: str | None,
    node_split_image: str | None,
) -> tuple[str, str | None, str | None]:
    if node_image_mode not in SUPPORTED_NODE_IMAGE_MODES:
        raise ValueError(
            "node_image_mode must be one of "
            f"{sorted(SUPPORTED_NODE_IMAGE_MODES)}"
        )
    if node_image_mode == "registry" and (
        not node_integrated_image or not node_split_image
    ):
        raise ValueError(
            "registry node image mode requires both "
            "--node-integrated-image and --node-split-image"
        )
    return node_image_mode, node_integrated_image, node_split_image


def _validate_non_negative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _validate_positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _load_template(
    *,
    base_dir: Path,
    template_name: str | None,
    configs_dir: Path | None,
) -> dict | None:
    if template_name is None:
        return None
    template_path = resolve_network_template_path(
        base_dir=base_dir,
        template_name=template_name,
        configs_dir=configs_dir,
    )
    return read_network_template(template_path)


def _handle_network_template_list(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    templates = [
        read_network_template(path)
        for path in list_network_template_paths(
            base_dir=base_dir,
            configs_dir=args.configs_dir,
        )
    ]
    print(json.dumps(templates, indent=2))
    return 0


def _handle_network_template_show(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    template = _load_template(
        base_dir=base_dir,
        template_name=args.name,
        configs_dir=args.configs_dir,
    )
    print(json.dumps(template, indent=2))
    return 0


def _load_solution_pack(
    *,
    base_dir: Path,
    pack_name: str,
    configs_dir: Path | None,
) -> dict:
    pack_path = resolve_solution_pack_path(
        base_dir=base_dir,
        pack_name=pack_name,
        configs_dir=configs_dir,
    )
    return read_solution_pack(pack_path)


def _handle_solution_pack_list(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    packs = [
        read_solution_pack(path)
        for path in list_solution_pack_paths(
            base_dir=base_dir,
            configs_dir=args.configs_dir,
        )
    ]
    summaries = [
        {
            "name": pack["name"],
            "display_name": pack["display_name"],
            "description": pack["description"],
            "recommended_local_template": pack["recommended_local_template"],
            "recommended_remote_template": pack["recommended_remote_template"],
            "docs_path": pack["docs_path"],
            "example_dir": pack["example_dir"],
        }
        for pack in packs
    ]
    print(json.dumps(summaries, indent=2))
    return 0


def _handle_solution_pack_show(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    pack = _load_solution_pack(
        base_dir=base_dir,
        pack_name=args.name,
        configs_dir=args.configs_dir,
    )
    print(json.dumps(pack, indent=2))
    return 0


def _handle_solution_pack_starter(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    pack = _load_solution_pack(
        base_dir=base_dir,
        pack_name=args.name,
        configs_dir=args.configs_dir,
    )
    flow = next(
        (item for item in pack["starter_flows"] if item["name"] == args.flow),
        None,
    )
    if flow is None:
        available = sorted(item["name"] for item in pack["starter_flows"])
        raise ValueError(
            "solution pack flow "
            f"'{args.flow}' not found; available: {available}"
        )

    starter = {
        "name": pack["name"],
        "display_name": pack["display_name"],
        "description": pack["description"],
        "use_case": pack["use_case"],
        "docs_path": pack["docs_path"],
        "example_dir": pack["example_dir"],
        "contract_paths": pack["contract_paths"],
        "flow": flow,
    }
    print(json.dumps(starter, indent=2))
    return 0


def _handle_keys_validator_generate(args: argparse.Namespace) -> int:
    if args.out_dir is not None:
        metadata_path = _write_validator_material_files(
            out_dir=args.out_dir.resolve(),
            private_key=args.private_key,
            force=args.force,
        )
        payload = read_json(metadata_path)
    else:
        payload = get_node_setup_module().generate_validator_material(
            args.private_key
        )

    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def _collect_creation_validator_names(
    args: argparse.Namespace,
    *,
    template: dict | None = None,
) -> tuple[str | None, list[str]]:
    bootstrap_name = args.bootstrap_node
    if bootstrap_name is None and template is not None:
        bootstrap_name = template.get("bootstrap_node_name")
    validator_names: list[str] = []

    if bootstrap_name is not None:
        validator_names.append(bootstrap_name)

    validator_inputs = (
        args.validator
        if args.validator is not None
        else (template.get("additional_validator_names") if template else [])
    )
    for validator_name in validator_inputs or []:
        if validator_name in validator_names:
            raise ValueError(
                "duplicate validator name in network creation: "
                f"{validator_name}"
            )
        validator_names.append(validator_name)

    return bootstrap_name, validator_names


def _collect_creation_validators(
    *,
    args: argparse.Namespace,
    base_dir: Path,
    bootstrap_name: str | None,
    validator_names: list[str],
) -> list[dict[str, object]]:
    validators: list[dict[str, object]] = []
    if not validator_names:
        if args.validator_key_ref is not None:
            raise ValueError("--validator-key-ref requires --bootstrap-node")
        return validators

    if not args.generate_validator_key:
        if len(validator_names) > 1:
            raise ValueError(
                "multi-validator network creation currently requires "
                "--generate-validator-key"
            )
        if args.validator_key_ref is None:
            return validators

    for index, validator_name in enumerate(validator_names):
        validator_key_ref: str | None = None
        validator_key_payload: dict | None = None

        if args.generate_validator_key:
            key_dir = (
                args.validator_key_dir or base_dir / "keys" / validator_name
            )
            if args.validator_key_dir is not None and len(validator_names) > 1:
                key_dir = key_dir / validator_name
            if not key_dir.is_absolute():
                key_dir = (base_dir / key_dir).resolve()
            metadata_path = _write_validator_material_files(
                out_dir=key_dir,
                force=args.force,
            )
            validator_key_ref = _stringify_path_for_profile(
                metadata_path,
                base_dir=base_dir,
            )
            validator_key_payload = read_json(metadata_path)
        elif index == 0 and args.validator_key_ref is not None:
            validator_key_path = _resolve_output_path(
                base_dir=base_dir,
                explicit_output=args.validator_key_ref,
                default_path=args.validator_key_ref,
            )
            validator_key_ref = _stringify_path_for_profile(
                validator_key_path,
                base_dir=base_dir,
            )
            validator_key_payload = read_json(validator_key_path)

        validators.append(
            {
                "name": validator_name,
                "moniker": (
                    args.moniker
                    if validator_name == bootstrap_name
                    and args.moniker is not None
                    else validator_name
                ),
                "validator_key_ref": validator_key_ref,
                "validator_key_payload": validator_key_payload,
                "power": args.validator_power,
                "is_bootstrap": validator_name == bootstrap_name,
            }
        )

    return validators


def _handle_network_create(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    template = _load_template(
        base_dir=base_dir,
        template_name=args.template,
        configs_dir=args.configs_dir,
    )
    target = _resolve_output_path(
        base_dir=base_dir,
        explicit_output=args.output,
        default_path=_default_network_manifest_path(base_dir, args.name),
    )
    network_dir = target.parent

    if args.generate_validator_key and args.validator_key_ref is not None:
        raise ValueError(
            "pass either --validator-key-ref or --generate-validator-key, "
            "not both"
        )
    if args.validator_key_dir is not None and not args.generate_validator_key:
        raise ValueError(
            "--validator-key-dir requires --generate-validator-key"
        )
    if args.init_node and args.bootstrap_node is None:
        if template is None or template.get("bootstrap_node_name") is None:
            raise ValueError("--init-node requires --bootstrap-node")
    bootstrap_name, validator_names = _collect_creation_validator_names(
        args,
        template=template,
    )
    if args.init_node and bootstrap_name is None:
        raise ValueError("--init-node requires --bootstrap-node")
    validators = _collect_creation_validators(
        args=args,
        base_dir=base_dir,
        bootstrap_name=bootstrap_name,
        validator_names=validator_names,
    )

    genesis_source = args.genesis_source
    generated_genesis_path: Path | None = None
    if genesis_source is None:
        if not validators or any(
            validator["validator_key_payload"] is None
            for validator in validators
        ):
            if bootstrap_name is not None or validator_names:
                raise ValueError(
                    "local network creation without --genesis-source "
                    "requires validator key material; pass "
                    "--generate-validator-key or --validator-key-ref"
                )
        else:
            founder_private_key = (
                args.founder_private_key
                or _extract_validator_private_key_hex(
                    validators[0]["validator_key_payload"]
                )
            )
            generated_genesis_path = network_dir / "genesis.json"
            genesis = _build_creation_genesis(
                chain_id=args.chain_id,
                founder_private_key=founder_private_key,
                validators=validators,
                genesis_preset=args.genesis_preset,
            )
            write_json(
                generated_genesis_path,
                genesis,
                force=args.force,
                preserve_runtime_types=True,
            )
            genesis_source = "./genesis.json"
    if bootstrap_name is not None and genesis_source is None and not validators:
        raise ValueError(
            "--bootstrap-node requires either --genesis-source or local "
            "genesis generation via validator key material"
        )
    if validators and any(
        validator["validator_key_ref"] is None for validator in validators
    ):
        raise ValueError(
            "initial validator profiles require validator key material; "
            "pass --generate-validator-key"
        )

    node_image_mode, node_integrated_image, node_split_image = (
        _resolve_node_image_settings(
            node_image_mode=_pick_template_value(
                args.node_image_mode,
                None,
                "local_build",
            ),
            node_integrated_image=args.node_integrated_image,
            node_split_image=args.node_split_image,
        )
    )

    manifest = NetworkManifest(
        name=args.name,
        chain_id=args.chain_id,
        mode=args.mode,
        runtime_backend=_pick_template_value(
            args.runtime_backend,
            None if template is None else template.get("runtime_backend"),
            "xian-stack",
        ),
        genesis_source=genesis_source,
        snapshot_url=args.snapshot_url,
        snapshot_signing_keys=args.snapshot_signing_key or [],
        seed_nodes=args.seed or [],
        block_policy_mode=_pick_template_value(
            args.block_policy_mode,
            None if template is None else template.get("block_policy_mode"),
            "on_demand",
        ),
        block_policy_interval=_pick_template_value(
            args.block_policy_interval,
            None if template is None else template.get("block_policy_interval"),
            "0s",
        ),
        tracer_mode=_pick_template_value(
            args.tracer_mode,
            None if template is None else template.get("tracer_mode"),
            "python_line_v1",
        ),
        node_image_mode=node_image_mode,
        node_integrated_image=node_integrated_image,
        node_split_image=node_split_image,
        node_release_manifest=None,
    )
    write_json(target, manifest.to_dict(), force=args.force)

    result: dict[str, object] = {
        "manifest_path": str(target),
        "mode": args.mode,
        "genesis_source": genesis_source,
        "template": None if template is None else template["name"],
    }
    if generated_genesis_path is not None:
        result["generated_genesis_path"] = str(generated_genesis_path)
    result["validators"] = []

    for validator in validators:
        validator_result: dict[str, object] = {
            "name": validator["name"],
            "moniker": validator["moniker"],
            "validator_key_ref": validator["validator_key_ref"],
        }
        profile_path = _resolve_output_path(
            base_dir=base_dir,
            explicit_output=(
                args.node_output if validator["is_bootstrap"] else None
            ),
            default_path=base_dir / "nodes" / f"{validator['name']}.json",
        )
        profile = NodeProfile(
            name=validator["name"],
            network=args.name,
            moniker=validator["moniker"],
            validator_key_ref=validator["validator_key_ref"],
            runtime_backend=manifest.runtime_backend,
            node_image_mode=manifest.node_image_mode,
            node_integrated_image=manifest.node_integrated_image,
            node_split_image=manifest.node_split_image,
            node_release_manifest=manifest.node_release_manifest,
            stack_dir=(
                str(args.stack_dir)
                if validator["is_bootstrap"] and args.stack_dir is not None
                else None
            ),
            seeds=[],
            genesis_url=None,
            snapshot_url=(
                args.snapshot_url if validator["is_bootstrap"] else None
            ),
            snapshot_signing_keys=(
                list(args.snapshot_signing_key or [])
                if validator["is_bootstrap"]
                else []
            ),
            service_node=(
                _pick_template_value(
                    args.service_node,
                    None if template is None else template.get("service_node"),
                    False,
                )
                if validator["is_bootstrap"]
                else False
            ),
            home=(
                str(args.home)
                if validator["is_bootstrap"] and args.home is not None
                else None
            ),
            pruning_enabled=(
                _pick_template_value(
                    args.enable_pruning,
                    None
                    if template is None
                    else template.get("pruning_enabled"),
                    False,
                )
                if validator["is_bootstrap"]
                else False
            ),
            blocks_to_keep=(
                _pick_template_value(
                    args.blocks_to_keep,
                    None
                    if template is None
                    else template.get("blocks_to_keep"),
                    100000,
                )
                if validator["is_bootstrap"]
                else 100000
            ),
            block_policy_mode=manifest.block_policy_mode,
            block_policy_interval=manifest.block_policy_interval,
            tracer_mode=manifest.tracer_mode,
            transaction_trace_logging=_pick_template_value(
                args.transaction_trace_logging,
                None
                if template is None
                else template.get("transaction_trace_logging"),
                False,
            ),
            app_log_level=_pick_template_value(
                args.app_log_level,
                None if template is None else template.get("app_log_level"),
                "INFO",
            ),
            app_log_json=_pick_template_value(
                args.app_log_json,
                None if template is None else template.get("app_log_json"),
                False,
            ),
            app_log_rotation_hours=_validate_positive_int(
                "app_log_rotation_hours",
                _pick_template_value(
                    args.app_log_rotation_hours,
                    None
                    if template is None
                    else template.get("app_log_rotation_hours"),
                    1,
                ),
            ),
            app_log_retention_days=_validate_positive_int(
                "app_log_retention_days",
                _pick_template_value(
                    args.app_log_retention_days,
                    None
                    if template is None
                    else template.get("app_log_retention_days"),
                    7,
                ),
            ),
            simulation_enabled=_pick_template_value(
                args.simulation_enabled,
                None
                if template is None
                else template.get("simulation_enabled"),
                True,
            ),
            simulation_max_concurrency=_validate_positive_int(
                "simulation_max_concurrency",
                _pick_template_value(
                    args.simulation_max_concurrency,
                    None
                    if template is None
                    else template.get("simulation_max_concurrency"),
                    2,
                ),
            ),
            simulation_timeout_ms=_validate_positive_int(
                "simulation_timeout_ms",
                _pick_template_value(
                    args.simulation_timeout_ms,
                    None
                    if template is None
                    else template.get("simulation_timeout_ms"),
                    3000,
                ),
            ),
            simulation_max_chi=_validate_positive_int(
                "simulation_max_chi",
                _pick_template_value(
                    args.simulation_max_chi,
                    None
                    if template is None
                    else template.get("simulation_max_chi"),
                    1_000_000,
                ),
            ),
            parallel_execution_enabled=_pick_template_value(
                args.parallel_execution_enabled,
                None
                if template is None
                else template.get("parallel_execution_enabled"),
                False,
            ),
            parallel_execution_workers=_validate_non_negative_int(
                "parallel_execution_workers",
                _pick_template_value(
                    args.parallel_execution_workers,
                    None
                    if template is None
                    else template.get("parallel_execution_workers"),
                    0,
                ),
            ),
            parallel_execution_min_transactions=_validate_non_negative_int(
                "parallel_execution_min_transactions",
                _pick_template_value(
                    args.parallel_execution_min_transactions,
                    None
                    if template is None
                    else template.get("parallel_execution_min_transactions"),
                    8,
                ),
            ),
            operator_profile=(
                template.get("operator_profile")
                if validator["is_bootstrap"] and template is not None
                else None
            ),
            monitoring_profile=(
                template.get("monitoring_profile")
                if validator["is_bootstrap"] and template is not None
                else None
            ),
            dashboard_enabled=(
                _pick_template_value(
                    args.enable_dashboard,
                    None
                    if template is None
                    else template.get("dashboard_enabled"),
                    False,
                )
                if validator["is_bootstrap"]
                else False
            ),
            monitoring_enabled=(
                _pick_template_value(
                    args.enable_monitoring,
                    None
                    if template is None
                    else template.get("monitoring_enabled"),
                    False,
                )
                if validator["is_bootstrap"]
                else False
            ),
            dashboard_host=(
                _pick_template_value(
                    args.dashboard_host,
                    None
                    if template is None
                    else template.get("dashboard_host"),
                    "127.0.0.1",
                )
                if validator["is_bootstrap"]
                else "127.0.0.1"
            ),
            dashboard_port=(
                _pick_template_value(
                    args.dashboard_port,
                    None
                    if template is None
                    else template.get("dashboard_port"),
                    8080,
                )
                if validator["is_bootstrap"]
                else 8080
            ),
            intentkit_enabled=(
                _pick_template_value(
                    args.enable_intentkit,
                    None
                    if template is None
                    else template.get("intentkit_enabled"),
                    False,
                )
                if validator["is_bootstrap"]
                else False
            ),
            intentkit_network_id=(
                _pick_template_value(
                    args.intentkit_network_id,
                    None
                    if template is None
                    else template.get("intentkit_network_id"),
                    "xian-localnet",
                )
                if validator["is_bootstrap"]
                else "xian-localnet"
            ),
            intentkit_host=(
                _pick_template_value(
                    args.intentkit_host,
                    None
                    if template is None
                    else template.get("intentkit_host"),
                    "127.0.0.1",
                )
                if validator["is_bootstrap"]
                else "127.0.0.1"
            ),
            intentkit_port=(
                _pick_template_value(
                    args.intentkit_port,
                    None
                    if template is None
                    else template.get("intentkit_port"),
                    38000,
                )
                if validator["is_bootstrap"]
                else 38000
            ),
            intentkit_api_port=(
                _pick_template_value(
                    args.intentkit_api_port,
                    None
                    if template is None
                    else template.get("intentkit_api_port"),
                    38080,
                )
                if validator["is_bootstrap"]
                else 38080
            ),
            shielded_relayer_enabled=(
                _pick_template_value(
                    None,
                    None
                    if template is None
                    else template.get("shielded_relayer_enabled"),
                    False,
                )
                if validator["is_bootstrap"]
                else False
            ),
            shielded_relayer_host=(
                _pick_template_value(
                    None,
                    None
                    if template is None
                    else template.get("shielded_relayer_host"),
                    "127.0.0.1",
                )
                if validator["is_bootstrap"]
                else "127.0.0.1"
            ),
            shielded_relayer_port=(
                _pick_template_value(
                    None,
                    None
                    if template is None
                    else template.get("shielded_relayer_port"),
                    38180,
                )
                if validator["is_bootstrap"]
                else 38180
            ),
        )
        write_json(profile_path, profile.to_dict(), force=args.force)
        validator_result["profile_path"] = str(profile_path)
        result["validators"].append(validator_result)

        if validator["is_bootstrap"]:
            result["profile_path"] = str(profile_path)
            if validator["validator_key_ref"] is not None:
                result["validator_key_ref"] = validator["validator_key_ref"]

    if bootstrap_name is not None:
        bootstrap_validator = next(
            (
                validator
                for validator in validators
                if validator["name"] == bootstrap_name
            ),
            None,
        )
        if (
            bootstrap_validator is None
            or bootstrap_validator["validator_key_ref"] is None
        ):
            raise ValueError(
                "--bootstrap-node requires validator key material; "
                "pass --generate-validator-key or --validator-key-ref"
            )

        if args.init_node:
            bootstrap_profile_path = Path(result["profile_path"])
            init_args = argparse.Namespace(
                name=bootstrap_name,
                base_dir=base_dir,
                profile=bootstrap_profile_path,
                network=target,
                validator_key=None,
                stack_dir=args.stack_dir,
                configs_dir=args.configs_dir,
                home=args.home,
                force=args.force,
                restore_snapshot=False,
                snapshot_url=None,
            )
            result["node_initialized"] = True
            result["node_init"] = _initialize_node_from_args(init_args)

    print(json.dumps(result, indent=2))
    return 0


def _handle_network_join(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    template = _load_template(
        base_dir=base_dir,
        template_name=args.template,
        configs_dir=args.configs_dir,
    )
    network_path = resolve_network_manifest_path(
        base_dir=base_dir,
        network_name=args.network,
        explicit_manifest=args.network_manifest,
        configs_dir=args.configs_dir,
    )
    network = read_network_manifest(network_path)
    runtime_backend = _pick_template_value(
        args.runtime_backend,
        None if template is None else template.get("runtime_backend"),
        network.get("runtime_backend") or "xian-stack",
    )
    if args.generate_validator_key and args.validator_key_ref is not None:
        raise ValueError(
            "pass either --validator-key-ref or --generate-validator-key, "
            "not both"
        )
    if args.validator_key_dir is not None and not args.generate_validator_key:
        raise ValueError(
            "--validator-key-dir requires --generate-validator-key"
        )
    if args.restore_snapshot and not args.init_node:
        raise ValueError("--restore-snapshot requires --init-node")

    requested_node_image_mode = _pick_template_value(
        args.node_image_mode,
        None,
        network.get("node_image_mode") or "local_build",
    )
    node_image_mode, node_integrated_image, node_split_image = (
        _resolve_node_image_settings(
            node_image_mode=requested_node_image_mode,
            node_integrated_image=_pick_template_value(
                args.node_integrated_image,
                None,
                network.get("node_integrated_image")
                if requested_node_image_mode == "registry"
                else None,
            ),
            node_split_image=_pick_template_value(
                args.node_split_image,
                None,
                network.get("node_split_image")
                if requested_node_image_mode == "registry"
                else None,
            ),
        )
    )

    validator_key_ref: str | None = None
    if args.validator_key_ref is not None:
        validator_key_ref = _stringify_path_for_profile(
            args.validator_key_ref,
            base_dir=base_dir,
        )
    elif args.generate_validator_key:
        key_dir = args.validator_key_dir or base_dir / "keys" / args.name
        if not key_dir.is_absolute():
            key_dir = (base_dir / key_dir).resolve()
        metadata_path = _write_validator_material_files(
            out_dir=key_dir,
            force=args.force,
        )
        validator_key_ref = _stringify_path_for_profile(
            metadata_path,
            base_dir=base_dir,
        )

    profile = NodeProfile(
        name=args.name,
        network=args.network,
        moniker=args.moniker or args.name,
        validator_key_ref=validator_key_ref,
        runtime_backend=runtime_backend,
        node_image_mode=node_image_mode,
        node_integrated_image=node_integrated_image,
        node_split_image=node_split_image,
        node_release_manifest=_effective_node_release_manifest(
            {
                "node_image_mode": node_image_mode,
                "node_integrated_image": node_integrated_image,
                "node_split_image": node_split_image,
            },
            network,
        ),
        stack_dir=str(args.stack_dir) if args.stack_dir is not None else None,
        seeds=args.seed or [],
        genesis_url=args.genesis_url,
        snapshot_url=args.snapshot_url,
        snapshot_signing_keys=args.snapshot_signing_key or [],
        service_node=_pick_template_value(
            args.service_node,
            None if template is None else template.get("service_node"),
            False,
        ),
        home=str(args.home) if args.home is not None else None,
        pruning_enabled=_pick_template_value(
            args.enable_pruning,
            None if template is None else template.get("pruning_enabled"),
            False,
        ),
        blocks_to_keep=_pick_template_value(
            args.blocks_to_keep,
            None if template is None else template.get("blocks_to_keep"),
            100000,
        ),
        block_policy_mode=_pick_template_value(
            args.block_policy_mode,
            None if template is None else template.get("block_policy_mode"),
            network.get("block_policy_mode", "on_demand"),
        ),
        block_policy_interval=_pick_template_value(
            args.block_policy_interval,
            None if template is None else template.get("block_policy_interval"),
            network.get("block_policy_interval", "0s"),
        ),
        tracer_mode=_pick_template_value(
            args.tracer_mode,
            None if template is None else template.get("tracer_mode"),
            network.get("tracer_mode", "python_line_v1"),
        ),
        transaction_trace_logging=_pick_template_value(
            args.transaction_trace_logging,
            None
            if template is None
            else template.get("transaction_trace_logging"),
            False,
        ),
        app_log_level=_pick_template_value(
            args.app_log_level,
            None if template is None else template.get("app_log_level"),
            "INFO",
        ),
        app_log_json=_pick_template_value(
            args.app_log_json,
            None if template is None else template.get("app_log_json"),
            False,
        ),
        app_log_rotation_hours=_validate_positive_int(
            "app_log_rotation_hours",
            _pick_template_value(
                args.app_log_rotation_hours,
                None
                if template is None
                else template.get("app_log_rotation_hours"),
                1,
            ),
        ),
        app_log_retention_days=_validate_positive_int(
            "app_log_retention_days",
            _pick_template_value(
                args.app_log_retention_days,
                None
                if template is None
                else template.get("app_log_retention_days"),
                7,
            ),
        ),
        simulation_enabled=_pick_template_value(
            args.simulation_enabled,
            None if template is None else template.get("simulation_enabled"),
            True,
        ),
        simulation_max_concurrency=_validate_positive_int(
            "simulation_max_concurrency",
            _pick_template_value(
                args.simulation_max_concurrency,
                None
                if template is None
                else template.get("simulation_max_concurrency"),
                2,
            ),
        ),
        simulation_timeout_ms=_validate_positive_int(
            "simulation_timeout_ms",
            _pick_template_value(
                args.simulation_timeout_ms,
                None
                if template is None
                else template.get("simulation_timeout_ms"),
                3000,
            ),
        ),
        simulation_max_chi=_validate_positive_int(
            "simulation_max_chi",
            _pick_template_value(
                args.simulation_max_chi,
                None
                if template is None
                else template.get("simulation_max_chi"),
                1_000_000,
            ),
        ),
        parallel_execution_enabled=_pick_template_value(
            args.parallel_execution_enabled,
            None
            if template is None
            else template.get("parallel_execution_enabled"),
            False,
        ),
        parallel_execution_workers=_validate_non_negative_int(
            "parallel_execution_workers",
            _pick_template_value(
                args.parallel_execution_workers,
                None
                if template is None
                else template.get("parallel_execution_workers"),
                0,
            ),
        ),
        parallel_execution_min_transactions=_validate_non_negative_int(
            "parallel_execution_min_transactions",
            _pick_template_value(
                args.parallel_execution_min_transactions,
                None
                if template is None
                else template.get("parallel_execution_min_transactions"),
                8,
            ),
        ),
        operator_profile=(
            None if template is None else template.get("operator_profile")
        ),
        monitoring_profile=(
            None if template is None else template.get("monitoring_profile")
        ),
        dashboard_enabled=_pick_template_value(
            args.enable_dashboard,
            None if template is None else template.get("dashboard_enabled"),
            False,
        ),
        monitoring_enabled=_pick_template_value(
            args.enable_monitoring,
            None if template is None else template.get("monitoring_enabled"),
            False,
        ),
        dashboard_host=_pick_template_value(
            args.dashboard_host,
            None if template is None else template.get("dashboard_host"),
            "127.0.0.1",
        ),
        dashboard_port=_pick_template_value(
            args.dashboard_port,
            None if template is None else template.get("dashboard_port"),
            8080,
        ),
        intentkit_enabled=_pick_template_value(
            args.enable_intentkit,
            None if template is None else template.get("intentkit_enabled"),
            False,
        ),
        intentkit_network_id=_pick_template_value(
            args.intentkit_network_id,
            None if template is None else template.get("intentkit_network_id"),
            _default_intentkit_network_id(network.get("name")),
        ),
        intentkit_host=_pick_template_value(
            args.intentkit_host,
            None if template is None else template.get("intentkit_host"),
            "127.0.0.1",
        ),
        intentkit_port=_pick_template_value(
            args.intentkit_port,
            None if template is None else template.get("intentkit_port"),
            38000,
        ),
        intentkit_api_port=_pick_template_value(
            args.intentkit_api_port,
            None if template is None else template.get("intentkit_api_port"),
            38080,
        ),
        shielded_relayer_enabled=_pick_template_value(
            None,
            None
            if template is None
            else template.get("shielded_relayer_enabled"),
            False,
        ),
        shielded_relayer_host=_pick_template_value(
            None,
            None if template is None else template.get("shielded_relayer_host"),
            "127.0.0.1",
        ),
        shielded_relayer_port=_pick_template_value(
            None,
            None if template is None else template.get("shielded_relayer_port"),
            38180,
        ),
    )
    target = args.output or base_dir / "nodes" / f"{args.name}.json"
    if not target.is_absolute():
        target = (base_dir / target).resolve()
    write_json(target, profile.to_dict(), force=args.force)
    if not args.init_node:
        print(f"wrote node profile to {target} using {network_path}")
        return 0

    init_args = argparse.Namespace(
        name=args.name,
        base_dir=base_dir,
        profile=target,
        network=network_path,
        validator_key=None,
        stack_dir=args.stack_dir,
        configs_dir=args.configs_dir,
        home=args.home,
        force=args.force,
        restore_snapshot=args.restore_snapshot,
        snapshot_url=args.snapshot_url,
    )
    init_result = _initialize_node_from_args(init_args)
    print(
        json.dumps(
            {
                "profile_path": str(target),
                "network_path": str(network_path),
                "template": None if template is None else template["name"],
                "validator_key_ref": validator_key_ref,
                "node_initialized": True,
                "node_init": init_result,
            },
            indent=2,
        )
    )
    return 0


def _resolve_path(
    value: str | None, *, base_dir: Path, fallback_dir: Path | None = None
) -> Path | None:
    if value is None:
        return None

    raw_path = Path(value).expanduser()
    if raw_path.is_absolute():
        return raw_path

    candidates = [base_dir / raw_path]
    if fallback_dir is not None:
        candidates.append(fallback_dir / raw_path)

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()

    return candidates[0].resolve()


def _resolve_stack_dir_from_profile(
    *,
    base_dir: Path,
    profile: dict,
    explicit_stack_dir: Path | None,
) -> Path | None:
    stack_dir = explicit_stack_dir
    if stack_dir is None and profile.get("stack_dir"):
        stack_dir = _resolve_path(
            profile["stack_dir"],
            base_dir=base_dir,
        )
    if stack_dir is not None and not stack_dir.is_absolute():
        stack_dir = (base_dir / stack_dir).resolve()
    return stack_dir


def _load_genesis_payload(
    genesis_source: str, *, base_dir: Path, manifest_path: Path
) -> dict:
    parsed = urlparse(genesis_source)
    if parsed.scheme in {"http", "https"}:
        return fetch_json(genesis_source)

    genesis_path = _resolve_path(
        genesis_source,
        base_dir=base_dir,
        fallback_dir=manifest_path.parent,
    )
    if genesis_path is None or not genesis_path.exists():
        raise FileNotFoundError(f"genesis source not found: {genesis_source}")

    return read_json(genesis_path)


def _build_preset_genesis_payload(
    *,
    base_dir: Path,
    chain_id: str,
    genesis_preset: str,
    genesis_time: str | None,
    configs_dir: Path | None,
) -> dict:
    genesis_builder = get_genesis_builder_module()
    resolved_configs_dir = resolve_configs_dir(base_dir, explicit=configs_dir)
    return genesis_builder.build_bundle_network_genesis(
        chain_id=chain_id,
        network=genesis_preset,
        contracts_dir=resolved_configs_dir / "contracts",
        genesis_time=genesis_time,
    )


def _resolve_effective_genesis_payload(
    *,
    profile: dict,
    network: dict,
    base_dir: Path,
    manifest_path: Path,
    configs_dir: Path | None,
) -> tuple[dict, str]:
    genesis_source = profile.get("genesis_url") or network.get("genesis_source")
    if genesis_source:
        return (
            _load_genesis_payload(
                genesis_source,
                base_dir=base_dir,
                manifest_path=manifest_path,
            ),
            genesis_source,
        )

    genesis_preset = network.get("genesis_preset")
    if genesis_preset:
        return (
            _build_preset_genesis_payload(
                base_dir=base_dir,
                chain_id=network["chain_id"],
                genesis_preset=genesis_preset,
                genesis_time=network.get("genesis_time"),
                configs_dir=configs_dir,
            ),
            f"preset:{genesis_preset}",
        )

    raise ValueError(
        "no genesis source configured; set genesis_source or genesis_preset "
        "in the network manifest"
    )


def _extract_priv_validator_key(payload: dict) -> dict:
    if "priv_validator_key" in payload:
        return payload["priv_validator_key"]

    if {"address", "pub_key", "priv_key"}.issubset(payload.keys()):
        return payload

    raise ValueError(
        "validator key file must contain either priv_validator_key or a raw "
        "priv_validator_key.json payload"
    )


def _extract_validator_private_key_hex(payload: dict) -> str:
    private_key_hex = payload.get("validator_private_key_hex")
    if private_key_hex is not None:
        return private_key_hex

    priv_validator_key = _extract_priv_validator_key(payload)
    try:
        raw_private_key = base64.b64decode(
            priv_validator_key["priv_key"]["value"].encode("ascii")
        )
    except (KeyError, ValueError) as exc:
        raise ValueError(
            "validator key file does not contain a usable private key"
        ) from exc

    if len(raw_private_key) < 32:
        raise ValueError(
            "validator key file does not contain a usable private key"
        )
    return raw_private_key[:32].hex()


def _extract_validator_public_key_hex(payload: dict) -> str:
    public_key_hex = payload.get("validator_public_key_hex")
    if public_key_hex is not None:
        return public_key_hex

    priv_validator_key = _extract_priv_validator_key(payload)
    try:
        raw_public_key = base64.b64decode(
            priv_validator_key["pub_key"]["value"].encode("ascii")
        )
    except (KeyError, ValueError) as exc:
        raise ValueError(
            "validator key file does not contain a usable public key"
        ) from exc

    if len(raw_public_key) != 32:
        raise ValueError(
            "validator key file does not contain a usable public key"
        )
    return raw_public_key.hex()


def _build_creation_validator_entries(
    *,
    validators: list[dict[str, object]],
) -> list[dict[str, object]]:
    return [
        {
            "account_public_key": _extract_validator_public_key_hex(
                validator["validator_key_payload"]
            ),
            "name": validator["name"],
            "power": validator["power"],
            "priv_validator_key": _extract_priv_validator_key(
                validator["validator_key_payload"]
            ),
        }
        for validator in validators
    ]


def _build_creation_genesis(
    *,
    chain_id: str,
    founder_private_key: str,
    validators: list[dict[str, object]],
    genesis_preset: str,
) -> dict:
    genesis_builder = get_genesis_builder_module()
    return genesis_builder.build_local_network_genesis(
        chain_id=chain_id,
        founder_private_key=founder_private_key,
        validators=_build_creation_validator_entries(validators=validators),
        network=genesis_preset,
    )


def _resolve_runtime_backend(profile: dict, network: dict) -> str:
    runtime_backend = profile.get("runtime_backend") or network.get(
        "runtime_backend"
    )
    if runtime_backend not in SUPPORTED_RUNTIME_BACKENDS:
        raise ValueError(f"unsupported runtime backend: {runtime_backend}")
    return runtime_backend


def _resolve_home(
    *,
    base_dir: Path,
    profile: dict,
    profile_path: Path,
    runtime_backend: str,
    stack_dir: Path | None,
    explicit_home: Path | None = None,
) -> Path:
    resolved_home = explicit_home
    if resolved_home is not None and not resolved_home.is_absolute():
        resolved_home = (base_dir / resolved_home).resolve()

    home = resolved_home or _resolve_path(
        profile.get("home"),
        base_dir=base_dir,
        fallback_dir=profile_path.parent,
    )
    if home is None:
        home = default_home_for_backend(
            base_dir=base_dir,
            runtime_backend=runtime_backend,
            stack_dir=stack_dir,
        )
    return home


def _resolve_effective_snapshot_url(
    *,
    profile: dict,
    network: dict,
    explicit_snapshot_url: str | None = None,
) -> str | None:
    return (
        explicit_snapshot_url
        or profile.get("snapshot_url")
        or network.get("snapshot_url")
    )


def _resolve_effective_snapshot_signing_keys(
    *,
    profile: dict,
    network: dict,
) -> list[str]:
    profile_keys = profile.get("snapshot_signing_keys")
    if isinstance(profile_keys, list) and profile_keys:
        return list(profile_keys)
    network_keys = network.get("snapshot_signing_keys")
    if isinstance(network_keys, list):
        return list(network_keys)
    return []


def _restore_snapshot(
    *,
    base_dir: Path,
    profile: dict,
    profile_path: Path,
    network: dict,
    runtime_backend: str,
    stack_dir: Path | None,
    explicit_home: Path | None = None,
    explicit_snapshot_url: str | None = None,
) -> dict:
    home = _resolve_home(
        base_dir=base_dir,
        profile=profile,
        profile_path=profile_path,
        runtime_backend=runtime_backend,
        stack_dir=stack_dir,
        explicit_home=explicit_home,
    )
    config_path = home / "config" / "config.toml"
    if not config_path.exists():
        raise FileNotFoundError(
            f"{config_path} does not exist; "
            f"run `xian node init {profile['name']}` first"
        )

    snapshot_url = _resolve_effective_snapshot_url(
        profile=profile,
        network=network,
        explicit_snapshot_url=explicit_snapshot_url,
    )
    snapshot_signing_keys = _resolve_effective_snapshot_signing_keys(
        profile=profile,
        network=network,
    )
    if not snapshot_url:
        raise ValueError(
            "no snapshot source configured; "
            "set snapshot_url in the network manifest or node profile"
        )

    node_admin = get_node_admin_module()
    snapshot_archive_name = node_admin.apply_snapshot_archive(
        snapshot_url,
        home,
        trusted_manifest_public_keys=snapshot_signing_keys,
        expected_chain_id=network.get("chain_id"),
    )
    return {
        "home": str(home),
        "snapshot_url": snapshot_url,
        "snapshot_archive_name": snapshot_archive_name,
    }


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
            f"{config_path} does not exist; "
            f"run `xian node init {profile['name']}` first"
        )

    if network["chain_id"] != plan["chain_id"]:
        raise ValueError(
            "recovery plan chain_id does not match the node network manifest"
        )

    rpc_status = None
    rpc_checked = False
    if rpc_url:
        rpc_status = _resolve_recovery_rpc_status(rpc_url=rpc_url)
        rpc_checked = rpc_status is not None
        if rpc_status is not None:
            network_id = (
                rpc_status.get("result", {}).get("node_info", {}).get("network")
            )
            if network_id and network_id != plan["chain_id"]:
                raise ValueError(
                    "live RPC chain_id does not match the recovery plan"
                )
            latest_height = (
                rpc_status.get("result", {})
                .get("sync_info", {})
                .get("latest_block_height")
            )
            if latest_height is not None:
                try:
                    latest_height_int = int(latest_height)
                except (TypeError, ValueError):
                    latest_height_int = None
                if (
                    latest_height_int is not None
                    and latest_height_int < plan["target_height"]
                ):
                    raise ValueError(
                        "live RPC height is below the recovery target height"
                    )

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
    runtime_backend = _resolve_runtime_backend(profile, network)
    stack_dir = _resolve_stack_dir_from_profile(
        base_dir=base_dir,
        profile=profile,
        explicit_stack_dir=args.stack_dir,
    )
    home = _resolve_home(
        base_dir=base_dir,
        profile=profile,
        profile_path=profile_path,
        runtime_backend=runtime_backend,
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
        raise ValueError(
            "recovery apply is destructive; pass --yes after reviewing the plan"
        )

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
    runtime_backend = _resolve_runtime_backend(profile, network)
    stack_dir = _resolve_stack_dir_from_profile(
        base_dir=base_dir,
        profile=profile,
        explicit_stack_dir=args.stack_dir,
    )
    home = _resolve_home(
        base_dir=base_dir,
        profile=profile,
        profile_path=profile_path,
        runtime_backend=runtime_backend,
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
    if runtime_backend == "xian-stack" and not args.skip_stop:
        if stack_dir is None:
            raise ValueError(
                "recovery apply requires a resolved xian-stack directory "
                "unless --skip-stop is used"
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
        if runtime_backend != "xian-stack" or stack_dir is None:
            raise ValueError(
                "--start-node currently requires the xian-stack runtime backend"
            )
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
            "backup_archive": (
                None if backup_archive is None else str(backup_archive)
            ),
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


def _load_profile_and_network(
    *,
    base_dir: Path,
    name: str,
    profile_arg: Path | None,
    network_arg: Path | None,
    configs_dir: Path | None = None,
) -> tuple[Path, dict, Path, dict]:
    profile_path = profile_arg or base_dir / "nodes" / f"{name}.json"
    if not profile_path.is_absolute():
        profile_path = (base_dir / profile_path).resolve()
    if not profile_path.exists():
        raise FileNotFoundError(f"node profile not found: {profile_path}")

    profile = read_node_profile(profile_path)
    network_name = profile.get("network")
    if not network_name:
        raise ValueError(
            "node profile is missing network; "
            "recreate it with xian network join"
        )

    network_path = resolve_network_manifest_path(
        base_dir=base_dir,
        network_name=network_name,
        explicit_manifest=network_arg,
        configs_dir=configs_dir,
    )
    network = read_network_manifest(network_path)
    return profile_path, profile, network_path, network


def _initialize_node_from_args(args: argparse.Namespace) -> dict:
    node_setup = get_node_setup_module()

    base_dir = args.base_dir.resolve()
    profile_path, profile, network_path, network = _load_profile_and_network(
        base_dir=base_dir,
        name=args.name,
        profile_arg=args.profile,
        network_arg=args.network,
        configs_dir=args.configs_dir,
    )
    runtime_backend = _resolve_runtime_backend(profile, network)

    stack_dir = _resolve_stack_dir_from_profile(
        base_dir=base_dir,
        profile=profile,
        explicit_stack_dir=args.stack_dir,
    )

    explicit_validator_key = args.validator_key
    if (
        explicit_validator_key is not None
        and not explicit_validator_key.is_absolute()
    ):
        explicit_validator_key = (base_dir / explicit_validator_key).resolve()

    validator_key_ref = explicit_validator_key or _resolve_path(
        profile.get("validator_key_ref"),
        base_dir=base_dir,
        fallback_dir=profile_path.parent,
    )
    if validator_key_ref is None or not validator_key_ref.exists():
        raise FileNotFoundError(
            "validator key reference is required; set validator_key_ref in the "
            "node profile or pass --validator-key"
        )

    validator_key_payload = _extract_priv_validator_key(
        read_json(validator_key_ref)
    )
    genesis, effective_genesis_source = _resolve_effective_genesis_payload(
        profile=profile,
        network=network,
        base_dir=base_dir,
        manifest_path=network_path,
        configs_dir=args.configs_dir,
    )

    if genesis.get("chain_id") and genesis["chain_id"] != network["chain_id"]:
        raise ValueError(
            f"genesis chain_id {genesis['chain_id']} does not match manifest "
            f"chain_id {network['chain_id']}"
        )

    home = _resolve_home(
        base_dir=base_dir,
        profile=profile,
        profile_path=profile_path,
        runtime_backend=runtime_backend,
        stack_dir=stack_dir,
        explicit_home=args.home,
    )

    seed_nodes = list(network.get("seed_nodes") or [])
    seed_nodes.extend(profile.get("seeds") or [])

    config = node_setup.render_cometbft_config(
        moniker=profile["moniker"],
        seed_nodes=seed_nodes,
        service_node=bool(profile.get("service_node")),
        enable_pruning=bool(profile.get("pruning_enabled")),
        blocks_to_keep=int(profile.get("blocks_to_keep", 100000)),
        block_policy_mode=str(
            profile.get(
                "block_policy_mode",
                network.get("block_policy_mode", "on_demand"),
            )
        ),
        block_policy_interval=str(
            profile.get(
                "block_policy_interval",
                network.get("block_policy_interval", "0s"),
            )
        ),
        tracer_mode=str(
            profile.get(
                "tracer_mode",
                network.get("tracer_mode", "python_line_v1"),
            )
        ),
        transaction_trace_logging=bool(
            profile.get("transaction_trace_logging", False)
        ),
        app_log_level=str(profile.get("app_log_level", "INFO")),
        app_log_json=bool(profile.get("app_log_json", False)),
        app_log_rotation_hours=int(profile.get("app_log_rotation_hours", 1)),
        app_log_retention_days=int(profile.get("app_log_retention_days", 7)),
        simulation_enabled=bool(profile.get("simulation_enabled", True)),
        simulation_max_concurrency=int(
            profile.get("simulation_max_concurrency", 2)
        ),
        simulation_timeout_ms=int(profile.get("simulation_timeout_ms", 3000)),
        simulation_max_chi=int(profile.get("simulation_max_chi", 1_000_000)),
        parallel_execution_enabled=bool(
            profile.get("parallel_execution_enabled", False)
        ),
        parallel_execution_workers=int(
            profile.get("parallel_execution_workers", 0)
        ),
        parallel_execution_min_transactions=int(
            profile.get("parallel_execution_min_transactions", 8)
        ),
        # The xian-stack runtime publishes the app metrics port from Docker,
        # so the in-container exporter must listen on all interfaces.
        metrics_host="0.0.0.0"
        if runtime_backend == "xian-stack"
        else "127.0.0.1",
    )

    result = node_setup.materialize_cometbft_home(
        home=home,
        config=config,
        genesis=genesis,
        priv_validator_key=validator_key_payload,
        overwrite=args.force,
    )
    effective_snapshot_url = _resolve_effective_snapshot_url(
        profile=profile,
        network=network,
        explicit_snapshot_url=getattr(args, "snapshot_url", None),
    )
    result["effective_snapshot_url"] = effective_snapshot_url
    result["effective_genesis_source"] = effective_genesis_source
    result["snapshot_restored"] = False
    if getattr(args, "restore_snapshot", False):
        snapshot_result = _restore_snapshot(
            base_dir=base_dir,
            profile=profile,
            profile_path=profile_path,
            network=network,
            runtime_backend=runtime_backend,
            stack_dir=stack_dir,
            explicit_home=home,
            explicit_snapshot_url=getattr(args, "snapshot_url", None),
        )
        result["snapshot_restored"] = True
        result["snapshot"] = snapshot_result
    return result


def _handle_node_init(args: argparse.Namespace) -> int:
    result = _initialize_node_from_args(args)
    print(json.dumps(result, indent=2))
    return 0


def _handle_node_start(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    profile_path, profile, _, network = _load_profile_and_network(
        base_dir=base_dir,
        name=args.name,
        profile_arg=args.profile,
        network_arg=args.network,
        configs_dir=args.configs_dir,
    )
    runtime_backend = _resolve_runtime_backend(profile, network)

    stack_dir = resolve_stack_dir(
        base_dir,
        explicit=_resolve_stack_dir_from_profile(
            base_dir=base_dir,
            profile=profile,
            explicit_stack_dir=args.stack_dir,
        ),
    )
    home = _resolve_home(
        base_dir=base_dir,
        profile=profile,
        profile_path=profile_path,
        runtime_backend=runtime_backend,
        stack_dir=stack_dir,
    )
    config_path = home / "config" / "config.toml"
    if not config_path.exists():
        raise FileNotFoundError(
            f"{config_path} does not exist; "
            f"run `xian node init {args.name}` first"
        )

    result = start_xian_stack_node(
        stack_dir=stack_dir,
        cometbft_home=home,
        **_stack_runtime_profile_kwargs(profile, network),
        wait_for_rpc=not args.skip_health_check,
        rpc_timeout_seconds=args.rpc_timeout_seconds,
    )
    print(json.dumps(result, indent=2))
    return 0


def _handle_node_stop(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    profile_path, profile, _, network = _load_profile_and_network(
        base_dir=base_dir,
        name=args.name,
        profile_arg=args.profile,
        network_arg=args.network,
        configs_dir=args.configs_dir,
    )

    stack_dir = resolve_stack_dir(
        base_dir,
        explicit=_resolve_stack_dir_from_profile(
            base_dir=base_dir,
            profile=profile,
            explicit_stack_dir=args.stack_dir,
        ),
    )
    result = stop_xian_stack_node(
        stack_dir=stack_dir,
        cometbft_home=_resolve_home(
            base_dir=base_dir,
            profile=profile,
            profile_path=profile_path,
            runtime_backend=_resolve_runtime_backend(profile, network),
            stack_dir=stack_dir,
        ),
        **_stack_runtime_profile_kwargs(profile, network),
    )
    print(json.dumps(result, indent=2))
    return 0


def _resolve_node_context(args: argparse.Namespace) -> dict[str, object]:
    base_dir = args.base_dir.resolve()
    profile_path, profile, network_path, network = _load_profile_and_network(
        base_dir=base_dir,
        name=args.name,
        profile_arg=args.profile,
        network_arg=args.network,
        configs_dir=args.configs_dir,
    )
    runtime_backend = _resolve_runtime_backend(profile, network)

    stack_dir = _resolve_stack_dir_from_profile(
        base_dir=base_dir,
        profile=profile,
        explicit_stack_dir=args.stack_dir,
    )
    if runtime_backend == "xian-stack":
        stack_dir = resolve_stack_dir(base_dir, explicit=stack_dir)

    home = _resolve_home(
        base_dir=base_dir,
        profile=profile,
        profile_path=profile_path,
        runtime_backend=runtime_backend,
        stack_dir=stack_dir,
        explicit_home=getattr(args, "home", None),
    )
    return {
        "base_dir": base_dir,
        "profile_path": profile_path,
        "profile": profile,
        "network_path": network_path,
        "network": network,
        "runtime_backend": runtime_backend,
        "stack_dir": stack_dir,
        "home": home,
    }


def _format_url_host(hostname: str) -> str:
    if ":" in hostname and not hostname.startswith("["):
        return f"[{hostname}]"
    return hostname


def _display_endpoint_host(hostname: str) -> str:
    if hostname == "0.0.0.0":
        return "127.0.0.1"
    if hostname == "::":
        return "::1"
    return hostname


def _replace_url_port(url: str, *, port: int, suffix: str = "") -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "http"
    hostname = _format_url_host(parsed.hostname or "127.0.0.1")
    return f"{scheme}://{hostname}:{port}{suffix}"


def _rpc_base_url(rpc_status_url: str) -> str:
    if rpc_status_url.endswith("/status"):
        return rpc_status_url[: -len("/status")]
    return rpc_status_url.rstrip("/")


def _fallback_node_endpoints(
    *,
    rpc_status_url: str,
    profile: NodeProfile,
    network: dict[str, object] | None = None,
) -> dict[str, str]:
    base_url = _rpc_base_url(rpc_status_url)
    endpoints = {
        "rpc": base_url,
        "rpc_status": rpc_status_url,
        "abci_query": f"{base_url}/abci_query",
        "cometbft_metrics": _replace_url_port(
            base_url,
            port=26660,
            suffix="/metrics",
        ),
        "xian_metrics": _replace_url_port(
            base_url,
            port=9108,
            suffix="/metrics",
        ),
    }
    if bool(profile.get("dashboard_enabled")):
        dashboard_host = _display_endpoint_host(
            str(profile.get("dashboard_host", "127.0.0.1"))
        )
        dashboard_port = int(profile.get("dashboard_port", 8080))
        dashboard_url = f"http://{dashboard_host}:{dashboard_port}"
        endpoints["dashboard"] = dashboard_url
        endpoints["dashboard_status"] = f"{dashboard_url}/api/status"
    if bool(profile.get("monitoring_enabled")):
        endpoints["prometheus"] = _replace_url_port(base_url, port=9090)
        endpoints["grafana"] = _replace_url_port(base_url, port=3000)
    if bool(profile.get("intentkit_enabled")):
        intentkit_host = _display_endpoint_host(
            str(profile.get("intentkit_host", "127.0.0.1"))
        )
        intentkit_port = int(profile.get("intentkit_port", 38000))
        intentkit_api_port = int(profile.get("intentkit_api_port", 38080))
        frontend_url = f"http://{intentkit_host}:{intentkit_port}"
        api_url = f"http://{intentkit_host}:{intentkit_api_port}"
        endpoints["intentkit"] = frontend_url
        endpoints["intentkit_api"] = api_url
        endpoints["intentkit_api_health"] = f"{api_url}/health"
    if bool(profile.get("shielded_relayer_enabled")):
        relayer_host = _display_endpoint_host(
            str(profile.get("shielded_relayer_host", "127.0.0.1"))
        )
        relayer_port = int(profile.get("shielded_relayer_port", 38180))
        relayer_url = f"http://{relayer_host}:{relayer_port}"
        endpoints["shielded_relayer"] = relayer_url
        endpoints["shielded_relayer_health"] = f"{relayer_url}/health"
        endpoints["shielded_relayer_info"] = f"{relayer_url}/v1/info"
        endpoints["shielded_relayer_metrics"] = f"{relayer_url}/metrics"
        endpoints["shielded_relayer_quote"] = f"{relayer_url}/v1/quote"
        endpoints["shielded_relayer_jobs"] = f"{relayer_url}/v1/jobs"
    else:
        endpoints.update(_network_shielded_relayer_endpoints(network))
    return endpoints


def _collect_node_endpoints(args: argparse.Namespace) -> dict[str, object]:
    context = _resolve_node_context(args)
    profile = context["profile"]
    network = context["network"]
    runtime_backend = context["runtime_backend"]
    stack_dir = context["stack_dir"]
    home = context["home"]
    rpc_status_url = getattr(args, "rpc_url", "http://127.0.0.1:26657/status")
    node_image_mode, node_integrated_image, node_split_image = (
        _effective_node_image_config(profile, network)
    )

    payload: dict[str, object] = {
        "profile_path": str(context["profile_path"]),
        "network_path": str(context["network_path"]),
        "runtime_backend": runtime_backend,
        "node_image_mode": node_image_mode,
        "node_integrated_image": node_integrated_image,
        "node_split_image": node_split_image,
        "service_node": bool(profile.get("service_node")),
        "operator_profile": profile.get("operator_profile"),
        "monitoring_profile": profile.get("monitoring_profile"),
        "dashboard_enabled": bool(profile.get("dashboard_enabled")),
        "monitoring_enabled": bool(profile.get("monitoring_enabled")),
        "intentkit_enabled": bool(profile.get("intentkit_enabled")),
        "shielded_relayer_enabled": bool(
            profile.get("shielded_relayer_enabled")
        ),
        "intentkit_network_id": profile.get("intentkit_network_id"),
    }
    if stack_dir is not None:
        payload["stack_dir"] = str(stack_dir)

    if runtime_backend == "xian-stack" and stack_dir is not None:
        try:
            backend_payload = get_xian_stack_node_endpoints(
                stack_dir=stack_dir,
                cometbft_home=home,
                **_stack_runtime_profile_kwargs(profile, context["network"]),
            )
            payload["endpoints"] = backend_payload["endpoints"]
            payload["backend_checked"] = True
            return payload
        except Exception as exc:
            payload["backend_checked"] = True
            payload["backend_error"] = str(exc)

    payload["endpoints"] = _fallback_node_endpoints(
        rpc_status_url=rpc_status_url,
        profile=profile,
        network=network,
    )
    return payload


def _summarize_node_status(result: dict[str, object]) -> dict[str, object]:
    rpc_payload = result.get("rpc_status")
    rpc_result = (
        rpc_payload.get("result", {}) if isinstance(rpc_payload, dict) else {}
    )
    sync_info = (
        rpc_result.get("sync_info", {}) if isinstance(rpc_result, dict) else {}
    )
    node_info = (
        rpc_result.get("node_info", {}) if isinstance(rpc_result, dict) else {}
    )
    other = node_info.get("other", {}) if isinstance(node_info, dict) else {}

    state = "ready"
    if not result.get("initialized"):
        state = "not_initialized"
    elif result.get("backend_checked") and not result.get("backend_running"):
        state = "stopped"
    elif result.get("rpc_checked") and not result.get("rpc_reachable"):
        state = "rpc_unreachable"

    summary: dict[str, object] = {
        "state": state,
        "initialized": bool(result.get("initialized")),
        "service_node": bool(result.get("profile", {}).get("service_node")),
        "operator_profile": result.get("profile", {}).get("operator_profile"),
        "monitoring_profile": result.get("profile", {}).get(
            "monitoring_profile"
        ),
        "dashboard_enabled": bool(
            result.get("profile", {}).get("dashboard_enabled")
        ),
        "monitoring_enabled": bool(
            result.get("profile", {}).get("monitoring_enabled")
        ),
        "intentkit_enabled": bool(
            result.get("profile", {}).get("intentkit_enabled")
        ),
        "shielded_relayer_enabled": bool(
            result.get("profile", {}).get("shielded_relayer_enabled")
        ),
        "backend_running": result.get("backend_running"),
        "rpc_reachable": result.get("rpc_reachable"),
        "rpc_height": sync_info.get("latest_block_height"),
        "rpc_catching_up": sync_info.get("catching_up"),
        "rpc_network": node_info.get("network"),
        "peer_count": other.get("n_peers"),
        "node_image_mode": result.get("profile", {}).get("node_image_mode"),
        "node_integrated_image": result.get("profile", {}).get(
            "node_integrated_image"
        ),
        "node_split_image": result.get("profile", {}).get("node_split_image"),
    }

    release_manifest = result.get("node_release_manifest")
    if isinstance(release_manifest, dict):
        components = release_manifest.get("components")
        build = release_manifest.get("build")
        if isinstance(components, dict):
            summary["release_manifest_refs"] = {
                str(name): component.get("ref")
                for name, component in components.items()
                if isinstance(name, str) and isinstance(component, dict)
            }
        if isinstance(build, dict):
            summary["release_manifest_build"] = {
                "python_image": build.get("python_image"),
                "cometbft_version": build.get("cometbft_version"),
                "s6_overlay_version": build.get("s6_overlay_version"),
            }

    backend_status = result.get("backend_status")
    if isinstance(backend_status, dict):
        compose_services = backend_status.get("compose_services")
        if isinstance(compose_services, list):
            runtime_images = {
                str(service.get("service")): service.get("image")
                for service in compose_services
                if isinstance(service, dict)
                and isinstance(service.get("service"), str)
                and service.get("image")
            }
            if runtime_images:
                summary["runtime_service_images"] = runtime_images
        if summary["dashboard_enabled"]:
            summary["dashboard_reachable"] = backend_status.get(
                "dashboard_reachable"
            )
        if summary["monitoring_enabled"]:
            summary["prometheus_reachable"] = backend_status.get(
                "prometheus_reachable"
            )
            summary["grafana_reachable"] = backend_status.get(
                "grafana_reachable"
            )
        if summary["intentkit_enabled"]:
            summary["intentkit_running"] = backend_status.get(
                "intentkit_running"
            )
            summary["intentkit_reachable"] = backend_status.get(
                "intentkit_reachable"
            )
            summary["intentkit_api_reachable"] = backend_status.get(
                "intentkit_api_reachable"
            )
        if summary["shielded_relayer_enabled"]:
            summary["shielded_relayer_running"] = backend_status.get(
                "shielded_relayer_running"
            )
            summary["shielded_relayer_reachable"] = backend_status.get(
                "shielded_relayer_reachable"
            )
    return summary


def _collect_node_status(
    args: argparse.Namespace,
    *,
    check_rpc: bool,
    check_backend: bool = True,
) -> dict:
    context = _resolve_node_context(args)
    profile_path = context["profile_path"]
    profile = context["profile"]
    network_path = context["network_path"]
    network = context["network"]
    runtime_backend = context["runtime_backend"]
    stack_dir = context["stack_dir"]
    home = context["home"]
    node_image_mode, node_integrated_image, node_split_image = (
        _effective_node_image_config(profile, network)
    )
    node_release_manifest = _effective_node_release_manifest(profile, network)
    config_path = home / "config" / "config.toml"
    genesis_path = home / "config" / "genesis.json"
    node_key_path = home / "config" / "node_key.json"
    validator_state_path = home / "data" / "priv_validator_state.json"

    result: dict[str, object] = {
        "profile_path": str(profile_path),
        "network_path": str(network_path),
        "runtime_backend": runtime_backend,
        "home": str(home),
        "initialized": config_path.exists(),
        "config_present": config_path.exists(),
        "genesis_present": genesis_path.exists(),
        "node_key_present": node_key_path.exists(),
        "priv_validator_state_present": validator_state_path.exists(),
        "effective_genesis_source": (
            profile.get("genesis_url")
            or network.get("genesis_source")
            or (
                None
                if network.get("genesis_preset") is None
                else f"preset:{network['genesis_preset']}"
            )
        ),
        "effective_snapshot_url": _resolve_effective_snapshot_url(
            profile=profile,
            network=network,
        ),
        "rpc_checked": check_rpc,
        "profile": {
            "name": args.name,
            "network": profile.get("network"),
            "node_image_mode": node_image_mode,
            "node_integrated_image": node_integrated_image,
            "node_split_image": node_split_image,
            "node_release_manifest": node_release_manifest,
            "service_node": bool(profile.get("service_node")),
            "operator_profile": profile.get("operator_profile"),
            "monitoring_profile": profile.get("monitoring_profile"),
            "dashboard_enabled": bool(profile.get("dashboard_enabled")),
            "monitoring_enabled": bool(profile.get("monitoring_enabled")),
            "intentkit_enabled": bool(profile.get("intentkit_enabled")),
            "intentkit_network_id": profile.get("intentkit_network_id"),
        },
    }
    result["node_release_manifest"] = node_release_manifest
    if stack_dir is not None:
        result["stack_dir"] = str(stack_dir)

    if node_key_path.exists():
        try:
            result["node_id"] = read_json(node_key_path).get("node_id")
        except json.JSONDecodeError:
            result["node_id"] = None

    if (
        runtime_backend == "xian-stack"
        and stack_dir is not None
        and check_backend
    ):
        try:
            backend_status = get_xian_stack_node_status(
                stack_dir=stack_dir,
                cometbft_home=home,
                **_stack_runtime_profile_kwargs(profile, context["network"]),
            )
            result["backend_status"] = backend_status
            result["backend_checked"] = True
            result["backend_running"] = backend_status.get("backend_running")
            if result.get("node_id") is None:
                result["node_id"] = backend_status.get("node_id")
        except Exception as exc:
            result["backend_checked"] = True
            result["backend_error"] = str(exc)

    if check_rpc:
        try:
            result["rpc_status"] = fetch_json(args.rpc_url)
            result["rpc_reachable"] = True
        except Exception as exc:
            result["rpc_reachable"] = False
            result["rpc_error"] = str(exc)

    if isinstance(result.get("backend_status"), dict) and isinstance(
        result["backend_status"].get("endpoints"), dict
    ):
        result["endpoints"] = result["backend_status"]["endpoints"]
    else:
        result["endpoints"] = _fallback_node_endpoints(
            rpc_status_url=args.rpc_url,
            profile=profile,
        )

    result["summary"] = _summarize_node_status(result)
    return result


def _handle_node_status(args: argparse.Namespace) -> int:
    result = _collect_node_status(args, check_rpc=not args.skip_rpc)
    print(json.dumps(result, indent=2))
    return 0


def _handle_node_endpoints(args: argparse.Namespace) -> int:
    result = _collect_node_endpoints(args)
    print(json.dumps(result, indent=2))
    return 0


def _read_rendered_config_toml(home: Path) -> dict[str, object]:
    config_path = home / "config" / "config.toml"
    if not config_path.exists():
        raise FileNotFoundError(f"{config_path} does not exist")
    try:
        return tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in {config_path}") from exc


def _collect_statesync_readiness(home: Path) -> dict[str, object]:
    config = _read_rendered_config_toml(home)
    statesync = config.get("statesync", {})
    enabled = bool(statesync.get("enable"))
    rpc_servers = [
        server.strip()
        for server in str(statesync.get("rpc_servers", "")).split(",")
        if server.strip()
    ]
    trust_height = int(statesync.get("trust_height", 0) or 0)
    trust_hash = str(statesync.get("trust_hash", "") or "")
    trust_period = str(statesync.get("trust_period", "") or "")

    if not enabled:
        state = "disabled"
        ready = False
    else:
        ready = (
            len(rpc_servers) >= 2
            and trust_height > 0
            and bool(trust_hash)
            and bool(trust_period)
        )
        state = "configured" if ready else "incomplete"

    return {
        "enabled": enabled,
        "state": state,
        "ready": ready,
        "rpc_servers": rpc_servers,
        "trust_height": trust_height,
        "trust_hash_present": bool(trust_hash),
        "trust_period": trust_period,
    }


def _collect_node_health(args: argparse.Namespace) -> dict[str, object]:
    context = _resolve_node_context(args)
    profile = context["profile"]
    network = context["network"]
    runtime_backend = context["runtime_backend"]
    stack_dir = context["stack_dir"]
    home = context["home"]
    node_image_mode, node_integrated_image, node_split_image = (
        _effective_node_image_config(profile, network)
    )

    payload: dict[str, object] = {
        "profile_path": str(context["profile_path"]),
        "network_path": str(context["network_path"]),
        "runtime_backend": runtime_backend,
        "node_image_mode": node_image_mode,
        "node_integrated_image": node_integrated_image,
        "node_split_image": node_split_image,
        "home": str(home),
        "service_node": bool(profile.get("service_node")),
        "operator_profile": profile.get("operator_profile"),
        "monitoring_profile": profile.get("monitoring_profile"),
        "dashboard_enabled": bool(profile.get("dashboard_enabled")),
        "monitoring_enabled": bool(profile.get("monitoring_enabled")),
        "intentkit_enabled": bool(profile.get("intentkit_enabled")),
        "intentkit_network_id": profile.get("intentkit_network_id"),
        "effective_snapshot_url": _resolve_effective_snapshot_url(
            profile=profile,
            network=context["network"],
        ),
    }
    if stack_dir is not None:
        payload["stack_dir"] = str(stack_dir)

    try:
        payload["statesync"] = _collect_statesync_readiness(home)
    except Exception as exc:
        payload["statesync"] = {
            "state": "unavailable",
            "error": str(exc),
        }

    if runtime_backend == "xian-stack" and stack_dir is not None:
        payload["health"] = get_xian_stack_node_health(
            stack_dir=stack_dir,
            cometbft_home=home,
            **_stack_runtime_profile_kwargs(profile, network),
            rpc_url=args.rpc_url,
            check_disk=not args.skip_disk_check,
        )
        payload["endpoints"] = payload["health"].get("endpoints", {})
    else:
        payload["endpoints"] = _fallback_node_endpoints(
            rpc_status_url=args.rpc_url,
            profile=profile,
        )
    return payload


def _handle_node_health(args: argparse.Namespace) -> int:
    result = _collect_node_health(args)
    print(json.dumps(result, indent=2))
    return 0


def _handle_snapshot_restore(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    profile_path, profile, _, network = _load_profile_and_network(
        base_dir=base_dir,
        name=args.name,
        profile_arg=args.profile,
        network_arg=args.network,
        configs_dir=args.configs_dir,
    )
    runtime_backend = _resolve_runtime_backend(profile, network)
    stack_dir = _resolve_stack_dir_from_profile(
        base_dir=base_dir,
        profile=profile,
        explicit_stack_dir=args.stack_dir,
    )
    result = _restore_snapshot(
        base_dir=base_dir,
        profile=profile,
        profile_path=profile_path,
        network=network,
        runtime_backend=runtime_backend,
        stack_dir=stack_dir,
        explicit_home=args.home,
        explicit_snapshot_url=args.snapshot_url,
    )
    print(json.dumps(result, indent=2))
    return 0


def _run_check(name: str, fn) -> dict[str, object]:
    try:
        detail = fn()
    except Exception as exc:
        return {
            "name": name,
            "ok": False,
            "detail": str(exc),
        }

    return {
        "name": name,
        "ok": True,
        "detail": detail,
    }


def _doctor_node_artifacts(status: dict[str, object]) -> dict[str, object]:
    missing = []
    if not status.get("config_present"):
        missing.append("config.toml")
    if not status.get("genesis_present"):
        missing.append("genesis.json")
    if not status.get("node_key_present"):
        missing.append("node_key.json")
    if not status.get("priv_validator_state_present"):
        missing.append("priv_validator_state.json")
    if not status.get("initialized") or missing:
        detail = ", ".join(missing) if missing else "node home not initialized"
        raise RuntimeError(detail)
    return {
        "home": status.get("home"),
        "node_id": status.get("node_id"),
    }


def _doctor_backend_check(status: dict[str, object]) -> dict[str, object]:
    if status.get("backend_error"):
        raise RuntimeError(str(status["backend_error"]))
    if not status.get("backend_running"):
        raise RuntimeError("xian-stack backend is not running")
    return {
        "stack_dir": status.get("stack_dir"),
        "backend_running": True,
    }


def _doctor_rpc_check(status: dict[str, object]) -> dict[str, object]:
    if not status.get("rpc_reachable"):
        raise RuntimeError(str(status.get("rpc_error", "RPC is unreachable")))
    return status.get("summary", {})


def _doctor_statesync_check(status: dict[str, object]) -> dict[str, object]:
    readiness = _collect_statesync_readiness(Path(str(status["home"])))
    if readiness["state"] == "incomplete":
        raise RuntimeError(
            "statesync is enabled but trust settings are incomplete"
        )
    return readiness


def _doctor_snapshot_check(status: dict[str, object]) -> dict[str, object]:
    return {
        "effective_snapshot_url": status.get("effective_snapshot_url"),
        "available": bool(status.get("effective_snapshot_url")),
    }


def _doctor_service_check(
    status: dict[str, object],
    *,
    service_name: str,
    reachable_key: str,
    error_key: str,
) -> dict[str, object]:
    backend_status = status.get("backend_status")
    if not isinstance(backend_status, dict):
        raise RuntimeError("backend status is unavailable")
    if not backend_status.get(reachable_key):
        raise RuntimeError(
            str(backend_status.get(error_key, f"{service_name} is unreachable"))
        )
    return {
        "service": service_name,
        "reachable": True,
        "url": status.get("endpoints", {}).get(service_name),
    }


def _handle_doctor(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    checks = [
        _run_check(
            "configs_dir",
            lambda: str(
                resolve_configs_dir(base_dir, explicit=args.configs_dir)
            ),
        ),
        _run_check(
            "stack_dir",
            lambda: str(resolve_stack_dir(base_dir, explicit=args.stack_dir)),
        ),
        _run_check("node_setup", lambda: get_node_setup_module().__name__),
        _run_check("node_admin", lambda: get_node_admin_module().__name__),
        _run_check(
            "genesis_builder",
            lambda: get_genesis_builder_module().__name__,
        ),
    ]

    if args.name is not None:
        status_args = argparse.Namespace(
            name=args.name,
            base_dir=base_dir,
            profile=args.profile,
            network=args.network,
            stack_dir=args.stack_dir,
            configs_dir=args.configs_dir,
            home=args.home,
            rpc_url=args.rpc_url,
            skip_rpc=args.skip_live_checks,
        )
        node_status = _collect_node_status(
            status_args,
            check_rpc=not args.skip_live_checks,
            check_backend=not args.skip_live_checks,
        )
        checks.append(
            _run_check(
                "node_status",
                lambda: node_status,
            )
        )
        checks.append(
            _run_check(
                "node_artifacts",
                lambda: _doctor_node_artifacts(node_status),
            )
        )
        checks.append(
            _run_check(
                "endpoints",
                lambda: node_status.get("endpoints", {}),
            )
        )
        checks.append(
            _run_check(
                "statesync",
                lambda: _doctor_statesync_check(node_status),
            )
        )
        checks.append(
            _run_check(
                "snapshot_bootstrap",
                lambda: _doctor_snapshot_check(node_status),
            )
        )
        if not args.skip_live_checks:
            checks.append(
                _run_check(
                    "backend",
                    lambda: _doctor_backend_check(node_status),
                )
            )
            checks.append(
                _run_check(
                    "rpc",
                    lambda: _doctor_rpc_check(node_status),
                )
            )
            profile = node_status.get("profile", {})
            if profile.get("dashboard_enabled"):
                checks.append(
                    _run_check(
                        "dashboard",
                        lambda: _doctor_service_check(
                            node_status,
                            service_name="dashboard",
                            reachable_key="dashboard_reachable",
                            error_key="dashboard_error",
                        ),
                    )
                )
            if profile.get("monitoring_enabled"):
                checks.append(
                    _run_check(
                        "prometheus",
                        lambda: _doctor_service_check(
                            node_status,
                            service_name="prometheus",
                            reachable_key="prometheus_reachable",
                            error_key="prometheus_error",
                        ),
                    )
                )
            if profile.get("intentkit_enabled"):
                checks.append(
                    _run_check(
                        "intentkit",
                        lambda: _doctor_service_check(
                            node_status,
                            service_name="intentkit",
                            reachable_key="intentkit_reachable",
                            error_key="intentkit_probe_error",
                        ),
                    )
                )
                checks.append(
                    _run_check(
                        "intentkit_api",
                        lambda: _doctor_service_check(
                            node_status,
                            service_name="intentkit_api",
                            reachable_key="intentkit_api_reachable",
                            error_key="intentkit_api_error",
                        ),
                    )
                )
            if profile.get("monitoring_enabled"):
                checks.append(
                    _run_check(
                        "grafana",
                        lambda: _doctor_service_check(
                            node_status,
                            service_name="grafana",
                            reachable_key="grafana_reachable",
                            error_key="grafana_error",
                        ),
                    )
                )

    result = {
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
    }
    if args.name is not None:
        result["node"] = node_status
    print(json.dumps(result, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    return _build_parser()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
