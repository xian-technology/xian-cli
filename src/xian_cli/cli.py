from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

from xian_cli.abci_bridge import get_node_setup_module
from xian_cli.cometbft import generate_validator_material
from xian_cli.models import NetworkManifest, NodeProfile, read_json, write_json
from xian_cli.runtime import (
    default_home_for_backend,
    fetch_json,
    resolve_stack_dir,
    start_xian_stack_node,
    stop_xian_stack_node,
)


def _handle_keys_validator_generate(args: argparse.Namespace) -> int:
    payload = generate_validator_material(args.private_key)

    if args.out_dir is not None:
        out_dir = args.out_dir.resolve()
        write_json(
            out_dir / "priv_validator_key.json",
            payload["priv_validator_key"],
            force=args.force,
        )
        write_json(
            out_dir / "validator_key_info.json", payload, force=args.force
        )

    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def _handle_network_create(args: argparse.Namespace) -> int:
    manifest = NetworkManifest(
        name=args.name,
        chain_id=args.chain_id,
        mode=args.mode,
        runtime_backend=args.runtime_backend,
        genesis_source=args.genesis_source,
        snapshot_url=args.snapshot_url,
        seed_nodes=args.seed or [],
    )
    target = args.output or Path("networks") / f"{args.name}.json"
    write_json(target, manifest.to_dict(), force=args.force)
    print(f"wrote network manifest to {target}")
    return 0


def _handle_network_join(args: argparse.Namespace) -> int:
    profile = NodeProfile(
        name=args.name,
        network=args.network,
        moniker=args.moniker or args.name,
        validator_key_ref=(
            str(args.validator_key_ref)
            if args.validator_key_ref is not None
            else None
        ),
        runtime_backend=args.runtime_backend,
        stack_dir=str(args.stack_dir) if args.stack_dir is not None else None,
        seeds=args.seed or [],
        genesis_url=args.genesis_url,
        snapshot_url=args.snapshot_url,
        service_node=args.service_node,
        home=str(args.home) if args.home is not None else None,
        pruning_enabled=args.enable_pruning,
        blocks_to_keep=args.blocks_to_keep,
    )
    target = args.output or Path("nodes") / f"{args.name}.json"
    write_json(target, profile.to_dict(), force=args.force)
    print(f"wrote node profile to {target}")
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


def _load_profile_and_network(
    *,
    base_dir: Path,
    name: str,
    profile_arg: Path | None,
    network_arg: Path | None,
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

    network_path = network_arg or base_dir / "networks" / f"{network_name}.json"
    if not network_path.is_absolute():
        network_path = (base_dir / network_path).resolve()
    if not network_path.exists():
        raise FileNotFoundError(f"network manifest not found: {network_path}")

    network = read_json(network_path)
    return profile_path, profile, network_path, network


def _handle_node_init(args: argparse.Namespace) -> int:
    node_setup = get_node_setup_module()

    base_dir = args.base_dir.resolve()
    profile_path, profile, network_path, network = _load_profile_and_network(
        base_dir=base_dir,
        name=args.name,
        profile_arg=args.profile,
        network_arg=args.network,
    )
    runtime_backend = profile.get("runtime_backend") or network.get(
        "runtime_backend"
    )
    if runtime_backend != "xian-stack":
        raise ValueError(f"unsupported runtime backend: {runtime_backend}")

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
    genesis_source = network.get("genesis_source") or profile.get("genesis_url")
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

    explicit_home = args.home
    if explicit_home is not None and not explicit_home.is_absolute():
        explicit_home = (base_dir / explicit_home).resolve()

    home = explicit_home or _resolve_path(
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
    print(json.dumps(result, indent=2))
    return 0


def _handle_node_start(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    profile_path, profile, _, network = _load_profile_and_network(
        base_dir=base_dir,
        name=args.name,
        profile_arg=args.profile,
        network_arg=args.network,
    )

    runtime_backend = profile.get("runtime_backend") or network.get(
        "runtime_backend"
    )
    if runtime_backend != "xian-stack":
        raise ValueError(f"unsupported runtime backend: {runtime_backend}")

    stack_dir = resolve_stack_dir(
        base_dir,
        explicit=_resolve_stack_dir_from_profile(
            base_dir=base_dir,
            profile=profile,
            explicit_stack_dir=args.stack_dir,
        ),
    )
    home = _resolve_path(
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
    )

    runtime_backend = profile.get("runtime_backend") or network.get(
        "runtime_backend"
    )
    if runtime_backend != "xian-stack":
        raise ValueError(f"unsupported runtime backend: {runtime_backend}")

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
        "--chain-id", required=True, help="chain identifier"
    )
    create_parser.add_argument(
        "--mode",
        default="join",
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
    create_parser.add_argument("--snapshot-url", help="optional snapshot URL")
    create_parser.add_argument(
        "--seed",
        action="append",
        help="seed in <node_id>@<host>:26656 format; may be repeated",
    )
    create_parser.add_argument(
        "--output",
        type=Path,
        help="output file path; defaults to ./networks/<name>.json",
    )
    create_parser.add_argument("--force", action="store_true")
    create_parser.set_defaults(handler=_handle_network_create)

    join_parser = network_subparsers.add_parser(
        "join", help="create a node profile for joining an existing network"
    )
    join_parser.add_argument("name", help="local profile name")
    join_parser.add_argument(
        "--network",
        required=True,
        help="network manifest name, for example mainnet",
    )
    join_parser.add_argument("--moniker", help="node moniker")
    join_parser.add_argument(
        "--validator-key-ref",
        type=Path,
        help="path to validator_key_info.json or priv_validator_key.json",
    )
    join_parser.add_argument(
        "--runtime-backend",
        default="xian-stack",
        help="runtime backend used for this node",
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
    join_parser.add_argument("--genesis-url", help="URL to fetch genesis from")
    join_parser.add_argument("--snapshot-url", help="optional snapshot URL")
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
        "--home",
        type=Path,
        help="explicit CometBFT home path; overrides the profile home",
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
    stop_parser.set_defaults(handler=_handle_node_stop)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
