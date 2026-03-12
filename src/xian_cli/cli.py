from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from xian_cli.cometbft import generate_validator_material
from xian_cli.models import NetworkManifest, NodeProfile, write_json


def _handle_keys_validator_generate(args: argparse.Namespace) -> int:
    payload = generate_validator_material(args.private_key)

    if args.out_dir is not None:
        out_dir = args.out_dir.resolve()
        write_json(
            out_dir / "priv_validator_key.json",
            payload["priv_validator_key"],
            force=args.force,
        )
        write_json(out_dir / "validator_key_info.json", payload, force=args.force)

    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def _handle_network_create(args: argparse.Namespace) -> int:
    manifest = NetworkManifest(
        name=args.name,
        chain_id=args.chain_id,
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
        chain_id=args.chain_id,
        moniker=args.moniker or args.name,
        seeds=args.seed or [],
        genesis_url=args.genesis_url,
        snapshot_url=args.snapshot_url,
        service_node=args.service_node,
        home=str(args.home) if args.home is not None else None,
    )
    target = args.output or Path("nodes") / f"{args.name}.json"
    write_json(target, profile.to_dict(), force=args.force)
    print(f"wrote node profile to {target}")
    return 0


def _handle_node_start(_: argparse.Namespace) -> int:
    print(
        "node start is not implemented yet; this command will eventually bridge "
        "xian-cli profiles into xian-stack runtime operations"
    )
    return 2


def _handle_node_stop(_: argparse.Namespace) -> int:
    print(
        "node stop is not implemented yet; this command will eventually bridge "
        "xian-cli profiles into xian-stack runtime operations"
    )
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xian",
        description="Operator CLI for Xian networks and nodes",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    keys_parser = subparsers.add_parser("keys", help="key management")
    keys_subparsers = keys_parser.add_subparsers(dest="keys_command", required=True)

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
        help="existing 64-character hex private key; omit to generate a new one",
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
    create_parser.add_argument("--chain-id", required=True, help="chain identifier")
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
    join_parser.add_argument("--chain-id", required=True, help="chain identifier")
    join_parser.add_argument("--moniker", help="node moniker")
    join_parser.add_argument(
        "--seed",
        action="append",
        help="seed in <node_id>@<host>:26656 format; may be repeated",
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
        "--output",
        type=Path,
        help="output file path; defaults to ./nodes/<name>.json",
    )
    join_parser.add_argument("--force", action="store_true")
    join_parser.set_defaults(handler=_handle_network_join)

    node_parser = subparsers.add_parser("node", help="node lifecycle")
    node_subparsers = node_parser.add_subparsers(dest="node_command", required=True)

    start_parser = node_subparsers.add_parser("start", help="start a node")
    start_parser.set_defaults(handler=_handle_node_start)

    stop_parser = node_subparsers.add_parser("stop", help="stop a node")
    stop_parser.set_defaults(handler=_handle_node_stop)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())

