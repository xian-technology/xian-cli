from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from xian_cli.abci_bridge import (
    get_genesis_builder_module,
    get_node_admin_module,
    get_node_setup_module,
)
from xian_cli.cometbft import generate_validator_material
from xian_cli.config_repo import (
    resolve_configs_dir,
    resolve_network_manifest_path,
)
from xian_cli.models import NetworkManifest, NodeProfile, read_json, write_json
from xian_cli.runtime import (
    default_home_for_backend,
    fetch_json,
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
    payload = generate_validator_material(private_key)
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


def _handle_keys_validator_generate(args: argparse.Namespace) -> int:
    if args.out_dir is not None:
        metadata_path = _write_validator_material_files(
            out_dir=args.out_dir.resolve(),
            private_key=args.private_key,
            force=args.force,
        )
        payload = read_json(metadata_path)
    else:
        payload = generate_validator_material(args.private_key)

    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def _handle_network_create(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
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
        raise ValueError("--init-node requires --bootstrap-node")

    validator_key_ref: str | None = None
    validator_key_payload: dict | None = None
    if args.validator_key_ref is not None:
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
    elif args.generate_validator_key:
        key_dir = args.validator_key_dir or base_dir / "keys" / (
            args.bootstrap_node or args.name
        )
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

    genesis_source = args.genesis_source
    generated_genesis_path: Path | None = None
    if genesis_source is None:
        if validator_key_payload is None:
            if args.bootstrap_node is not None:
                raise ValueError(
                    "bootstrap network creation without --genesis-source "
                    "requires validator key material; pass "
                    "--generate-validator-key or --validator-key-ref"
                )
        else:
            founder_private_key = (
                args.founder_private_key
                or _extract_validator_private_key_hex(validator_key_payload)
            )
            generated_genesis_path = network_dir / "genesis.json"
            genesis = _build_creation_genesis(
                chain_id=args.chain_id,
                founder_private_key=founder_private_key,
                validator_key_payload=validator_key_payload,
                genesis_preset=args.genesis_preset,
                validator_name=args.bootstrap_node or args.name,
                validator_power=args.validator_power,
            )
            write_json(generated_genesis_path, genesis, force=args.force)
            genesis_source = "./genesis.json"
    if (
        args.bootstrap_node is not None
        and genesis_source is None
        and validator_key_payload is None
    ):
        raise ValueError(
            "--bootstrap-node requires either --genesis-source or local "
            "genesis generation via validator key material"
        )

    manifest = NetworkManifest(
        name=args.name,
        chain_id=args.chain_id,
        mode=args.mode,
        runtime_backend=args.runtime_backend,
        genesis_source=genesis_source,
        snapshot_url=args.snapshot_url,
        seed_nodes=args.seed or [],
    )
    write_json(target, manifest.to_dict(), force=args.force)

    result: dict[str, object] = {
        "manifest_path": str(target),
        "mode": args.mode,
        "genesis_source": genesis_source,
    }
    if generated_genesis_path is not None:
        result["generated_genesis_path"] = str(generated_genesis_path)
    if validator_key_ref is not None:
        result["validator_key_ref"] = validator_key_ref

    if args.bootstrap_node is not None:
        if validator_key_ref is None:
            raise ValueError(
                "--bootstrap-node requires validator key material; "
                "pass --generate-validator-key or --validator-key-ref"
            )
        profile_path = _resolve_output_path(
            base_dir=base_dir,
            explicit_output=args.node_output,
            default_path=base_dir / "nodes" / f"{args.bootstrap_node}.json",
        )
        profile = NodeProfile(
            name=args.bootstrap_node,
            network=args.name,
            moniker=args.moniker or args.bootstrap_node,
            validator_key_ref=validator_key_ref,
            runtime_backend=args.runtime_backend,
            stack_dir=(
                str(args.stack_dir) if args.stack_dir is not None else None
            ),
            seeds=[],
            genesis_url=None,
            snapshot_url=args.snapshot_url,
            service_node=args.service_node,
            home=str(args.home) if args.home is not None else None,
            pruning_enabled=args.enable_pruning,
            blocks_to_keep=args.blocks_to_keep,
        )
        write_json(profile_path, profile.to_dict(), force=args.force)
        result["profile_path"] = str(profile_path)

        if args.init_node:
            init_args = argparse.Namespace(
                name=args.bootstrap_node,
                base_dir=base_dir,
                profile=profile_path,
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
    network_path = resolve_network_manifest_path(
        base_dir=base_dir,
        network_name=args.network,
        explicit_manifest=args.network_manifest,
        configs_dir=args.configs_dir,
    )
    network = read_json(network_path)
    runtime_backend = (
        args.runtime_backend or network.get("runtime_backend") or "xian-stack"
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
        stack_dir=str(args.stack_dir) if args.stack_dir is not None else None,
        seeds=args.seed or [],
        genesis_url=args.genesis_url,
        snapshot_url=args.snapshot_url,
        service_node=args.service_node,
        home=str(args.home) if args.home is not None else None,
        pruning_enabled=args.enable_pruning,
        blocks_to_keep=args.blocks_to_keep,
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


def _build_creation_genesis(
    *,
    chain_id: str,
    founder_private_key: str,
    validator_key_payload: dict,
    genesis_preset: str,
    validator_name: str,
    validator_power: int,
) -> dict:
    genesis_builder = get_genesis_builder_module()
    return genesis_builder.build_single_validator_genesis(
        chain_id=chain_id,
        founder_private_key=founder_private_key,
        priv_validator_key=_extract_priv_validator_key(validator_key_payload),
        network=genesis_preset,
        validator_name=validator_name,
        validator_power=validator_power,
    )


def _resolve_runtime_backend(profile: dict, network: dict) -> str:
    runtime_backend = profile.get("runtime_backend") or network.get(
        "runtime_backend"
    )
    if runtime_backend != "xian-stack":
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
    if not snapshot_url:
        raise ValueError(
            "no snapshot source configured; "
            "set snapshot_url in the network manifest or node profile"
        )

    node_admin = get_node_admin_module()
    snapshot_archive_name = node_admin.apply_snapshot_archive(
        snapshot_url, home
    )
    return {
        "home": str(home),
        "snapshot_url": snapshot_url,
        "snapshot_archive_name": snapshot_archive_name,
    }


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

    profile = read_json(profile_path)
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
    network = read_json(network_path)
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
    genesis_source = profile.get("genesis_url") or network.get("genesis_source")
    if not genesis_source:
        raise ValueError(
            "no genesis source configured; "
            "set genesis_source in the network manifest"
        )

    genesis = _load_genesis_payload(
        genesis_source,
        base_dir=base_dir,
        manifest_path=network_path,
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
        service_node=bool(profile.get("service_node")),
        wait_for_rpc=not args.skip_health_check,
        rpc_timeout_seconds=args.rpc_timeout_seconds,
    )
    print(json.dumps(result, indent=2))
    return 0


def _handle_node_stop(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    _, profile, _, network = _load_profile_and_network(
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
        service_node=bool(profile.get("service_node")),
    )
    print(json.dumps(result, indent=2))
    return 0


def _collect_node_status(
    args: argparse.Namespace,
    *,
    check_rpc: bool,
) -> dict:
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
    home = _resolve_home(
        base_dir=base_dir,
        profile=profile,
        profile_path=profile_path,
        runtime_backend=runtime_backend,
        stack_dir=stack_dir,
        explicit_home=getattr(args, "home", None),
    )
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
            profile.get("genesis_url") or network.get("genesis_source")
        ),
        "effective_snapshot_url": _resolve_effective_snapshot_url(
            profile=profile,
            network=network,
        ),
        "rpc_checked": check_rpc,
    }
    if stack_dir is not None:
        result["stack_dir"] = str(stack_dir)

    if node_key_path.exists():
        try:
            result["node_id"] = read_json(node_key_path).get("node_id")
        except json.JSONDecodeError:
            result["node_id"] = None

    if check_rpc:
        try:
            result["rpc_status"] = fetch_json(args.rpc_url)
            result["rpc_reachable"] = True
        except Exception as exc:
            result["rpc_reachable"] = False
            result["rpc_error"] = str(exc)

    return result


def _handle_node_status(args: argparse.Namespace) -> int:
    result = _collect_node_status(args, check_rpc=not args.skip_rpc)
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
            skip_rpc=True,
        )
        checks.append(
            _run_check(
                "node_status",
                lambda: _collect_node_status(status_args, check_rpc=False),
            )
        )

    result = {
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
    }
    print(json.dumps(result, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xian",
        description="Operator CLI for Xian networks and nodes",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    keys_parser = subparsers.add_parser("keys", help="key management")
    keys_subparsers = keys_parser.add_subparsers(
        dest="keys_command", required=True
    )

    validator_parser = keys_subparsers.add_parser(
        "validator", help="validator key management"
    )
    validator_subparsers = validator_parser.add_subparsers(
        dest="validator_command", required=True
    )

    generate_parser = validator_subparsers.add_parser(
        "generate", help="generate validator key material"
    )
    generate_parser.add_argument(
        "--private-key",
        help=(
            "existing 64-character hex private key; omit to generate a new one"
        ),
    )
    generate_parser.add_argument(
        "--out-dir",
        type=Path,
        help="directory to write priv_validator_key.json and metadata to",
    )
    generate_parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing output files",
    )
    generate_parser.set_defaults(handler=_handle_keys_validator_generate)

    network_parser = subparsers.add_parser("network", help="network manifests")
    network_subparsers = network_parser.add_subparsers(
        dest="network_command", required=True
    )

    create_parser = network_subparsers.add_parser(
        "create", help="create a new network manifest"
    )
    create_parser.add_argument("name", help="network name")
    create_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "workspace directory that contains ./networks, ./nodes, "
            "./keys, and optionally sibling repos"
        ),
    )
    create_parser.add_argument(
        "--chain-id", required=True, help="chain identifier"
    )
    create_parser.add_argument(
        "--mode",
        default="create",
        choices=["join", "create"],
        help=(
            "whether this manifest describes joining an existing network "
            "or creating a new one"
        ),
    )
    create_parser.add_argument(
        "--runtime-backend",
        default="xian-stack",
        help="runtime backend used for this network",
    )
    create_parser.add_argument(
        "--genesis-source",
        help="path or URL for the genesis source used to bootstrap the network",
    )
    create_parser.add_argument(
        "--genesis-preset",
        default="local",
        help=(
            "genesis contract preset used when generating a local genesis "
            "file; defaults to the universal local preset"
        ),
    )
    create_parser.add_argument(
        "--founder-private-key",
        help=(
            "64-character hex private key for the founder account; defaults "
            "to the validator private key when a validator key is available"
        ),
    )
    create_parser.add_argument(
        "--validator-key-ref",
        type=Path,
        help=(
            "path to validator_key_info.json or priv_validator_key.json for "
            "the initial validator"
        ),
    )
    create_parser.add_argument(
        "--generate-validator-key",
        action="store_true",
        help="generate validator key material for the initial validator",
    )
    create_parser.add_argument(
        "--validator-key-dir",
        type=Path,
        help=(
            "output directory for generated validator key material; defaults "
            "to ./keys/<bootstrap-node-or-network-name>"
        ),
    )
    create_parser.add_argument(
        "--validator-power",
        type=int,
        default=10,
        help="voting power for the generated initial validator entry",
    )
    create_parser.add_argument("--snapshot-url", help="optional snapshot URL")
    create_parser.add_argument(
        "--seed",
        action="append",
        help="seed in <node_id>@<host>:26656 format; may be repeated",
    )
    create_parser.add_argument(
        "--bootstrap-node",
        help=(
            "create an initial node profile for this network using the "
            "generated or referenced validator key"
        ),
    )
    create_parser.add_argument(
        "--node-output",
        type=Path,
        help="output file path for the bootstrap node profile",
    )
    create_parser.add_argument("--moniker", help="bootstrap node moniker")
    create_parser.add_argument(
        "--init-node",
        action="store_true",
        help="run node initialization immediately after writing the profile",
    )
    create_parser.add_argument(
        "--stack-dir",
        type=Path,
        help="path to the xian-stack checkout for the xian-stack backend",
    )
    create_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path; defaults to XIAN_CONFIGS_DIR "
            "or the sibling workspace layout"
        ),
    )
    create_parser.add_argument(
        "--home",
        type=Path,
        help="bootstrap node home directory, for example ~/.cometbft",
    )
    create_parser.add_argument(
        "--service-node",
        action="store_true",
        help="mark the bootstrap node profile as a service node",
    )
    create_parser.add_argument(
        "--enable-pruning",
        action="store_true",
        help="enable pruning for the bootstrap node",
    )
    create_parser.add_argument(
        "--blocks-to-keep",
        type=int,
        default=100000,
        help="number of blocks to retain when pruning is enabled",
    )
    create_parser.add_argument(
        "--output",
        type=Path,
        help=(
            "manifest output path; defaults to ./networks/<name>/manifest.json"
        ),
    )
    create_parser.add_argument("--force", action="store_true")
    create_parser.set_defaults(handler=_handle_network_create)

    join_parser = network_subparsers.add_parser(
        "join", help="create a node profile for joining an existing network"
    )
    join_parser.add_argument("name", help="local profile name")
    join_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "workspace directory that contains ./nodes, ./networks, "
            "and optionally ./xian-configs"
        ),
    )
    join_parser.add_argument(
        "--network",
        required=True,
        help="network manifest name, for example mainnet",
    )
    join_parser.add_argument(
        "--network-manifest",
        type=Path,
        help=(
            "explicit network manifest path; overrides local and canonical "
            "lookup"
        ),
    )
    join_parser.add_argument("--moniker", help="node moniker")
    join_parser.add_argument(
        "--validator-key-ref",
        type=Path,
        help=(
            "path to validator_key_info.json or priv_validator_key.json; "
            "omit this and pass --generate-validator-key to create one"
        ),
    )
    join_parser.add_argument(
        "--generate-validator-key",
        action="store_true",
        help="generate validator key material into ./keys/<name> by default",
    )
    join_parser.add_argument(
        "--validator-key-dir",
        type=Path,
        help=(
            "output directory for generated validator key material; defaults "
            "to ./keys/<name>"
        ),
    )
    join_parser.add_argument(
        "--runtime-backend",
        help=(
            "node-local runtime backend override; defaults to the network "
            "manifest value"
        ),
    )
    join_parser.add_argument(
        "--stack-dir",
        type=Path,
        help="path to the xian-stack checkout for the xian-stack backend",
    )
    join_parser.add_argument(
        "--seed",
        action="append",
        help=(
            "optional node-local seed override in "
            "<node_id>@<host>:26656 format; may be repeated"
        ),
    )
    join_parser.add_argument(
        "--genesis-url",
        help=(
            "node-local genesis URL override; when set it takes precedence "
            "over the network manifest genesis_source"
        ),
    )
    join_parser.add_argument(
        "--snapshot-url",
        help="node-local snapshot URL override",
    )
    join_parser.add_argument(
        "--init-node",
        action="store_true",
        help="run node initialization immediately after writing the profile",
    )
    join_parser.add_argument(
        "--restore-snapshot",
        action="store_true",
        help=(
            "when used with --init-node, restore the effective snapshot URL "
            "after initializing the node home"
        ),
    )
    join_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path; defaults to XIAN_CONFIGS_DIR "
            "or the sibling workspace layout"
        ),
    )
    join_parser.add_argument(
        "--home",
        type=Path,
        help="node home directory, for example ~/.cometbft",
    )
    join_parser.add_argument(
        "--service-node",
        action="store_true",
        help="mark the node profile as a service node",
    )
    join_parser.add_argument(
        "--enable-pruning",
        action="store_true",
        help="enable pruning for this node",
    )
    join_parser.add_argument(
        "--blocks-to-keep",
        type=int,
        default=100000,
        help="number of blocks to retain when pruning is enabled",
    )
    join_parser.add_argument(
        "--output",
        type=Path,
        help="output file path; defaults to ./nodes/<name>.json",
    )
    join_parser.add_argument("--force", action="store_true")
    join_parser.set_defaults(handler=_handle_network_join)

    node_parser = subparsers.add_parser("node", help="node lifecycle")
    node_subparsers = node_parser.add_subparsers(
        dest="node_command", required=True
    )

    init_parser = node_subparsers.add_parser(
        "init", help="materialize a node home from manifests and keys"
    )
    init_parser.add_argument("name", help="node profile name")
    init_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help="workspace directory that contains ./nodes and ./networks",
    )
    init_parser.add_argument(
        "--profile",
        type=Path,
        help="explicit node profile path; defaults to ./nodes/<name>.json",
    )
    init_parser.add_argument(
        "--network",
        type=Path,
        help=(
            "explicit network manifest path; defaults to "
            "./networks/<profile.network>.json"
        ),
    )
    init_parser.add_argument(
        "--validator-key",
        type=Path,
        help=(
            "explicit validator key path; "
            "overrides validator_key_ref in the profile"
        ),
    )
    init_parser.add_argument(
        "--stack-dir",
        type=Path,
        help=(
            "explicit xian-stack checkout path; "
            "overrides stack_dir in the profile"
        ),
    )
    init_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path; defaults to XIAN_CONFIGS_DIR "
            "or the sibling workspace layout"
        ),
    )
    init_parser.add_argument(
        "--home",
        type=Path,
        help="explicit CometBFT home path; overrides the profile home",
    )
    init_parser.add_argument(
        "--snapshot-url",
        help="explicit snapshot URL override",
    )
    init_parser.add_argument(
        "--restore-snapshot",
        action="store_true",
        help="restore the effective snapshot URL after node initialization",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "overwrite config, genesis, and priv_validator_key.json "
            "if they already exist"
        ),
    )
    init_parser.set_defaults(handler=_handle_node_init)

    start_parser = node_subparsers.add_parser("start", help="start a node")
    start_parser.add_argument("name", help="node profile name")
    start_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "workspace directory that contains ./nodes, ./networks, "
            "and ./xian-stack"
        ),
    )
    start_parser.add_argument(
        "--profile",
        type=Path,
        help="explicit node profile path; defaults to ./nodes/<name>.json",
    )
    start_parser.add_argument(
        "--network",
        type=Path,
        help=(
            "explicit network manifest path; defaults to "
            "./networks/<profile.network>.json"
        ),
    )
    start_parser.add_argument(
        "--stack-dir",
        type=Path,
        help=(
            "explicit xian-stack checkout path; "
            "overrides stack_dir in the profile"
        ),
    )
    start_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path; defaults to XIAN_CONFIGS_DIR "
            "or the sibling workspace layout"
        ),
    )
    start_parser.add_argument(
        "--skip-health-check",
        action="store_true",
        help="start the node without waiting for the RPC health check",
    )
    start_parser.add_argument(
        "--rpc-timeout-seconds",
        type=float,
        default=30.0,
        help="time to wait for the local RPC status endpoint",
    )
    start_parser.set_defaults(handler=_handle_node_start)

    stop_parser = node_subparsers.add_parser("stop", help="stop a node")
    stop_parser.add_argument("name", help="node profile name")
    stop_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "workspace directory that contains ./nodes, ./networks, "
            "and ./xian-stack"
        ),
    )
    stop_parser.add_argument(
        "--profile",
        type=Path,
        help="explicit node profile path; defaults to ./nodes/<name>.json",
    )
    stop_parser.add_argument(
        "--network",
        type=Path,
        help=(
            "explicit network manifest path; defaults to "
            "./networks/<profile.network>.json"
        ),
    )
    stop_parser.add_argument(
        "--stack-dir",
        type=Path,
        help=(
            "explicit xian-stack checkout path; "
            "overrides stack_dir in the profile"
        ),
    )
    stop_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path; defaults to XIAN_CONFIGS_DIR "
            "or the sibling workspace layout"
        ),
    )
    stop_parser.set_defaults(handler=_handle_node_stop)

    status_parser = node_subparsers.add_parser(
        "status", help="inspect node bootstrap and RPC status"
    )
    status_parser.add_argument("name", help="node profile name")
    status_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "workspace directory that contains ./nodes, ./networks, "
            "and optionally sibling repos"
        ),
    )
    status_parser.add_argument(
        "--profile",
        type=Path,
        help="explicit node profile path; defaults to ./nodes/<name>.json",
    )
    status_parser.add_argument(
        "--network",
        type=Path,
        help=(
            "explicit network manifest path; defaults to "
            "./networks/<profile.network>/manifest.json"
        ),
    )
    status_parser.add_argument(
        "--stack-dir",
        type=Path,
        help=(
            "explicit xian-stack checkout path; "
            "overrides stack_dir in the profile"
        ),
    )
    status_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path; defaults to XIAN_CONFIGS_DIR "
            "or the sibling workspace layout"
        ),
    )
    status_parser.add_argument(
        "--home",
        type=Path,
        help="explicit CometBFT home path; overrides the profile home",
    )
    status_parser.add_argument(
        "--rpc-url",
        default="http://127.0.0.1:26657/status",
        help="RPC status endpoint used for readiness inspection",
    )
    status_parser.add_argument(
        "--skip-rpc",
        action="store_true",
        help="skip the live RPC status probe",
    )
    status_parser.set_defaults(handler=_handle_node_status)

    snapshot_parser = subparsers.add_parser("snapshot", help="snapshot tools")
    snapshot_subparsers = snapshot_parser.add_subparsers(
        dest="snapshot_command", required=True
    )

    restore_parser = snapshot_subparsers.add_parser(
        "restore",
        help="restore a node snapshot into an initialized home",
    )
    restore_parser.add_argument("name", help="node profile name")
    restore_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "workspace directory that contains ./nodes, ./networks, "
            "and optionally ./xian-configs"
        ),
    )
    restore_parser.add_argument(
        "--profile",
        type=Path,
        help="explicit node profile path; defaults to ./nodes/<name>.json",
    )
    restore_parser.add_argument(
        "--network",
        type=Path,
        help=(
            "explicit network manifest path; defaults to "
            "./networks/<profile.network>.json"
        ),
    )
    restore_parser.add_argument(
        "--stack-dir",
        type=Path,
        help=(
            "explicit xian-stack checkout path; "
            "overrides stack_dir in the profile"
        ),
    )
    restore_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path; defaults to XIAN_CONFIGS_DIR "
            "or the sibling workspace layout"
        ),
    )
    restore_parser.add_argument(
        "--home",
        type=Path,
        help="explicit CometBFT home path; overrides the profile home",
    )
    restore_parser.add_argument(
        "--snapshot-url",
        help="explicit snapshot URL override",
    )
    restore_parser.set_defaults(handler=_handle_snapshot_restore)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="check workspace and optional node prerequisites",
    )
    doctor_parser.add_argument(
        "name",
        nargs="?",
        help=(
            "optional node profile name to inspect in addition to "
            "workspace checks"
        ),
    )
    doctor_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "workspace directory that contains ./nodes, ./networks, "
            "and optionally sibling repos"
        ),
    )
    doctor_parser.add_argument(
        "--profile",
        type=Path,
        help="explicit node profile path; defaults to ./nodes/<name>.json",
    )
    doctor_parser.add_argument(
        "--network",
        type=Path,
        help=(
            "explicit network manifest path; defaults to "
            "./networks/<profile.network>/manifest.json"
        ),
    )
    doctor_parser.add_argument(
        "--stack-dir",
        type=Path,
        help="explicit xian-stack checkout path",
    )
    doctor_parser.add_argument(
        "--configs-dir",
        type=Path,
        help="explicit xian-configs checkout path",
    )
    doctor_parser.add_argument(
        "--home",
        type=Path,
        help="explicit CometBFT home path; overrides the profile home",
    )
    doctor_parser.add_argument(
        "--rpc-url",
        default="http://127.0.0.1:26657/status",
        help="RPC status endpoint for node inspection",
    )
    doctor_parser.set_defaults(handler=_handle_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
