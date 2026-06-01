from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from xian_cli.client.parser import register_client_commands
from xian_cli.models import (
    SUPPORTED_APP_LOG_LEVELS,
    SUPPORTED_BLOCK_POLICY_MODES,
    SUPPORTED_INTENTKIT_NETWORK_IDS,
    SUPPORTED_NODE_IMAGE_MODES,
)
from xian_cli.runtime import DEFAULT_RPC_TIMEOUT_SECONDS


def _package_version() -> str:
    try:
        return version("xian-tech-cli")
    except PackageNotFoundError:
        return "unknown"


def add_node_profile_runtime_args(
    parser: argparse.ArgumentParser,
    *,
    subject: str,
) -> None:
    parser.add_argument(
        "--enable-bds",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=f"enable Blockchain Data Service indexing for {subject}",
    )
    parser.add_argument(
        "--enable-pruning",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=f"enable pruning for {subject}",
    )
    parser.add_argument(
        "--blocks-to-keep",
        type=int,
        help="number of blocks to retain when pruning is enabled",
    )
    parser.add_argument(
        "--block-policy-mode",
        choices=sorted(SUPPORTED_BLOCK_POLICY_MODES),
        help=(
            "block production policy: on_demand waits for transactions, "
            "idle_interval emits empty blocks after an idle interval, "
            "periodic enables scheduled empty blocks"
        ),
    )
    parser.add_argument(
        "--block-policy-interval",
        type=str,
        help="idle or periodic block interval, for example 10s",
    )
    parser.add_argument(
        "--transaction-trace-logging",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=f"emit per-transaction debug summaries in {subject}",
    )
    parser.add_argument(
        "--app-log-level",
        choices=sorted(SUPPORTED_APP_LOG_LEVELS),
        type=str,
        help=f"application log level for {subject}",
    )
    parser.add_argument(
        "--app-log-json",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=f"emit structured JSON application logs in {subject}",
    )
    parser.add_argument(
        "--app-log-rotation-hours",
        type=int,
        help=f"rotate application log files after this many hours in {subject}",
    )
    parser.add_argument(
        "--app-log-retention-days",
        type=int,
        help=f"retain rotated application logs for this many days in {subject}",
    )
    parser.add_argument(
        "--simulation-enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=f"enable readonly transaction simulation in {subject}",
    )
    parser.add_argument(
        "--simulation-max-concurrency",
        type=int,
        help=f"maximum concurrent simulation requests accepted by {subject}",
    )
    parser.add_argument(
        "--simulation-timeout-ms",
        type=int,
        help=f"simulation timeout in milliseconds for {subject}",
    )
    parser.add_argument(
        "--simulation-max-chi",
        type=int,
        help=f"chi budget cap used for readonly simulation in {subject}",
    )
    parser.add_argument(
        "--parallel-execution-enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=f"enable speculative parallel block execution in {subject}",
    )
    parser.add_argument(
        "--parallel-execution-workers",
        type=int,
        help=f"speculative execution worker count for {subject}",
    )
    parser.add_argument(
        "--parallel-execution-min-transactions",
        type=int,
        help=(f"minimum transactions in a block before parallel execution is used in {subject}"),
    )
    parser.add_argument(
        "--enable-dashboard",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=f"start the optional dashboard alongside {subject}",
    )
    parser.add_argument(
        "--enable-monitoring",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=f"start the optional Prometheus and Grafana stack for {subject}",
    )
    parser.add_argument(
        "--dashboard-host",
        type=str,
        help="host interface to bind for the dashboard publish port",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        help="host port to publish for the dashboard",
    )
    parser.add_argument(
        "--enable-intentkit",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=f"start the optional xian-intentkit stack for {subject}",
    )
    parser.add_argument(
        "--intentkit-network-id",
        choices=sorted(SUPPORTED_INTENTKIT_NETWORK_IDS),
        help="xian-intentkit Xian network slot for this profile",
    )
    parser.add_argument(
        "--intentkit-host",
        type=str,
        help="host interface to bind for the xian-intentkit frontend port",
    )
    parser.add_argument(
        "--intentkit-port",
        type=int,
        help="host port to publish for the xian-intentkit frontend",
    )
    parser.add_argument(
        "--intentkit-api-port",
        type=int,
        help="host port to publish for the xian-intentkit API",
    )
    parser.add_argument(
        "--enable-dex-automation",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="start the optional deterministic DEX automation sidecar",
    )
    parser.add_argument(
        "--dex-automation-host",
        type=str,
        help="host interface to bind for the DEX automation web UI",
    )
    parser.add_argument(
        "--dex-automation-port",
        type=int,
        help="host port to publish for the DEX automation web UI",
    )
    parser.add_argument(
        "--dex-automation-config",
        type=str,
        help="stack-managed DEX automation config path override",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xian",
        description="Operator CLI for Xian networks and nodes",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_package_version()}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    register_client_commands(subparsers)

    keys_parser = subparsers.add_parser("keys", help="key management")
    keys_subparsers = keys_parser.add_subparsers(dest="keys_command", required=True)

    validator_parser = keys_subparsers.add_parser("validator", help="validator key management")
    validator_subparsers = validator_parser.add_subparsers(dest="validator_command", required=True)

    generate_parser = validator_subparsers.add_parser(
        "generate", help="generate validator key material"
    )
    generate_parser.add_argument(
        "--private-key",
        help=("existing 64-character hex private key; omit to generate a new one"),
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
    generate_parser.set_defaults(handler_name="_handle_keys_validator_generate")

    network_parser = subparsers.add_parser("network", help="network manifests")
    network_subparsers = network_parser.add_subparsers(dest="network_command", required=True)

    template_parser = network_subparsers.add_parser(
        "template", help="inspect canonical network templates"
    )
    template_subparsers = template_parser.add_subparsers(
        dest="network_template_command", required=True
    )

    template_list_parser = template_subparsers.add_parser(
        "list", help="list available network templates"
    )
    template_list_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "workspace directory that may contain local ./templates and optionally sibling repos"
        ),
    )
    template_list_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path; defaults to XIAN_CONFIGS_DIR "
            "or the sibling workspace layout"
        ),
    )
    template_list_parser.set_defaults(handler_name="_handle_network_template_list")

    template_show_parser = template_subparsers.add_parser("show", help="show one network template")
    template_show_parser.add_argument("name", help="template name")
    template_show_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "workspace directory that may contain local ./templates and optionally sibling repos"
        ),
    )
    template_show_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path; defaults to XIAN_CONFIGS_DIR "
            "or the sibling workspace layout"
        ),
    )
    template_show_parser.set_defaults(handler_name="_handle_network_template_show")

    contract_pack_parser = subparsers.add_parser(
        "contract-pack",
        help="inspect, validate, and install reusable contract packs",
    )
    contract_pack_subparsers = contract_pack_parser.add_subparsers(
        dest="contract_pack_command", required=True
    )

    contract_pack_list_parser = contract_pack_subparsers.add_parser(
        "list", help="list available contract packs"
    )
    contract_pack_list_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "workspace directory that may contain local ./contract-packs and "
            "optionally sibling repos"
        ),
    )
    contract_pack_list_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path; defaults to XIAN_CONFIGS_DIR "
            "or the sibling workspace layout"
        ),
    )
    contract_pack_list_parser.set_defaults(handler_name="_handle_contract_pack_list")

    contract_pack_show_parser = contract_pack_subparsers.add_parser(
        "show", help="show one contract pack"
    )
    contract_pack_show_parser.add_argument("name", help="contract pack name")
    contract_pack_show_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "workspace directory that may contain local ./contract-packs and "
            "optionally sibling repos"
        ),
    )
    contract_pack_show_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path; defaults to XIAN_CONFIGS_DIR "
            "or the sibling workspace layout"
        ),
    )
    contract_pack_show_parser.set_defaults(handler_name="_handle_contract_pack_show")

    contract_pack_validate_parser = contract_pack_subparsers.add_parser(
        "validate", help="validate one contract pack and its bundles"
    )
    contract_pack_validate_parser.add_argument("name", help="contract pack name")
    contract_pack_validate_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "workspace directory that may contain local ./contract-packs and "
            "optionally sibling repos"
        ),
    )
    contract_pack_validate_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path; defaults to XIAN_CONFIGS_DIR "
            "or the sibling workspace layout"
        ),
    )
    contract_pack_validate_parser.set_defaults(handler_name="_handle_contract_pack_validate")

    contract_pack_install_parser = contract_pack_subparsers.add_parser(
        "install", help="install a contract pack onto a running network"
    )
    contract_pack_install_parser.add_argument("name", help="contract pack name")
    contract_pack_install_parser.add_argument(
        "--recipe",
        help="contract pack recipe; defaults to the contract pack default",
    )
    contract_pack_install_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "workspace directory that may contain local ./contract-packs and "
            "optionally sibling repos"
        ),
    )
    contract_pack_install_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path; defaults to XIAN_CONFIGS_DIR "
            "or the sibling workspace layout"
        ),
    )
    contract_pack_install_parser.add_argument(
        "--stack-dir",
        type=Path,
        help="explicit xian-stack checkout path for stack-backed installers",
    )
    contract_pack_install_parser.add_argument(
        "--repo-dir",
        type=Path,
        help="explicit owning repo checkout path for external installers",
    )
    contract_pack_install_parser.add_argument("--rpc-url")
    contract_pack_install_parser.add_argument("--chain-id")
    contract_pack_install_parser.add_argument("--deployer-private-key")
    contract_pack_install_parser.add_argument(
        "--top-up-liquidity",
        action="store_true",
        help=("top up the local DEX demo pool when the selected recipe supports it"),
    )
    contract_pack_install_parser.add_argument(
        "--emit-test-swap",
        action="store_true",
        help="emit a small local DEX swap after installation",
    )
    contract_pack_install_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved installer command without executing it",
    )
    contract_pack_install_parser.set_defaults(handler_name="_handle_contract_pack_install")

    example_parser = subparsers.add_parser(
        "example",
        help="inspect application and operator examples",
    )
    example_subparsers = example_parser.add_subparsers(dest="example_command", required=True)

    example_list_parser = example_subparsers.add_parser("list", help="list available examples")
    example_list_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=("workspace directory that may contain local ./examples and optionally sibling repos"),
    )
    example_list_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path; defaults to XIAN_CONFIGS_DIR "
            "or the sibling workspace layout"
        ),
    )
    example_list_parser.set_defaults(handler_name="_handle_example_list")

    example_show_parser = example_subparsers.add_parser("show", help="show one example")
    example_show_parser.add_argument("name", help="example name")
    example_show_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=("workspace directory that may contain local ./examples and optionally sibling repos"),
    )
    example_show_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path; defaults to XIAN_CONFIGS_DIR "
            "or the sibling workspace layout"
        ),
    )
    example_show_parser.set_defaults(handler_name="_handle_example_show")

    example_starter_parser = example_subparsers.add_parser(
        "starter",
        help="show the canonical starter flow for one example",
    )
    example_starter_parser.add_argument("name", help="example name")
    example_starter_parser.add_argument(
        "--flow",
        default="local",
        help="starter flow name; defaults to local",
    )
    example_starter_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=("workspace directory that may contain local ./examples and optionally sibling repos"),
    )
    example_starter_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path; defaults to XIAN_CONFIGS_DIR "
            "or the sibling workspace layout"
        ),
    )
    example_starter_parser.set_defaults(handler_name="_handle_example_starter")

    contract_parser = subparsers.add_parser("contract", help="contract helpers")
    contract_subparsers = contract_parser.add_subparsers(dest="contract_command", required=True)

    contract_build_artifacts_parser = contract_subparsers.add_parser(
        "build-artifacts",
        help="build xian_vm_v1 deployment artifacts from contract source",
    )
    contract_build_artifacts_parser.add_argument(
        "source",
        type=Path,
        help="contract source file path, or '-' to read from stdin",
    )
    contract_build_artifacts_parser.add_argument(
        "--name",
        help=(
            "contract module name; defaults to the source filename, with "
            "'.s.py' stripped when present"
        ),
    )
    contract_build_artifacts_parser.add_argument(
        "--output",
        type=Path,
        help="write artifact JSON to this path instead of stdout",
    )
    contract_build_artifacts_parser.add_argument(
        "--no-lint",
        action="store_true",
        help="skip lint checks while building artifacts",
    )
    contract_build_artifacts_parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite --output when it already exists",
    )
    contract_build_artifacts_parser.set_defaults(handler_name="_handle_contract_build_artifacts")

    contract_bundle_parser = contract_subparsers.add_parser(
        "bundle", help="inspect and validate contract bundles"
    )
    contract_bundle_subparsers = contract_bundle_parser.add_subparsers(
        dest="contract_bundle_command", required=True
    )

    contract_bundle_validate_parser = contract_bundle_subparsers.add_parser(
        "validate", help="validate a hash-pinned contract bundle"
    )
    contract_bundle_validate_parser.add_argument(
        "path", type=Path, help="path to contract-bundle.json"
    )
    contract_bundle_validate_parser.set_defaults(handler_name="_handle_contract_bundle_validate")

    create_parser = network_subparsers.add_parser("create", help="create a new network manifest")
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
    create_parser.add_argument("--chain-id", required=True, help="chain identifier")
    create_parser.add_argument(
        "--template",
        help=(
            "canonical or local template name used to prefill network and "
            "bootstrap-profile defaults"
        ),
    )
    create_parser.add_argument(
        "--node-image-mode",
        choices=sorted(SUPPORTED_NODE_IMAGE_MODES),
        help=(
            "node image source for generated manifests and profiles; "
            "use registry for pinned published images or local_build for "
            "workspace-built images"
        ),
    )
    create_parser.add_argument(
        "--node-integrated-image",
        help="pinned integrated node image reference for registry mode",
    )
    create_parser.add_argument(
        "--node-split-image",
        help="pinned split-runtime node image reference for registry mode",
    )
    create_parser.add_argument(
        "--genesis-source",
        help="path or URL for the genesis source used to bootstrap the network",
    )
    create_parser.add_argument(
        "--genesis-bundle",
        default="local",
        help=(
            "genesis contract bundle used when generating a local genesis "
            "file or when no local genesis file is generated; defaults to "
            "the universal local bundle"
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
            "path to validator_key_info.json or priv_validator_key.json for the initial validator"
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
        "--snapshot-signing-key",
        action="append",
        help=("trusted Ed25519 public key for signed snapshot manifests; may be repeated"),
    )
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
        "--validator",
        action="append",
        help=(
            "additional initial validator profile name; may be repeated "
            "for multi-validator network creation"
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
        help=("run node initialization immediately after writing the bootstrap node profile"),
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
    add_node_profile_runtime_args(
        create_parser,
        subject="the bootstrap node profile",
    )
    create_parser.add_argument(
        "--output",
        type=Path,
        help=("manifest output path; defaults to ./networks/<name>/manifest.json"),
    )
    create_parser.add_argument("--force", action="store_true")
    create_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "validate inputs and print the planned manifest path and profile without writing files"
        ),
    )
    create_parser.set_defaults(handler_name="_handle_network_create")

    join_parser = network_subparsers.add_parser(
        "join", help="create a node profile for joining an existing network"
    )
    join_parser.add_argument("name", help="local profile name")
    join_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "workspace directory that contains ./nodes, ./networks, and optionally ./xian-configs"
        ),
    )
    join_parser.add_argument(
        "--network",
        required=True,
        help="network manifest name, for example mainnet",
    )
    join_parser.add_argument(
        "--template",
        help=("canonical or local template name used to prefill node-profile runtime defaults"),
    )
    join_parser.add_argument(
        "--network-manifest",
        type=Path,
        help=("explicit network manifest path; overrides local and canonical lookup"),
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
        help=("output directory for generated validator key material; defaults to ./keys/<name>"),
    )
    join_parser.add_argument(
        "--node-image-mode",
        choices=sorted(SUPPORTED_NODE_IMAGE_MODES),
        help=("node image source override; defaults to the network manifest value"),
    )
    join_parser.add_argument(
        "--node-integrated-image",
        help="explicit integrated node image override for registry mode",
    )
    join_parser.add_argument(
        "--node-split-image",
        help="explicit split-runtime node image override for registry mode",
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
            "optional node-local seed override in <node_id>@<host>:26656 format; may be repeated"
        ),
    )
    join_parser.add_argument(
        "--genesis-source",
        dest="genesis_source",
        help=(
            "node-local genesis file or URL override; when set it takes "
            "precedence over the network manifest genesis"
        ),
    )
    join_parser.add_argument(
        "--snapshot-url",
        help="node-local snapshot URL override",
    )
    join_parser.add_argument(
        "--snapshot-signing-key",
        action="append",
        help=("trusted Ed25519 public key for signed snapshot manifests; may be repeated"),
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
    add_node_profile_runtime_args(
        join_parser,
        subject="this node profile",
    )
    join_parser.add_argument(
        "--output",
        type=Path,
        help="output file path; defaults to ./nodes/<name>.json",
    )
    join_parser.add_argument("--force", action="store_true")
    join_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=("validate inputs and print the planned node profile path without writing files"),
    )
    join_parser.set_defaults(handler_name="_handle_network_join")

    operator_bundle_parser = network_subparsers.add_parser(
        "package-operator-bundle",
        help="package a shareable operator bundle for an existing network",
    )
    operator_bundle_parser.add_argument("network", help="network name")
    operator_bundle_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=("workspace directory that may contain local ./networks and optionally sibling repos"),
    )
    operator_bundle_parser.add_argument(
        "--network-manifest",
        type=Path,
        help=("explicit network manifest path; overrides local and canonical lookup"),
    )
    operator_bundle_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path; defaults to XIAN_CONFIGS_DIR "
            "or the sibling workspace layout"
        ),
    )
    operator_bundle_parser.add_argument(
        "--output",
        type=Path,
        help=("output directory; defaults to ./dist/<network>-operator-bundle"),
    )
    operator_bundle_parser.add_argument(
        "--bootstrap-seed",
        help=(
            "bootstrap seed in <node_id>@<host>:26656 format to include in the bundle and manifest"
        ),
    )
    operator_bundle_parser.add_argument(
        "--archive",
        action="store_true",
        help="also write a .tar.gz archive next to the output directory",
    )
    operator_bundle_parser.add_argument("--force", action="store_true")
    operator_bundle_parser.set_defaults(handler_name="_handle_network_package_operator_bundle")

    setup_parser = subparsers.add_parser("setup", help="guided setup workflows")
    setup_subparsers = setup_parser.add_subparsers(dest="setup_command", required=True)

    setup_node_parser = setup_subparsers.add_parser(
        "node",
        help="guided node setup wizard",
    )
    setup_node_parser.add_argument(
        "--mode",
        choices=["join", "local"],
        help="setup path; join an existing network or create a fresh local network",
    )
    setup_node_parser.add_argument(
        "--name",
        help="local node profile name; defaults to validator-1",
    )
    setup_node_parser.add_argument(
        "--network",
        help="target network name; defaults to testnet for joins and local-dev locally",
    )
    setup_node_parser.add_argument(
        "--chain-id",
        help="chain identifier for --mode local; defaults from the network name",
    )
    setup_node_parser.add_argument(
        "--preset",
        choices=["basic", "indexed"],
        help="runtime preset; basic uses single-node-dev, indexed uses single-node-indexed",
    )
    setup_node_parser.add_argument(
        "--template",
        help="explicit canonical or local template name; overrides --preset",
    )
    setup_node_parser.add_argument(
        "--key-mode",
        choices=["generate", "existing"],
        help="generate new validator key material or use --validator-key-ref",
    )
    setup_node_parser.add_argument(
        "--validator-key-ref",
        type=Path,
        help="existing validator_key_info.json or priv_validator_key.json",
    )
    setup_node_parser.add_argument(
        "--validator-key-dir",
        type=Path,
        help="output directory for generated validator key material",
    )
    setup_node_parser.add_argument(
        "--network-manifest",
        type=Path,
        help="explicit network manifest path for --mode join",
    )
    setup_node_parser.add_argument(
        "--genesis-source",
        help="local genesis file or URL override",
    )
    setup_node_parser.add_argument(
        "--genesis-bundle",
        default="local",
        help="genesis contract bundle used for --mode local; defaults to local",
    )
    setup_node_parser.add_argument(
        "--bootstrap-mode",
        choices=["genesis", "snapshot"],
        help="join bootstrap mode; snapshot restores the effective snapshot after init",
    )
    setup_node_parser.add_argument(
        "--restore-snapshot",
        action="store_true",
        help="restore the effective snapshot after initializing a joined node",
    )
    setup_node_parser.add_argument(
        "--snapshot-url",
        help="node-local snapshot URL override",
    )
    setup_node_parser.add_argument(
        "--snapshot-signing-key",
        action="append",
        help="trusted Ed25519 public key for signed snapshot manifests; may be repeated",
    )
    setup_node_parser.add_argument(
        "--seed",
        action="append",
        help="node-local seed in <node_id>@<host>:26656 format; may be repeated",
    )
    setup_node_parser.add_argument(
        "--moniker",
        help="node moniker; defaults to the node name",
    )
    setup_node_parser.add_argument(
        "--node-image-mode",
        choices=sorted(SUPPORTED_NODE_IMAGE_MODES),
        help="node image source override; defaults to the existing lifecycle command behavior",
    )
    setup_node_parser.add_argument(
        "--node-integrated-image",
        help="explicit integrated node image override for registry mode",
    )
    setup_node_parser.add_argument(
        "--node-split-image",
        help="explicit split-runtime node image override for registry mode",
    )
    setup_node_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help="workspace directory that contains ./nodes, ./networks, and optionally sibling repos",
    )
    setup_node_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path; defaults to XIAN_CONFIGS_DIR "
            "or the sibling workspace layout"
        ),
    )
    setup_node_parser.add_argument(
        "--stack-dir",
        type=Path,
        help="path to the xian-stack checkout for the xian-stack backend",
    )
    setup_node_parser.add_argument(
        "--home",
        type=Path,
        help="explicit CometBFT home path",
    )
    setup_node_parser.add_argument(
        "--start",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="start the node after setup; interactive mode asks when omitted",
    )
    setup_node_parser.add_argument(
        "--rpc-url",
        default="http://127.0.0.1:26657/status",
        help="RPC status endpoint used by the post-start health check",
    )
    setup_node_parser.add_argument(
        "--rpc-timeout-seconds",
        type=float,
        default=DEFAULT_RPC_TIMEOUT_SECONDS,
        help="time to wait for the local RPC status endpoint when starting",
    )
    setup_node_parser.add_argument(
        "--skip-disk-check",
        action="store_true",
        help="skip the host-disk and data-volume health probe",
    )
    setup_node_parser.add_argument("--force", action="store_true")
    setup_node_parser.add_argument(
        "--plan",
        "--dry-run",
        action="store_true",
        help="print the setup plan without writing files or starting the node",
    )
    setup_node_parser.add_argument(
        "--yes",
        action="store_true",
        help="apply the setup without interactive confirmation",
    )
    setup_node_parser.set_defaults(handler_name="_handle_setup_node")

    node_parser = subparsers.add_parser("node", help="node lifecycle")
    node_subparsers = node_parser.add_subparsers(dest="node_command", required=True)

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
            "explicit network manifest path; defaults to ./networks/<profile.network>/manifest.json"
        ),
    )
    init_parser.add_argument(
        "--validator-key",
        type=Path,
        help=("explicit validator key path; overrides validator_key_ref in the profile"),
    )
    init_parser.add_argument(
        "--stack-dir",
        type=Path,
        help=("explicit xian-stack checkout path; overrides stack_dir in the profile"),
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
        help=("overwrite config, genesis, and priv_validator_key.json if they already exist"),
    )
    init_parser.set_defaults(handler_name="_handle_node_init")

    start_parser = node_subparsers.add_parser("start", help="start a node")
    start_parser.add_argument("name", help="node profile name")
    start_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=("workspace directory that contains ./nodes, ./networks, and ./xian-stack"),
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
            "explicit network manifest path; defaults to ./networks/<profile.network>/manifest.json"
        ),
    )
    start_parser.add_argument(
        "--stack-dir",
        type=Path,
        help=("explicit xian-stack checkout path; overrides stack_dir in the profile"),
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
        default=DEFAULT_RPC_TIMEOUT_SECONDS,
        help="time to wait for the local RPC status endpoint",
    )
    start_parser.set_defaults(handler_name="_handle_node_start")

    stop_parser = node_subparsers.add_parser("stop", help="stop a node")
    stop_parser.add_argument("name", help="node profile name")
    stop_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=("workspace directory that contains ./nodes, ./networks, and ./xian-stack"),
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
            "explicit network manifest path; defaults to ./networks/<profile.network>/manifest.json"
        ),
    )
    stop_parser.add_argument(
        "--stack-dir",
        type=Path,
        help=("explicit xian-stack checkout path; overrides stack_dir in the profile"),
    )
    stop_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path; defaults to XIAN_CONFIGS_DIR "
            "or the sibling workspace layout"
        ),
    )
    stop_parser.set_defaults(handler_name="_handle_node_stop")

    status_parser = node_subparsers.add_parser(
        "status", help="inspect node bootstrap and RPC status"
    )
    status_parser.add_argument("name", help="node profile name")
    status_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "workspace directory that contains ./nodes, ./networks, and optionally sibling repos"
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
            "explicit network manifest path; defaults to ./networks/<profile.network>/manifest.json"
        ),
    )
    status_parser.add_argument(
        "--stack-dir",
        type=Path,
        help=("explicit xian-stack checkout path; overrides stack_dir in the profile"),
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
    status_parser.set_defaults(handler_name="_handle_node_status")

    endpoints_parser = node_subparsers.add_parser(
        "endpoints",
        help=("print the expected local URLs for RPC, metrics, dashboard, and monitoring"),
    )
    endpoints_parser.add_argument("name", help="node profile name")
    endpoints_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "base directory containing ./nodes, ./networks, ./keys, and optionally sibling repos"
        ),
    )
    endpoints_parser.add_argument(
        "--profile",
        type=Path,
        help="explicit node profile path; defaults to ./nodes/<name>.json",
    )
    endpoints_parser.add_argument(
        "--network",
        type=Path,
        help=(
            "explicit network manifest path; defaults to ./networks/<profile.network>/manifest.json"
        ),
    )
    endpoints_parser.add_argument(
        "--stack-dir",
        type=Path,
        help=(
            "explicit xian-stack checkout path when using the xian-stack "
            "backend; "
            "overrides stack_dir in the profile"
        ),
    )
    endpoints_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path for canonical manifests "
            "or the sibling workspace layout"
        ),
    )
    endpoints_parser.add_argument(
        "--home",
        type=Path,
        help="explicit CometBFT home path; overrides the profile home",
    )
    endpoints_parser.add_argument(
        "--rpc-url",
        default="http://127.0.0.1:26657/status",
        help="RPC status endpoint used to derive default host/port URLs",
    )
    endpoints_parser.set_defaults(handler_name="_handle_node_endpoints")

    health_parser = node_subparsers.add_parser(
        "health",
        help=("inspect live runtime health, disk pressure, and state-sync readiness"),
    )
    health_parser.add_argument("name", help="node profile name")
    health_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "base directory containing ./nodes, ./networks, ./keys, and optionally sibling repos"
        ),
    )
    health_parser.add_argument(
        "--profile",
        type=Path,
        help="explicit node profile path; defaults to ./nodes/<name>.json",
    )
    health_parser.add_argument(
        "--network",
        type=Path,
        help=(
            "explicit network manifest path; defaults to ./networks/<profile.network>/manifest.json"
        ),
    )
    health_parser.add_argument(
        "--stack-dir",
        type=Path,
        help=(
            "explicit xian-stack checkout path when using the xian-stack "
            "backend; overrides stack_dir in the profile"
        ),
    )
    health_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path for canonical manifests "
            "or the sibling workspace layout"
        ),
    )
    health_parser.add_argument(
        "--home",
        type=Path,
        help="explicit CometBFT home path; overrides the profile home",
    )
    health_parser.add_argument(
        "--rpc-url",
        default="http://127.0.0.1:26657/status",
        help="RPC status endpoint used for live health probes",
    )
    health_parser.add_argument(
        "--skip-disk-check",
        action="store_true",
        help="skip the host-disk and data-volume health probe",
    )
    health_parser.set_defaults(handler_name="_handle_node_health")

    recovery_parser = subparsers.add_parser(
        "recovery",
        help="validated rollback/recovery plan tools",
    )
    recovery_subparsers = recovery_parser.add_subparsers(dest="recovery_command", required=True)

    recovery_validate_parser = recovery_subparsers.add_parser(
        "validate",
        help="validate a recovery plan against a local node profile/home",
    )
    recovery_validate_parser.add_argument(
        "plan",
        type=Path,
        help="path to the recovery plan JSON file",
    )
    recovery_validate_parser.add_argument("name", help="node profile name")
    recovery_validate_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "workspace directory that contains ./nodes, ./networks, and optionally ./xian-configs"
        ),
    )
    recovery_validate_parser.add_argument(
        "--profile",
        type=Path,
        help="explicit node profile path; defaults to ./nodes/<name>.json",
    )
    recovery_validate_parser.add_argument(
        "--network",
        type=Path,
        help=(
            "explicit network manifest path; defaults to ./networks/<profile.network>/manifest.json"
        ),
    )
    recovery_validate_parser.add_argument(
        "--stack-dir",
        type=Path,
        help=("explicit xian-stack checkout path; overrides stack_dir in the profile"),
    )
    recovery_validate_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path; defaults to XIAN_CONFIGS_DIR "
            "or the sibling workspace layout"
        ),
    )
    recovery_validate_parser.add_argument(
        "--home",
        type=Path,
        help="explicit CometBFT home path; overrides the profile home",
    )
    recovery_validate_parser.add_argument(
        "--rpc-url",
        default="http://127.0.0.1:26657/status",
        help="optional RPC status endpoint used for pre-recovery validation",
    )
    recovery_validate_parser.set_defaults(handler_name="_handle_recovery_validate")

    recovery_apply_parser = recovery_subparsers.add_parser(
        "apply",
        help="apply a validated recovery plan to a local node home",
    )
    recovery_apply_parser.add_argument(
        "plan",
        type=Path,
        help="path to the recovery plan JSON file",
    )
    recovery_apply_parser.add_argument("name", help="node profile name")
    recovery_apply_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "workspace directory that contains ./nodes, ./networks, and optionally ./xian-configs"
        ),
    )
    recovery_apply_parser.add_argument(
        "--profile",
        type=Path,
        help="explicit node profile path; defaults to ./nodes/<name>.json",
    )
    recovery_apply_parser.add_argument(
        "--network",
        type=Path,
        help=(
            "explicit network manifest path; defaults to ./networks/<profile.network>/manifest.json"
        ),
    )
    recovery_apply_parser.add_argument(
        "--stack-dir",
        type=Path,
        help=("explicit xian-stack checkout path; overrides stack_dir in the profile"),
    )
    recovery_apply_parser.add_argument(
        "--configs-dir",
        type=Path,
        help=(
            "explicit xian-configs checkout path; defaults to XIAN_CONFIGS_DIR "
            "or the sibling workspace layout"
        ),
    )
    recovery_apply_parser.add_argument(
        "--home",
        type=Path,
        help="explicit CometBFT home path; overrides the profile home",
    )
    recovery_apply_parser.add_argument(
        "--rpc-url",
        default="http://127.0.0.1:26657/status",
        help="optional RPC status endpoint used for pre-recovery validation",
    )
    recovery_apply_parser.add_argument(
        "--backup-dir",
        type=Path,
        help="directory for the pre-recovery node-home backup archive",
    )
    recovery_apply_parser.add_argument(
        "--skip-backup",
        action="store_true",
        help="skip creating a pre-recovery node-home backup archive",
    )
    recovery_apply_parser.add_argument(
        "--skip-stop",
        action="store_true",
        help="skip stopping the local xian-stack node before restore",
    )
    recovery_apply_parser.add_argument(
        "--start-node",
        action="store_true",
        help="start the node again after the recovery snapshot is applied",
    )
    recovery_apply_parser.add_argument(
        "--no-wait",
        action="store_true",
        help="when used with --start-node, do not wait for RPC readiness",
    )
    recovery_apply_parser.add_argument(
        "--rpc-timeout-seconds",
        type=float,
        default=DEFAULT_RPC_TIMEOUT_SECONDS,
        help="RPC wait timeout used with --start-node",
    )
    recovery_apply_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=("validate and print the recovery actions without changing the node home"),
    )
    recovery_apply_parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm that the validated recovery plan should be applied",
    )
    recovery_apply_parser.set_defaults(handler_name="_handle_recovery_apply")

    snapshot_parser = subparsers.add_parser("snapshot", help="snapshot tools")
    snapshot_subparsers = snapshot_parser.add_subparsers(dest="snapshot_command", required=True)

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
            "workspace directory that contains ./nodes, ./networks, and optionally ./xian-configs"
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
            "explicit network manifest path; defaults to ./networks/<profile.network>/manifest.json"
        ),
    )
    restore_parser.add_argument(
        "--stack-dir",
        type=Path,
        help=("explicit xian-stack checkout path; overrides stack_dir in the profile"),
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
    restore_parser.set_defaults(handler_name="_handle_snapshot_restore")

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="check workspace and optional node prerequisites",
    )
    doctor_parser.add_argument(
        "name",
        nargs="?",
        help=("optional node profile name to inspect in addition to workspace checks"),
    )
    doctor_parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "workspace directory that contains ./nodes, ./networks, and optionally sibling repos"
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
            "explicit network manifest path; defaults to ./networks/<profile.network>/manifest.json"
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
    doctor_parser.add_argument(
        "--skip-live-checks",
        action="store_true",
        help=(
            "only verify local workspace and node-home artifacts; "
            "skip backend, RPC, dashboard, and monitoring reachability"
        ),
    )
    doctor_parser.set_defaults(handler_name="_handle_doctor")

    return parser
