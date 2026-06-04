from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.error import URLError

from xian_accounts import Ed25519Account
from xian_py.models import IndexedBlock, TransactionReceipt
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.time import Datetime

import xian_cli.abci_bridge as abci_bridge
import xian_cli.client.handlers as client_handlers
import xian_cli.output as cli_output
from xian_cli.cli import _fallback_node_endpoints, main
from xian_cli.config_repo import (
    resolve_configs_dir,
    resolve_contract_pack_path,
    resolve_example_path,
    resolve_network_manifest_path,
    resolve_network_template_path,
)
from xian_cli.models import (
    read_contract_pack,
    read_example,
    read_network_manifest,
    read_network_template,
    read_node_profile,
)
from xian_cli.runtime import (
    default_home_for_backend,
    get_xian_stack_node_endpoints,
    get_xian_stack_node_health,
    get_xian_stack_node_status,
    resolve_stack_dir,
    run_backend_command,
    start_xian_stack_node,
    stop_xian_stack_node,
    wait_for_rpc_ready,
)

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TEST_FIXTURE_GENESIS = Path(__file__).resolve().parent / "fixtures" / "genesis.json"
CANONICAL_DEVNET_MANIFEST = (
    WORKSPACE_ROOT / "xian-configs" / "networks" / "devnet" / "manifest.json"
)
CANONICAL_NODE_RELEASE_MANIFEST = json.loads(
    (WORKSPACE_ROOT / "xian-stack" / "release-manifest.json").read_text(encoding="utf-8")
)
CANONICAL_RELEASE_INTEGRATED_IMAGE = (
    "ghcr.io/xian-technology/xian-node@sha256:"
    "014527ec7a7e5bc0b63f512421a3d6feedc7b3999c68113d195deb6b41eae6c3"
)
CANONICAL_RELEASE_SPLIT_IMAGE = (
    "ghcr.io/xian-technology/xian-node-split@sha256:"
    "2351ca938fe147af9bed8e827ac9c86de6686dbac228f3822de7e1b4ac41a54c"
)


class ParserUxTests(unittest.TestCase):
    def test_top_level_version_flag_reports_package_version(self) -> None:
        expected_version = tomllib.loads(
            (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["version"]
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                main(["--version"])

        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(stdout.getvalue().strip(), f"xian {expected_version}")

    def test_runtime_help_describes_fee_mode_options(self) -> None:
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as exc:
                main(["network", "join", "--help"])

        self.assertEqual(exc.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("--tx-fee-mode", help_text)
        self.assertIn("transaction fee policy", help_text)
        self.assertIn("--free-tx-max-chi", help_text)
        self.assertIn("--free-block-max-chi", help_text)


def _normalized_release_manifest(manifest: dict) -> dict:
    build = manifest["build"]
    return {
        "schema_version": manifest["schema_version"],
        "components": manifest["components"],
        "build": {
            "python_image": build["python_image"],
            "go_image": build["go_image"],
            "cometbft_version": build["cometbft_version"],
            "cometbft_source_url": build["cometbft_source_url"],
            "cometbft_source_sha256": build["cometbft_source_sha256"],
            "s6_overlay_version": build["s6_overlay_version"],
            "s6_overlay_noarch_sha256": build["s6_overlay_noarch_sha256"],
            "s6_overlay_x86_64_sha256": build["s6_overlay_x86_64_sha256"],
            "s6_overlay_aarch64_sha256": build["s6_overlay_aarch64_sha256"],
        },
        "images": manifest["images"],
    }


def _manifest_payload(
    *,
    name: str = "canonical",
    chain_id: str = "xian-canonical-1",
    genesis: dict | None = None,
    p2p_seeds: list[str] | None = None,
    **overrides,
) -> dict:
    payload = {
        "schema_version": 1,
        "name": name,
        "chain_id": chain_id,
        "genesis": genesis or {"kind": "source", "source": "./genesis.json"},
        "snapshot_url": None,
        "p2p": {
            "seeds": p2p_seeds or [],
            "persistent_peers": [],
        },
    }
    payload.update(overrides)
    return payload


def _services_payload(
    *,
    bds_enabled: bool = False,
    dashboard_enabled: bool = False,
    monitoring_enabled: bool = False,
    dashboard_host: str = "127.0.0.1",
    dashboard_port: int = 8080,
) -> dict:
    return {
        "bds": {"enabled": bds_enabled},
        "dashboard": {
            "enabled": dashboard_enabled,
            "host": dashboard_host,
            "port": dashboard_port,
        },
        "monitoring": {"enabled": monitoring_enabled},
    }


def _profile_payload(
    *,
    name: str = "validator-1",
    network: str = "mainnet",
    moniker: str = "validator-1",
    stack_dir: str | None = None,
    p2p_seeds: list[str] | None = None,
    services: dict | None = None,
    **overrides,
) -> dict:
    payload = {
        "schema_version": 1,
        "name": name,
        "network": network,
        "moniker": moniker,
        "stack_dir": stack_dir,
        "p2p": {
            "seeds": p2p_seeds or [],
            "persistent_peers": [],
        },
        "genesis": None,
        "snapshot_url": None,
        "services": services or _services_payload(),
    }
    payload.update(overrides)
    return payload


def _template_services(
    *,
    bds_enabled: bool,
    dashboard_enabled: bool,
    monitoring_enabled: bool,
    dashboard_host: str,
    dashboard_port: int,
) -> dict:
    return _services_payload(
        bds_enabled=bds_enabled,
        dashboard_enabled=dashboard_enabled,
        monitoring_enabled=monitoring_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
    )


class ValidatorKeyTests(unittest.TestCase):
    def test_generate_validator_material_shape(self) -> None:
        payload = abci_bridge.get_node_setup_module().generate_validator_material()
        self.assertEqual(len(payload["validator_private_key_hex"]), 64)
        self.assertEqual(len(payload["validator_public_key_hex"]), 64)
        self.assertIn("address", payload["priv_validator_key"])
        self.assertIn("pub_key", payload["priv_validator_key"])
        self.assertIn("priv_key", payload["priv_validator_key"])

    def test_generate_validator_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with redirect_stdout(io.StringIO()):
                exit_code = main(["keys", "validator", "generate", "--out-dir", tmp_dir])
            self.assertEqual(exit_code, 0)

            output_dir = Path(tmp_dir)
            priv_validator_path = output_dir / "priv_validator_key.json"
            metadata_path = output_dir / "validator_key_info.json"

            self.assertTrue(priv_validator_path.exists())
            self.assertTrue(metadata_path.exists())

            priv_validator_payload = json.loads(priv_validator_path.read_text(encoding="utf-8"))
            metadata_payload = json.loads(metadata_path.read_text(encoding="utf-8"))

            self.assertEqual(
                priv_validator_payload["address"],
                metadata_payload["priv_validator_key"]["address"],
            )


class SetupNodeCommandTests(unittest.TestCase):
    def test_setup_node_plan_join_prints_lifecycle_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "setup",
                        "node",
                        "--mode",
                        "join",
                        "--network",
                        "testnet",
                        "--name",
                        "validator-1",
                        "--preset",
                        "indexed",
                        "--key-mode",
                        "generate",
                        "--tx-fee-mode",
                        "free_metered",
                        "--free-tx-max-chi",
                        "250000",
                        "--free-block-max-chi",
                        "1000000",
                        "--start",
                        "--base-dir",
                        str(base_dir),
                        "--plan",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["mode"], "join")
            self.assertEqual(payload["template"], "single-node-indexed")
            self.assertEqual(
                payload["block_policy"],
                {"mode": "on_demand", "interval": "0s", "source": "template"},
            )
            self.assertEqual(
                payload["writes"][0],
                str(base_dir.resolve() / "nodes/validator-1.json"),
            )
            self.assertEqual(payload["steps"][0]["name"], "join-network")
            self.assertEqual(
                payload["steps"][0]["command"][:4],
                ["xian", "network", "join", "validator-1"],
            )
            self.assertIn("--generate-validator-key", payload["steps"][0]["command"])
            self.assertIn("--init-node", payload["steps"][0]["command"])
            self.assertIn("--tx-fee-mode", payload["steps"][0]["command"])
            self.assertIn("free_metered", payload["steps"][0]["command"])
            self.assertEqual(
                payload["runtime_args"],
                {
                    "tx_fee_mode": "free_metered",
                    "free_tx_max_chi": 250000,
                    "free_block_max_chi": 1000000,
                },
            )
            self.assertEqual(payload["steps"][1]["name"], "start-node")
            self.assertEqual(payload["steps"][2]["name"], "health-check")

    def test_setup_node_plan_forwards_block_policy_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "setup",
                        "node",
                        "--mode",
                        "local",
                        "--network",
                        "local-dev",
                        "--name",
                        "validator-1",
                        "--chain-id",
                        "xian-local-1",
                        "--preset",
                        "basic",
                        "--key-mode",
                        "generate",
                        "--block-policy-mode",
                        "periodic",
                        "--block-policy-interval",
                        "1s",
                        "--no-start",
                        "--base-dir",
                        str(base_dir),
                        "--plan",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(
                payload["block_policy"],
                {"mode": "periodic", "interval": "1s", "source": "arguments"},
            )
            self.assertEqual(
                payload["runtime_args"],
                {
                    "block_policy_mode": "periodic",
                    "block_policy_interval": "1s",
                },
            )
            self.assertIn("--block-policy-mode", payload["steps"][0]["command"])
            self.assertIn("periodic", payload["steps"][0]["command"])
            self.assertIn("--block-policy-interval", payload["steps"][0]["command"])
            self.assertIn("1s", payload["steps"][0]["command"])

    def test_setup_node_rejects_interval_without_periodic_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaisesRegex(ValueError, "has no effect"):
                with redirect_stdout(io.StringIO()):
                    main(
                        [
                            "setup",
                            "node",
                            "--mode",
                            "local",
                            "--network",
                            "local-dev",
                            "--name",
                            "validator-1",
                            "--chain-id",
                            "xian-local-1",
                            "--preset",
                            "basic",
                            "--key-mode",
                            "generate",
                            "--block-policy-interval",
                            "1s",
                            "--base-dir",
                            tmp_dir,
                            "--plan",
                        ]
                    )

    def test_setup_node_join_initializes_node_without_starting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            network_dir = base_dir / "networks" / "testnet"
            network_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(
                    _manifest_payload(
                        name="testnet",
                        chain_id="xian-testnet-12",
                        genesis={"kind": "source", "source": str(TEST_FIXTURE_GENESIS)},
                    )
                ),
                encoding="utf-8",
            )
            home = base_dir / ".cometbft"
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "setup",
                        "node",
                        "--mode",
                        "join",
                        "--network",
                        "testnet",
                        "--name",
                        "validator-1",
                        "--preset",
                        "indexed",
                        "--key-mode",
                        "generate",
                        "--home",
                        str(home),
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(WORKSPACE_ROOT / "xian-configs"),
                        "--no-start",
                        "--yes",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["setup"], "node")
            self.assertFalse(payload["started"])
            self.assertTrue(payload["network"]["node_initialized"])
            self.assertEqual(
                payload["network"]["validator_key_ref"],
                "keys/validator-1/validator_key_info.json",
            )
            self.assertTrue(
                (base_dir / "keys" / "validator-1" / "validator_key_info.json").exists()
            )
            self.assertTrue((base_dir / "nodes" / "validator-1.json").exists())
            self.assertTrue((home / "config" / "config.toml").exists())
            self.assertTrue((home / "config" / "xian.toml").exists())
            self.assertTrue((home / "config" / "genesis.json").exists())
            profile = json.loads(
                (base_dir / "nodes" / "validator-1.json").read_text(encoding="utf-8")
            )
            self.assertTrue(profile["services"]["bds"]["enabled"])
            self.assertEqual(profile["services"]["dashboard"]["host"], "127.0.0.1")
            self.assertEqual(profile["operator_profile"], "indexed_development")
            self.assertEqual(payload["next_steps"][0][:4], ["xian", "node", "start", "validator-1"])

    def test_setup_node_local_initializes_node_without_starting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            home = base_dir / ".cometbft"
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "setup",
                        "node",
                        "--mode",
                        "local",
                        "--network",
                        "local-dev",
                        "--name",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(WORKSPACE_ROOT / "xian-configs"),
                        "--home",
                        str(home),
                        "--no-start",
                        "--yes",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["mode"], "local")
            self.assertEqual(payload["plan"]["chain_id"], "xian-local-1")
            self.assertTrue(payload["network"]["node_initialized"])
            self.assertTrue((base_dir / "networks" / "local-dev" / "manifest.json").exists())
            self.assertTrue((base_dir / "networks" / "local-dev" / "genesis.json").exists())
            self.assertTrue((base_dir / "nodes" / "validator-1.json").exists())
            self.assertTrue((home / "config" / "config.toml").exists())
            profile = json.loads(
                (base_dir / "nodes" / "validator-1.json").read_text(encoding="utf-8")
            )
            self.assertFalse(profile["services"]["bds"]["enabled"])
            self.assertEqual(profile["services"]["dashboard"]["host"], "127.0.0.1")
            self.assertEqual(profile["operator_profile"], "local_development")

    def test_setup_node_local_periodic_block_policy_writes_cometbft_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            home = base_dir / ".cometbft"
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "setup",
                        "node",
                        "--mode",
                        "local",
                        "--network",
                        "local-dev",
                        "--name",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(WORKSPACE_ROOT / "xian-configs"),
                        "--home",
                        str(home),
                        "--block-policy-mode",
                        "periodic",
                        "--block-policy-interval",
                        "1s",
                        "--no-start",
                        "--yes",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(
                payload["plan"]["block_policy"],
                {"mode": "periodic", "interval": "1s", "source": "arguments"},
            )
            profile = json.loads(
                (base_dir / "nodes" / "validator-1.json").read_text(encoding="utf-8")
            )
            self.assertEqual(profile["block_policy_mode"], "periodic")
            self.assertEqual(profile["block_policy_interval"], "1s")
            config = tomllib.loads((home / "config" / "config.toml").read_text())
            self.assertTrue(config["consensus"]["create_empty_blocks"])
            self.assertEqual(config["consensus"]["create_empty_blocks_interval"], "1s")

    def test_setup_node_interactive_prompt_can_override_block_policy(self) -> None:
        class TtyInput(io.StringIO):
            def isatty(self) -> bool:
                return True

        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            home = base_dir / ".cometbft"
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                patch("sys.stdin", TtyInput("y\nperiodic\n1s\ny\n")),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                exit_code = main(
                    [
                        "setup",
                        "node",
                        "--mode",
                        "local",
                        "--network",
                        "local-dev",
                        "--name",
                        "validator-1",
                        "--chain-id",
                        "xian-local-1",
                        "--preset",
                        "basic",
                        "--key-mode",
                        "generate",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(WORKSPACE_ROOT / "xian-configs"),
                        "--home",
                        str(home),
                        "--no-start",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertIn("Effective block production policy", stderr.getvalue())
            self.assertIn("Empty-block interval", stderr.getvalue())
            payload = json.loads(stdout.getvalue())
            self.assertEqual(
                payload["plan"]["block_policy"],
                {"mode": "periodic", "interval": "1s", "source": "wizard"},
            )
            profile = json.loads(
                (base_dir / "nodes" / "validator-1.json").read_text(encoding="utf-8")
            )
            self.assertEqual(profile["block_policy_mode"], "periodic")
            self.assertEqual(profile["block_policy_interval"], "1s")

    def test_setup_node_forwards_runtime_service_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            home = base_dir / ".cometbft"
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "setup",
                        "node",
                        "--mode",
                        "local",
                        "--network",
                        "local-dev",
                        "--name",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(WORKSPACE_ROOT / "xian-configs"),
                        "--home",
                        str(home),
                        "--enable-intentkit",
                        "--intentkit-network-id",
                        "xian-localnet",
                        "--intentkit-api-port",
                        "38180",
                        "--enable-dex-automation",
                        "--dex-automation-host",
                        "0.0.0.0",
                        "--dex-automation-port",
                        "38281",
                        "--dex-automation-config",
                        "/tmp/dex-automation.yaml",
                        "--enable-shielded-relayer",
                        "--shielded-relayer-host",
                        "0.0.0.0",
                        "--shielded-relayer-port",
                        "38181",
                        "--no-start",
                        "--yes",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(
                payload["plan"]["runtime_args"],
                {
                    "enable_intentkit": True,
                    "intentkit_network_id": "xian-localnet",
                    "intentkit_api_port": 38180,
                    "enable_dex_automation": True,
                    "dex_automation_host": "0.0.0.0",
                    "dex_automation_port": 38281,
                    "dex_automation_config": "/tmp/dex-automation.yaml",
                    "enable_shielded_relayer": True,
                    "shielded_relayer_host": "0.0.0.0",
                    "shielded_relayer_port": 38181,
                },
            )
            profile = json.loads(
                (base_dir / "nodes" / "validator-1.json").read_text(encoding="utf-8")
            )
            self.assertTrue(profile["services"]["intentkit"]["enabled"])
            self.assertEqual(profile["services"]["intentkit"]["network_id"], "xian-localnet")
            self.assertEqual(profile["services"]["intentkit"]["api_port"], 38180)
            self.assertTrue(profile["services"]["dex_automation"]["enabled"])
            self.assertEqual(profile["services"]["dex_automation"]["host"], "0.0.0.0")
            self.assertEqual(profile["services"]["dex_automation"]["port"], 38281)
            self.assertEqual(
                profile["services"]["dex_automation"]["config"],
                "/tmp/dex-automation.yaml",
            )
            self.assertTrue(profile["services"]["shielded_relayer"]["enabled"])
            self.assertEqual(profile["services"]["shielded_relayer"]["host"], "0.0.0.0")
            self.assertEqual(profile["services"]["shielded_relayer"]["port"], 38181)

    def test_setup_node_no_start_sidecar_matrix_writes_expected_profile(self) -> None:
        scenarios = [
            {
                "name": "intentkit",
                "preset": "basic",
                "flags": [
                    "--enable-intentkit",
                    "--intentkit-network-id",
                    "xian-localnet",
                    "--intentkit-port",
                    "39000",
                    "--intentkit-api-port",
                    "39080",
                ],
                "services": {
                    "intentkit": {
                        "enabled": True,
                        "network_id": "xian-localnet",
                        "port": 39000,
                        "api_port": 39080,
                    },
                    "dex_automation": {"enabled": False},
                    "shielded_relayer": {"enabled": False},
                },
            },
            {
                "name": "dex-automation",
                "preset": "indexed",
                "flags": [
                    "--enable-dex-automation",
                    "--dex-automation-port",
                    "39280",
                    "--dex-automation-config",
                    "/tmp/xian-dex-automation.yaml",
                ],
                "services": {
                    "bds": {"enabled": True},
                    "intentkit": {"enabled": False},
                    "dex_automation": {
                        "enabled": True,
                        "port": 39280,
                        "config": "/tmp/xian-dex-automation.yaml",
                    },
                    "shielded_relayer": {"enabled": False},
                },
            },
            {
                "name": "shielded-relayer",
                "preset": "basic",
                "flags": [
                    "--enable-shielded-relayer",
                    "--shielded-relayer-port",
                    "39180",
                ],
                "services": {
                    "intentkit": {"enabled": False},
                    "dex_automation": {"enabled": False},
                    "shielded_relayer": {"enabled": True, "port": 39180},
                },
            },
            {
                "name": "all-sidecars",
                "preset": "indexed",
                "flags": [
                    "--enable-intentkit",
                    "--intentkit-network-id",
                    "xian-localnet",
                    "--enable-dex-automation",
                    "--dex-automation-port",
                    "39281",
                    "--enable-shielded-relayer",
                    "--shielded-relayer-port",
                    "39181",
                ],
                "services": {
                    "bds": {"enabled": True},
                    "intentkit": {"enabled": True, "network_id": "xian-localnet"},
                    "dex_automation": {"enabled": True, "port": 39281},
                    "shielded_relayer": {"enabled": True, "port": 39181},
                },
            },
        ]

        for scenario in scenarios:
            with self.subTest(scenario=scenario["name"]):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    base_dir = Path(tmp_dir)
                    home = base_dir / ".cometbft"
                    stdout = io.StringIO()

                    with redirect_stdout(stdout):
                        exit_code = main(
                            [
                                "setup",
                                "node",
                                "--mode",
                                "local",
                                "--network",
                                f"local-{scenario['name']}",
                                "--name",
                                "validator-1",
                                "--preset",
                                scenario["preset"],
                                "--base-dir",
                                str(base_dir),
                                "--configs-dir",
                                str(WORKSPACE_ROOT / "xian-configs"),
                                "--home",
                                str(home),
                                *scenario["flags"],
                                "--no-start",
                                "--yes",
                            ]
                        )

                    self.assertEqual(exit_code, 0)
                    payload = json.loads(stdout.getvalue())
                    self.assertFalse(payload["started"])
                    self.assertTrue(payload["network"]["node_initialized"])
                    self.assertIn("start", payload["next_steps"][0])
                    profile = json.loads(
                        (base_dir / "nodes" / "validator-1.json").read_text(encoding="utf-8")
                    )
                    for service_name, expected in scenario["services"].items():
                        for field_name, expected_value in expected.items():
                            self.assertEqual(
                                expected_value,
                                profile["services"][service_name][field_name],
                                f"{scenario['name']} {service_name}.{field_name}",
                            )


class _FakeContextClient:
    def __init__(self, **responses):
        self.responses = responses
        self.send_tx_calls = []
        self.submit_contract_calls = []
        self.send_calls = []
        self.call_calls = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get_balance(self, address: str, contract: str = "currency"):
        return self.responses["balance"]

    def get_tx(self, tx_hash: str):
        return self.responses["tx"]

    def get_block(self, height: int):
        return self.responses["block"]

    def get_block_by_hash(self, block_hash: str):
        return self.responses["block"]

    def call(self, contract: str, function: str, kwargs: dict):
        self.call_calls.append((contract, function, kwargs))
        return self.responses["call"]

    def simulate(self, contract: str, function: str, kwargs: dict):
        return self.responses["simulate"]

    def send_tx(self, **kwargs):
        self.send_tx_calls.append(kwargs)
        return self.responses["send_tx"]

    def submit_contract(self, name, deployment_artifacts, args=None, **kwargs):
        self.submit_contract_calls.append(
            {
                "name": name,
                "deployment_artifacts": deployment_artifacts,
                "args": args,
                **kwargs,
            }
        )
        return self.responses["submit_contract"]

    def send(self, amount, to_address, token="currency", **kwargs):
        self.send_calls.append(
            {
                "amount": amount,
                "to_address": to_address,
                "token": token,
                **kwargs,
            }
        )
        return self.responses["send"]


class ClientCommandTests(unittest.TestCase):
    def test_resolve_node_url_prefers_argument(self) -> None:
        args = argparse.Namespace(node_url="http://node.example")
        self.assertEqual(
            client_handlers._resolve_node_url(args),
            "http://node.example",
        )

    def test_resolve_node_url_uses_environment(self) -> None:
        args = argparse.Namespace(node_url=None)
        with patch.dict("os.environ", {"XIAN_NODE_URL": "http://env-node"}):
            self.assertEqual(
                client_handlers._resolve_node_url(args),
                "http://env-node",
            )

    def test_resolve_node_url_requires_value(self) -> None:
        args = argparse.Namespace(node_url=None)
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(ValueError, "node URL is required"):
                client_handlers._resolve_node_url(args)

    def test_resolve_chain_id_uses_environment(self) -> None:
        args = argparse.Namespace(chain_id=None)
        with patch.dict("os.environ", {"XIAN_CHAIN_ID": "xian-1"}):
            self.assertEqual(
                client_handlers._resolve_chain_id(args),
                "xian-1",
            )

    def test_load_private_key_from_file(self) -> None:
        account = Ed25519Account.generate()
        with tempfile.TemporaryDirectory() as tmp_dir:
            key_path = Path(tmp_dir) / "wallet.key"
            key_path.write_text(account.private_key + "\n", encoding="utf-8")
            args = argparse.Namespace(
                private_key=None,
                private_key_env=None,
                private_key_file=str(key_path),
            )
            self.assertEqual(
                client_handlers._load_private_key_from_args(args),
                account.private_key,
            )

    def test_load_private_key_from_env(self) -> None:
        account = Ed25519Account.generate()
        args = argparse.Namespace(
            private_key=None,
            private_key_env="XIAN_PRIVATE_KEY",
            private_key_file=None,
        )
        with patch.dict(
            "os.environ",
            {"XIAN_PRIVATE_KEY": account.private_key},
        ):
            self.assertEqual(
                client_handlers._load_private_key_from_args(args),
                account.private_key,
            )

    def test_load_private_key_requires_single_source(self) -> None:
        account = Ed25519Account.generate()
        args = argparse.Namespace(
            private_key=account.private_key,
            private_key_env="XIAN_PRIVATE_KEY",
            private_key_file=None,
        )
        with patch.dict(
            "os.environ",
            {"XIAN_PRIVATE_KEY": account.private_key},
        ):
            with self.assertRaisesRegex(ValueError, "provide only one"):
                client_handlers._load_private_key_from_args(args)

    def test_load_private_key_requires_value(self) -> None:
        args = argparse.Namespace(
            private_key=None,
            private_key_env=None,
            private_key_file=None,
        )
        with self.assertRaisesRegex(ValueError, "private key is required"):
            client_handlers._load_private_key_from_args(args)

    def test_make_client_constructs_xian(self) -> None:
        args = argparse.Namespace(
            node_url="http://node.example",
            chain_id="xian-1",
        )
        wallet = object()
        with patch(
            "xian_cli.client.handlers.Xian",
            return_value="client",
        ) as ctor:
            client = client_handlers._make_client(args, wallet=wallet)

        self.assertEqual(client, "client")
        ctor.assert_called_once_with(
            "http://node.example",
            chain_id="xian-1",
            wallet=wallet,
        )

    def test_parse_json_object_rejects_invalid_json(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be valid JSON"):
            client_handlers._parse_json_object(
                "{bad",
                flag_name="--kwargs-json",
            )

    def test_client_wallet_generate_hides_private_key_by_default(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = main(["client", "wallet", "generate"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["address"], payload["public_key"])
        self.assertNotIn("private_key", payload)

    def test_client_wallet_generate_can_emit_and_write_private_key(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = Path(tmp_dir) / "wallet.key"
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "wallet",
                        "generate",
                        "--include-private-key",
                        "--private-key-out",
                        str(out_path),
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(len(payload["private_key"]), 64)
            self.assertEqual(
                out_path.read_text(encoding="utf-8").strip(),
                payload["private_key"],
            )

    def test_client_query_nonce(self) -> None:
        with patch(
            "xian_cli.client.handlers.tx_api.get_nonce_async",
            AsyncMock(return_value=12),
        ) as get_nonce:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "query",
                        "nonce",
                        "--node-url",
                        "http://node.example",
                        "alice",
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload, {"address": "alice", "next_nonce": 12})
        get_nonce.assert_awaited_once_with("http://node.example", "alice")

    def test_client_query_balance_serializes_runtime_types(self) -> None:
        fake_client = _FakeContextClient(balance=ContractingDecimal("12.50"))
        with patch(
            "xian_cli.client.handlers._make_client",
            return_value=fake_client,
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "query",
                        "balance",
                        "--node-url",
                        "http://node.example",
                        "alice",
                        "--contract",
                        "currency",
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["balance"], "12.5")
        self.assertEqual(payload["address"], "alice")
        self.assertEqual(payload["contract"], "currency")

    def test_client_query_tx_serializes_receipt_dataclass(self) -> None:
        receipt = TransactionReceipt.from_lookup(
            {
                "success": True,
                "tx_hash": "ABC123",
                "message": {"ok": True},
                "transaction": {"hash": "ABC123"},
                "execution": {"status_code": 0},
            }
        )
        fake_client = _FakeContextClient(tx=receipt)
        with patch(
            "xian_cli.client.handlers._make_client",
            return_value=fake_client,
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "query",
                        "tx",
                        "--node-url",
                        "http://node.example",
                        "ABC123",
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["success"])
        self.assertEqual(payload["tx_hash"], "ABC123")
        self.assertEqual(payload["execution"]["status_code"], 0)

    def test_client_query_block_by_height_serializes_dataclass(self) -> None:
        block = IndexedBlock.from_dict(
            {
                "height": 7,
                "hash": "BLOCK123",
                "tx_count": 2,
                "app_hash": "APP123",
                "block_time_iso": "2026-01-02T03:04:05Z",
            }
        )
        fake_client = _FakeContextClient(block=block)
        with patch(
            "xian_cli.client.handlers._make_client",
            return_value=fake_client,
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "query",
                        "block",
                        "--node-url",
                        "http://node.example",
                        "--height",
                        "7",
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["height"], 7)
        self.assertEqual(payload["block_hash"], "BLOCK123")

    def test_client_query_block_by_hash_uses_hash_lookup(self) -> None:
        block = IndexedBlock.from_dict({"height": 7, "hash": "BLOCK123"})
        fake_client = _FakeContextClient(block=block)
        with patch(
            "xian_cli.client.handlers._make_client",
            return_value=fake_client,
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "query",
                        "block",
                        "--node-url",
                        "http://node.example",
                        "--block-hash",
                        "BLOCK123",
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["block_hash"], "BLOCK123")

    def test_client_call_passes_json_kwargs(self) -> None:
        fake_client = _FakeContextClient(call={"ok": True})
        with patch(
            "xian_cli.client.handlers._make_client",
            return_value=fake_client,
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "call",
                        "--node-url",
                        "http://node.example",
                        "currency",
                        "balance_of",
                        "--kwargs-json",
                        '{"address":"alice"}',
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["result"], {"ok": True})
        self.assertEqual(
            fake_client.call_calls,
            [("currency", "balance_of", {"address": "alice"})],
        )

    def test_client_call_rejects_non_object_kwargs(self) -> None:
        with self.assertRaisesRegex(ValueError, "must decode to a JSON object"):
            main(
                [
                    "client",
                    "call",
                    "--node-url",
                    "http://node.example",
                    "currency",
                    "balance_of",
                    "--kwargs-json",
                    '["alice"]',
                ]
            )

    def test_client_simulate_returns_payload(self) -> None:
        fake_client = _FakeContextClient(simulate={"status_code": 0, "chi_used": 17})
        with patch(
            "xian_cli.client.handlers._make_client",
            return_value=fake_client,
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "simulate",
                        "--node-url",
                        "http://node.example",
                        "currency",
                        "transfer",
                        "--kwargs-json",
                        '{"amount":1,"to":"bob"}',
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status_code"], 0)
        self.assertEqual(payload["chi_used"], 17)

    def test_client_tx_send_uses_submission_args(self) -> None:
        fake_client = _FakeContextClient(
            send_tx={
                "submitted": True,
                "tx_hash": "ABC123",
                "mode": "checktx",
            }
        )
        with (
            patch(
                "xian_cli.client.handlers._make_client",
                return_value=fake_client,
            ),
            patch(
                "xian_cli.client.handlers._build_wallet",
                return_value=object(),
            ),
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "tx",
                        "send",
                        "--node-url",
                        "http://node.example",
                        "currency",
                        "approve",
                        "--kwargs-json",
                        '{"to":"bob","amount":7}',
                        "--mode",
                        "checktx",
                        "--wait-for-tx",
                        "--timeout-seconds",
                        "5",
                        "--chi",
                        "123",
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["tx_hash"], "ABC123")
        self.assertEqual(len(fake_client.send_tx_calls), 1)
        call = fake_client.send_tx_calls[0]
        self.assertEqual(call["contract"], "currency")
        self.assertEqual(call["function"], "approve")
        self.assertEqual(call["kwargs"], {"to": "bob", "amount": 7})
        self.assertEqual(call["mode"], "checktx")
        self.assertTrue(call["wait_for_tx"])
        self.assertEqual(call["timeout_seconds"], 5.0)
        self.assertEqual(call["chi"], 123)

    def test_client_tx_submit_artifacts_uses_artifact_module_name(self) -> None:
        fake_client = _FakeContextClient(
            submit_contract={
                "submitted": True,
                "tx_hash": "DEF456",
                "mode": "checktx",
            }
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            artifact_path = Path(tmp_dir) / "con_counter.artifacts.json"
            artifact_path.write_text(
                json.dumps(
                    {
                        "format": "xian_contract_artifact_v1",
                        "module_name": "con_counter",
                        "vm_profile": "xian_vm_v1",
                        "source": "counter = Variable()\n",
                        "vm_ir_json": "{}",
                        "hashes": {},
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch(
                    "xian_cli.client.handlers._make_client",
                    return_value=fake_client,
                ),
                patch(
                    "xian_cli.client.handlers._build_wallet",
                    return_value=object(),
                ),
            ):
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "client",
                            "tx",
                            "submit-artifacts",
                            "--node-url",
                            "http://node.example",
                            str(artifact_path),
                            "--args-json",
                            '{"seed":7}',
                            "--mode",
                            "checktx",
                            "--nonce",
                            "42",
                        ]
                    )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["tx_hash"], "DEF456")
        self.assertEqual(len(fake_client.submit_contract_calls), 1)
        call = fake_client.submit_contract_calls[0]
        self.assertEqual(call["name"], "con_counter")
        self.assertEqual(call["args"], {"seed": 7})
        self.assertEqual(call["mode"], "checktx")
        self.assertEqual(call["nonce"], 42)
        self.assertEqual(
            call["deployment_artifacts"]["format"],
            "xian_contract_artifact_v1",
        )

    def test_client_tx_submit_artifacts_rejects_name_mismatch(self) -> None:
        fake_client = _FakeContextClient(submit_contract={})
        with tempfile.TemporaryDirectory() as tmp_dir:
            artifact_path = Path(tmp_dir) / "artifacts.json"
            artifact_path.write_text(
                json.dumps({"module_name": "con_counter"}),
                encoding="utf-8",
            )

            with (
                patch(
                    "xian_cli.client.handlers._make_client",
                    return_value=fake_client,
                ),
                patch(
                    "xian_cli.client.handlers._build_wallet",
                    return_value=object(),
                ),
            ):
                with self.assertRaisesRegex(ValueError, "module_name"):
                    main(
                        [
                            "client",
                            "tx",
                            "submit-artifacts",
                            "--node-url",
                            "http://node.example",
                            str(artifact_path),
                            "--name",
                            "con_other",
                        ]
                    )

        self.assertEqual(fake_client.submit_contract_calls, [])

    def test_client_tx_transfer(self) -> None:
        fake_client = _FakeContextClient(
            send={
                "submitted": True,
                "tx_hash": "XYZ789",
                "mode": "async",
            }
        )
        with (
            patch(
                "xian_cli.client.handlers._make_client",
                return_value=fake_client,
            ),
            patch(
                "xian_cli.client.handlers._build_wallet",
                return_value=object(),
            ),
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "tx",
                        "transfer",
                        "--node-url",
                        "http://node.example",
                        "bob",
                        "1.25",
                        "--token",
                        "currency",
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["tx_hash"], "XYZ789")
        self.assertEqual(len(fake_client.send_calls), 1)
        call = fake_client.send_calls[0]
        self.assertEqual(call["amount"], "1.25")
        self.assertEqual(call["to_address"], "bob")
        self.assertEqual(call["token"], "currency")

    def test_client_tx_transfer_uses_private_key_env(self) -> None:
        account = Ed25519Account.generate()
        fake_client = _FakeContextClient(
            send={"submitted": True, "tx_hash": "XYZ789", "mode": "async"}
        )
        with (
            patch(
                "xian_cli.client.handlers._make_client",
                return_value=fake_client,
            ),
            patch.dict(
                "os.environ",
                {
                    "XIAN_PRIVATE_KEY": account.private_key,
                    "XIAN_NODE_URL": "http://node.example",
                },
            ),
        ):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "client",
                        "tx",
                        "transfer",
                        "--private-key-env",
                        "XIAN_PRIVATE_KEY",
                        "bob",
                        "2",
                    ]
                )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["tx_hash"], "XYZ789")

    def test_client_tx_send_requires_private_key(self) -> None:
        with self.assertRaisesRegex(ValueError, "private key is required"):
            main(
                [
                    "client",
                    "tx",
                    "send",
                    "--node-url",
                    "http://node.example",
                    "currency",
                    "approve",
                ]
            )

    def test_console_main_reports_operational_errors_without_traceback(
        self,
    ) -> None:
        stderr = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                ["xian", "client", "query", "balance", "alice"],
            ),
            redirect_stderr(stderr),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 1)
        error = stderr.getvalue()
        self.assertIn("xian: error:", error)
        self.assertIn("node URL is required", error)
        self.assertNotIn("Traceback", error)


class ClientOutputTests(unittest.TestCase):
    def test_to_jsonable_handles_runtime_types_and_bytes(self) -> None:
        receipt = TransactionReceipt.from_lookup(
            {
                "success": True,
                "tx_hash": "ABC123",
                "message": ContractingDecimal("1.5"),
                "transaction": {"created": Datetime(2026, 1, 2, 3, 4, 5)},
                "execution": {"payload": b"hello", "raw": b"\xff"},
            }
        )

        payload = cli_output.to_jsonable(receipt)
        self.assertEqual(payload["message"], "1.5")
        self.assertEqual(
            payload["transaction"]["created"],
            "2026-01-02 03:04:05",
        )
        self.assertEqual(payload["execution"]["payload"], "hello")
        self.assertEqual(payload["execution"]["raw"], "ff")

    def test_emit_json_writes_newline_terminated_json(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            cli_output.emit_json({"ok": True})

        self.assertEqual(stdout.getvalue(), '{\n  "ok": true\n}\n')

    def test_to_jsonable_handles_lists_and_tuples(self) -> None:
        payload = cli_output.to_jsonable({"items": [1, ("a", b"\xff")]})
        self.assertEqual(payload, {"items": [1, ["a", "ff"]]})


class NetworkManifestTests(unittest.TestCase):
    def test_network_template_list_reads_canonical_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            templates_dir = configs_dir / "templates"
            templates_dir.mkdir(parents=True)
            (templates_dir / "single-node-dev.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "single-node-dev",
                        "display_name": "Single-Node Dev",
                        "description": "Local dev template",
                        "block_policy_mode": "on_demand",
                        "block_policy_interval": "0s",
                        "operator_profile": "local_development",
                        "monitoring_profile": "none",
                        "bootstrap_node_name": "validator-1",
                        "additional_validator_names": [],
                        "services": _template_services(
                            bds_enabled=False,
                            dashboard_enabled=True,
                            monitoring_enabled=False,
                            dashboard_host="0.0.0.0",
                            dashboard_port=18080,
                        ),
                        "pruning_enabled": False,
                        "blocks_to_keep": 100000,
                    }
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "network",
                        "template",
                        "list",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(configs_dir),
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(len(payload), 1)
            self.assertEqual(payload[0]["name"], "single-node-dev")
            self.assertEqual(payload[0]["operator_profile"], "local_development")
            self.assertTrue(payload[0]["services"]["dashboard"]["enabled"])

    def test_network_create_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "local.json"
            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--output",
                        str(output_path),
                    ]
                )
            self.assertEqual(exit_code, 0)
            manifest = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["name"], "local-dev")
            self.assertEqual(manifest["chain_id"], "xian-local-1")
            self.assertNotIn("mode", manifest)
            self.assertNotIn("runtime_backend", manifest)
            self.assertEqual(
                manifest["genesis"],
                {"kind": "bundle", "bundle": "local"},
            )
            self.assertEqual(manifest["p2p"]["seeds"], [])
            self.assertEqual(manifest["block_policy_mode"], "on_demand")
            self.assertEqual(manifest["block_policy_interval"], "0s")
            self.assertNotIn("tracer_mode", manifest)
            self.assertEqual(manifest["node_image_mode"], "local_build")
            self.assertIsNone(manifest["node_integrated_image"])
            self.assertIsNone(manifest["node_split_image"])
            self.assertIsNone(manifest["node_release_manifest"])

    def test_network_create_defaults_to_network_first_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--base-dir",
                        str(base_dir),
                        "--chain-id",
                        "xian-local-1",
                    ]
                )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            manifest_path = base_dir / "networks" / "local-dev" / "manifest.json"
            self.assertEqual(
                result["manifest_path"],
                str(manifest_path.resolve()),
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertNotIn("mode", manifest)
            self.assertEqual(
                manifest["genesis"],
                {"kind": "bundle", "bundle": "local"},
            )
            self.assertEqual(manifest["block_policy_mode"], "on_demand")
            self.assertEqual(manifest["block_policy_interval"], "0s")
            self.assertNotIn("tracer_mode", manifest)
            self.assertEqual(manifest["node_image_mode"], "local_build")

    def test_network_create_dry_run_validates_without_writing_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--base-dir",
                        str(base_dir),
                        "--chain-id",
                        "xian-local-1",
                        "--dry-run",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            manifest_path = base_dir / "networks" / "local-dev" / "manifest.json"
            self.assertTrue(payload["dry_run"])
            self.assertEqual(
                payload["manifest_path"],
                str(manifest_path.resolve()),
            )
            self.assertFalse(manifest_path.exists())

    def test_read_network_manifest_accepts_bundle_built_genesis(self) -> None:
        manifest = read_network_manifest(CANONICAL_DEVNET_MANIFEST)
        self.assertEqual(manifest["genesis"]["kind"], "bundle")
        self.assertEqual(manifest["genesis"]["bundle"], "devnet")
        self.assertEqual(
            manifest["genesis"]["genesis_time"],
            "2026-03-30T00:00:00.000000Z",
        )

    def test_read_network_manifest_accepts_privacy_policy_surfaces(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manifest_path = Path(tmp_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    _manifest_payload(
                        name="privacy-test",
                        chain_id="xian-privacy-1",
                        genesis={
                            "kind": "bundle",
                            "bundle": "devnet",
                            "genesis_time": ("2026-03-30T00:00:00.000000Z"),
                        },
                        node_image_mode="local_build",
                        shielded_relayers=[],
                        privacy_artifact_catalog={
                            "path": "./privacy/artifacts.json",
                            "sha256": "a" * 64,
                        },
                        shielded_history_policy={
                            "feed_version": 1,
                            "compatibility_commitment": "versioned",
                            "retention_class": "archive",
                            "bds_snapshot_support": True,
                            "operator_notice": ("retain encrypted payload history"),
                        },
                        privacy_submission_policy={
                            "disclosure_policy": "user_controlled",
                            "shared_relayer_auth_required": True,
                            "hidden_sender_submission_mode": "relayer_optional",
                            "operator_notice": ("shared relayers require operator auth"),
                        },
                        block_policy_mode="on_demand",
                        block_policy_interval="0s",
                    )
                ),
                encoding="utf-8",
            )

            manifest = read_network_manifest(manifest_path)

        self.assertEqual(
            manifest["privacy_artifact_catalog"]["path"],
            "./privacy/artifacts.json",
        )
        self.assertEqual(
            manifest["shielded_history_policy"]["compatibility_commitment"],
            "versioned",
        )
        self.assertEqual(
            manifest["privacy_submission_policy"]["hidden_sender_submission_mode"],
            "relayer_optional",
        )

    def test_read_network_manifest_rejects_incomplete_registry_image_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manifest_path = Path(tmp_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    _manifest_payload(
                        node_image_mode="registry",
                        node_integrated_image=("ghcr.io/xian-technology/xian-node@sha256:abc"),
                        block_policy_mode="on_demand",
                        block_policy_interval="0s",
                    )
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "requires both node_integrated_image"):
                read_network_manifest(manifest_path)

    def test_network_create_uses_template_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            templates_dir = configs_dir / "templates"
            templates_dir.mkdir(parents=True)
            (templates_dir / "single-node-indexed.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "single-node-indexed",
                        "display_name": "Single-Node Indexed",
                        "description": "Indexed single node",
                        "block_policy_mode": "on_demand",
                        "block_policy_interval": "0s",
                        "transaction_trace_logging": True,
                        "app_log_level": "DEBUG",
                        "app_log_json": True,
                        "app_log_rotation_hours": 4,
                        "app_log_retention_days": 10,
                        "simulation_enabled": False,
                        "simulation_max_concurrency": 3,
                        "simulation_timeout_ms": 2500,
                        "simulation_max_chi": 500000,
                        "tx_fee_mode": "free_metered",
                        "free_tx_max_chi": 250000,
                        "free_block_max_chi": 1000000,
                        "parallel_execution_enabled": True,
                        "parallel_execution_workers": 4,
                        "parallel_execution_min_transactions": 12,
                        "operator_profile": "indexed_development",
                        "monitoring_profile": "local_stack",
                        "bootstrap_node_name": "validator-1",
                        "additional_validator_names": [],
                        "services": _template_services(
                            bds_enabled=True,
                            dashboard_enabled=True,
                            monitoring_enabled=True,
                            dashboard_host="0.0.0.0",
                            dashboard_port=18080,
                        ),
                        "advanced": {
                            "metrics": {
                                "host": "0.0.0.0",
                            },
                        },
                        "pruning_enabled": False,
                        "blocks_to_keep": 100000,
                    }
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(configs_dir),
                        "--chain-id",
                        "xian-local-1",
                        "--template",
                        "single-node-indexed",
                        "--generate-validator-key",
                        "--genesis-source",
                        str(TEST_FIXTURE_GENESIS),
                    ]
                )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            manifest = json.loads(
                (base_dir / "networks" / "local-dev" / "manifest.json").read_text(encoding="utf-8")
            )
            profile = json.loads(
                (base_dir / "nodes" / "validator-1.json").read_text(encoding="utf-8")
            )

            self.assertEqual(result["template"], "single-node-indexed")
            self.assertNotIn("tracer_mode", manifest)
            self.assertTrue(profile["services"]["bds"]["enabled"])
            self.assertTrue(profile["transaction_trace_logging"])
            self.assertEqual(profile["app_log_level"], "DEBUG")
            self.assertTrue(profile["app_log_json"])
            self.assertEqual(profile["app_log_rotation_hours"], 4)
            self.assertEqual(profile["app_log_retention_days"], 10)
            self.assertFalse(profile["simulation_enabled"])
            self.assertEqual(profile["simulation_max_concurrency"], 3)
            self.assertEqual(profile["simulation_timeout_ms"], 2500)
            self.assertEqual(profile["simulation_max_chi"], 500000)
            self.assertEqual(profile["tx_fee_mode"], "free_metered")
            self.assertEqual(profile["free_tx_max_chi"], 250000)
            self.assertEqual(profile["free_block_max_chi"], 1000000)
            self.assertTrue(profile["parallel_execution_enabled"])
            self.assertEqual(profile["parallel_execution_workers"], 4)
            self.assertEqual(profile["parallel_execution_min_transactions"], 12)
            self.assertEqual(profile["operator_profile"], "indexed_development")
            self.assertEqual(profile["monitoring_profile"], "local_stack")
            self.assertTrue(profile["services"]["dashboard"]["enabled"])
            self.assertTrue(profile["services"]["monitoring"]["enabled"])
            self.assertEqual(profile["services"]["dashboard"]["host"], "0.0.0.0")
            self.assertEqual(profile["services"]["dashboard"]["port"], 18080)
            self.assertEqual(profile["advanced"]["metrics"]["host"], "0.0.0.0")
            self.assertEqual(profile["advanced"]["metrics"]["bds_refresh_seconds"], 5.0)
            self.assertEqual(
                profile["advanced"]["parallel_execution"]["max_speculative_waves"],
                4,
            )

    def test_network_package_operator_bundle_materializes_shareable_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            network_dir = base_dir / "networks" / "private-dev"
            privacy_dir = network_dir / "privacy"
            privacy_dir.mkdir(parents=True)
            (network_dir / "genesis.json").write_text(
                TEST_FIXTURE_GENESIS.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (privacy_dir / "artifacts.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "network": "private-dev",
                        "bundle_policy": {
                            "approved_setup_modes": ["single-party"],
                            "allow_single_party": True,
                        },
                        "artifacts": [],
                    }
                ),
                encoding="utf-8",
            )
            manifest_path = network_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    _manifest_payload(
                        name="private-dev",
                        chain_id="xian-private-dev-1",
                        node_image_mode="local_build",
                        snapshot_signing_keys=[],
                        block_policy_mode="idle_interval",
                        block_policy_interval="5s",
                        privacy_artifact_catalog={
                            "path": "./privacy/artifacts.json",
                            "sha256": "a" * 64,
                        },
                    )
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "network",
                        "package-operator-bundle",
                        "private-dev",
                        "--base-dir",
                        str(base_dir),
                        "--bootstrap-seed",
                        "abc@seed.example:26656",
                    ]
                )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            bundle_dir = Path(result["bundle_dir"])
            bundled_manifest = json.loads(
                (bundle_dir / "manifest.json").read_text(encoding="utf-8")
            )
            join_script = (bundle_dir / "participant-join.sh").read_text(encoding="utf-8")

            self.assertEqual(
                bundled_manifest["genesis"],
                {"kind": "source", "source": "./genesis.json"},
            )
            self.assertEqual(
                bundled_manifest["p2p"]["seeds"],
                ["abc@seed.example:26656"],
            )
            self.assertNotIn("runtime_backend", bundled_manifest)
            self.assertNotIn("tracer_mode", bundled_manifest)
            self.assertTrue((bundle_dir / "genesis.json").exists())
            self.assertTrue((bundle_dir / "privacy" / "artifacts.json").exists())
            self.assertEqual(
                (bundle_dir / "bootstrap-seed.txt").read_text(encoding="utf-8"),
                "abc@seed.example:26656\n",
            )
            self.assertIn("--parallel-execution-enabled", join_script)
            self.assertNotIn("--tracer-mode", join_script)
            self.assertNotIn("--runtime-backend", join_script)
            self.assertTrue(os.access(bundle_dir / "participant-join.sh", os.X_OK))
            self.assertTrue(os.access(bundle_dir / "participant-bds-node.sh", os.X_OK))

    def test_network_join_writes_node_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "validator-1.json"
            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--network",
                        "devnet",
                        "--seed",
                        "abc@127.0.0.1:26656",
                        "--enable-bds",
                        "--enable-dashboard",
                        "--dashboard-host",
                        "0.0.0.0",
                        "--dashboard-port",
                        "18080",
                        "--output",
                        str(output_path),
                    ]
                )
            self.assertEqual(exit_code, 0)
            profile = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(profile["schema_version"], 1)
            self.assertEqual(profile["name"], "validator-1")
            self.assertEqual(profile["network"], "devnet")
            self.assertEqual(profile["moniker"], "validator-1")
            self.assertEqual(profile["p2p"]["seeds"], ["abc@127.0.0.1:26656"])
            self.assertTrue(profile["services"]["bds"]["enabled"])
            self.assertTrue(profile["services"]["dashboard"]["enabled"])
            self.assertEqual(profile["services"]["dashboard"]["host"], "0.0.0.0")
            self.assertEqual(profile["services"]["dashboard"]["port"], 18080)
            self.assertEqual(profile["block_policy_mode"], "idle_interval")
            self.assertEqual(profile["block_policy_interval"], "5s")
            self.assertNotIn("tracer_mode", profile)
            self.assertEqual(profile["node_image_mode"], "local_build")
            self.assertIsNone(profile["node_integrated_image"])
            self.assertIsNone(profile["node_split_image"])

    def test_network_join_dry_run_validates_without_writing_profile(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "devnet",
                        "--dry-run",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            profile_path = base_dir / "nodes" / "validator-1.json"
            self.assertTrue(payload["dry_run"])
            self.assertEqual(
                payload["node_profile_path"],
                str(profile_path.resolve()),
            )
            self.assertFalse(profile_path.exists())

    def test_network_create_writes_dashboard_settings_to_bootstrap_profile(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            key_dir = base_dir / "keys" / "local-dev"
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "keys",
                            "validator",
                            "generate",
                            "--out-dir",
                            str(key_dir),
                        ]
                    ),
                    0,
                )
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--base-dir",
                        str(base_dir),
                        "--chain-id",
                        "xian-local-1",
                        "--bootstrap-node",
                        "local-dev",
                        "--validator-key-ref",
                        str(key_dir / "validator_key_info.json"),
                        "--genesis-source",
                        str(TEST_FIXTURE_GENESIS),
                        "--enable-dashboard",
                        "--dashboard-host",
                        "0.0.0.0",
                        "--dashboard-port",
                        "18080",
                    ]
                )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            profile_path = Path(result["validators"][0]["profile_path"])
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertTrue(profile["services"]["dashboard"]["enabled"])
            self.assertEqual(profile["services"]["dashboard"]["host"], "0.0.0.0")
            self.assertEqual(profile["services"]["dashboard"]["port"], 18080)

    def test_network_join_uses_canonical_manifest_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(
                    _manifest_payload(
                        snapshot_url="https://example.invalid/snapshot",
                        p2p_seeds=["seed-1@127.0.0.1:26656"],
                        block_policy_mode="periodic",
                        block_policy_interval="10s",
                        node_image_mode="registry",
                        node_integrated_image=("ghcr.io/xian-technology/xian-node@sha256:abc"),
                        node_split_image=("ghcr.io/xian-technology/xian-node-split@sha256:def"),
                        node_release_manifest=(CANONICAL_NODE_RELEASE_MANIFEST),
                    )
                ),
                encoding="utf-8",
            )

            output_path = base_dir / "nodes" / "validator-1.json"
            with patch.dict("os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}):
                with redirect_stdout(io.StringIO()):
                    exit_code = main(
                        [
                            "network",
                            "join",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                            "--network",
                            "canonical",
                            "--output",
                            str(output_path),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            profile = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertNotIn("runtime_backend", profile)
            self.assertEqual(profile["p2p"]["seeds"], [])
            self.assertIsNone(profile["snapshot_url"])
            self.assertEqual(profile["block_policy_mode"], "periodic")
            self.assertEqual(profile["block_policy_interval"], "10s")
            self.assertNotIn("tracer_mode", profile)
            self.assertEqual(profile["node_image_mode"], "registry")
            self.assertEqual(
                profile["node_integrated_image"],
                "ghcr.io/xian-technology/xian-node@sha256:abc",
            )
            self.assertEqual(
                profile["node_split_image"],
                "ghcr.io/xian-technology/xian-node-split@sha256:def",
            )
            self.assertEqual(
                profile["node_release_manifest"],
                _normalized_release_manifest(CANONICAL_NODE_RELEASE_MANIFEST),
            )

    def test_network_join_uses_template_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            templates_dir = configs_dir / "templates"
            network_dir.mkdir(parents=True)
            templates_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(
                    _manifest_payload(
                        block_policy_mode="on_demand",
                        block_policy_interval="0s",
                    )
                ),
                encoding="utf-8",
            )
            (templates_dir / "single-node-indexed.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "single-node-indexed",
                        "display_name": "Single-Node Indexed",
                        "description": "Indexed local node defaults",
                        "block_policy_mode": "on_demand",
                        "block_policy_interval": "0s",
                        "transaction_trace_logging": True,
                        "app_log_level": "WARNING",
                        "app_log_json": True,
                        "app_log_rotation_hours": 6,
                        "app_log_retention_days": 12,
                        "simulation_enabled": True,
                        "simulation_max_concurrency": 4,
                        "simulation_timeout_ms": 4000,
                        "simulation_max_chi": 800000,
                        "parallel_execution_enabled": True,
                        "parallel_execution_workers": 3,
                        "parallel_execution_min_transactions": 9,
                        "operator_profile": "indexed_development",
                        "monitoring_profile": "local_stack",
                        "bootstrap_node_name": "validator-1",
                        "additional_validator_names": [],
                        "services": _template_services(
                            bds_enabled=True,
                            dashboard_enabled=False,
                            monitoring_enabled=True,
                            dashboard_host="127.0.0.1",
                            dashboard_port=8080,
                        ),
                        "pruning_enabled": False,
                        "blocks_to_keep": 100000,
                    }
                ),
                encoding="utf-8",
            )

            output_path = base_dir / "nodes" / "validator-1.json"
            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(configs_dir),
                        "--network",
                        "canonical",
                        "--template",
                        "single-node-indexed",
                        "--output",
                        str(output_path),
                    ]
                )

            self.assertEqual(exit_code, 0)
            profile = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertTrue(profile["services"]["bds"]["enabled"])
            self.assertTrue(profile["transaction_trace_logging"])
            self.assertEqual(profile["app_log_level"], "WARNING")
            self.assertTrue(profile["app_log_json"])
            self.assertEqual(profile["app_log_rotation_hours"], 6)
            self.assertEqual(profile["app_log_retention_days"], 12)
            self.assertTrue(profile["simulation_enabled"])
            self.assertEqual(profile["simulation_max_concurrency"], 4)
            self.assertEqual(profile["simulation_timeout_ms"], 4000)
            self.assertEqual(profile["simulation_max_chi"], 800000)
            self.assertTrue(profile["parallel_execution_enabled"])
            self.assertEqual(profile["parallel_execution_workers"], 3)
            self.assertEqual(profile["parallel_execution_min_transactions"], 9)
            self.assertEqual(profile["operator_profile"], "indexed_development")
            self.assertEqual(profile["monitoring_profile"], "local_stack")
            self.assertTrue(profile["services"]["monitoring"]["enabled"])
            self.assertFalse(profile["services"]["dashboard"]["enabled"])
            self.assertNotIn("tracer_mode", profile)

    def test_network_join_drops_release_manifest_for_local_build_override(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(
                    _manifest_payload(
                        block_policy_mode="on_demand",
                        block_policy_interval="0s",
                        node_image_mode="registry",
                        node_integrated_image=("ghcr.io/xian-technology/xian-node@sha256:abc"),
                        node_split_image=("ghcr.io/xian-technology/xian-node-split@sha256:def"),
                        node_release_manifest=(CANONICAL_NODE_RELEASE_MANIFEST),
                    )
                ),
                encoding="utf-8",
            )

            output_path = base_dir / "nodes" / "validator-1.json"
            with patch.dict("os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}):
                with redirect_stdout(io.StringIO()):
                    exit_code = main(
                        [
                            "network",
                            "join",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                            "--network",
                            "canonical",
                            "--node-image-mode",
                            "local_build",
                            "--output",
                            str(output_path),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            profile = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(profile["node_image_mode"], "local_build")
            self.assertIsNone(profile["node_integrated_image"])
            self.assertIsNone(profile["node_split_image"])
            self.assertIsNone(profile["node_release_manifest"])

    def test_network_join_allows_block_policy_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(
                    _manifest_payload(
                        block_policy_mode="on_demand",
                        block_policy_interval="0s",
                    )
                ),
                encoding="utf-8",
            )

            output_path = base_dir / "nodes" / "validator-1.json"
            with patch.dict("os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}):
                with redirect_stdout(io.StringIO()):
                    exit_code = main(
                        [
                            "network",
                            "join",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                            "--network",
                            "canonical",
                            "--block-policy-mode",
                            "idle_interval",
                            "--block-policy-interval",
                            "10s",
                            "--transaction-trace-logging",
                            "--app-log-level",
                            "ERROR",
                            "--app-log-json",
                            "--app-log-rotation-hours",
                            "5",
                            "--app-log-retention-days",
                            "15",
                            "--simulation-enabled",
                            "--simulation-max-concurrency",
                            "5",
                            "--simulation-timeout-ms",
                            "3500",
                            "--simulation-max-chi",
                            "900000",
                            "--tx-fee-mode",
                            "free_metered",
                            "--free-tx-max-chi",
                            "300000",
                            "--free-block-max-chi",
                            "900000",
                            "--parallel-execution-enabled",
                            "--parallel-execution-workers",
                            "6",
                            "--parallel-execution-min-transactions",
                            "14",
                            "--output",
                            str(output_path),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            profile = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(profile["block_policy_mode"], "idle_interval")
            self.assertEqual(profile["block_policy_interval"], "10s")
            self.assertNotIn("tracer_mode", profile)
            self.assertTrue(profile["transaction_trace_logging"])
            self.assertEqual(profile["app_log_level"], "ERROR")
            self.assertTrue(profile["app_log_json"])
            self.assertEqual(profile["app_log_rotation_hours"], 5)
            self.assertEqual(profile["app_log_retention_days"], 15)
            self.assertTrue(profile["simulation_enabled"])
            self.assertEqual(profile["simulation_max_concurrency"], 5)
            self.assertEqual(profile["simulation_timeout_ms"], 3500)
            self.assertEqual(profile["simulation_max_chi"], 900000)
            self.assertEqual(profile["tx_fee_mode"], "free_metered")
            self.assertEqual(profile["free_tx_max_chi"], 300000)
            self.assertEqual(profile["free_block_max_chi"], 900000)
            self.assertTrue(profile["parallel_execution_enabled"])
            self.assertEqual(profile["parallel_execution_workers"], 6)
            self.assertEqual(profile["parallel_execution_min_transactions"], 14)

    def test_network_join_allows_node_local_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(
                    _manifest_payload(
                        p2p_seeds=["seed-1@127.0.0.1:26656"],
                    )
                ),
                encoding="utf-8",
            )

            output_path = base_dir / "nodes" / "validator-1.json"
            with patch.dict("os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}):
                with redirect_stdout(io.StringIO()):
                    exit_code = main(
                        [
                            "network",
                            "join",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                            "--network",
                            "canonical",
                            "--seed",
                            "local-seed@127.0.0.1:26656",
                            "--snapshot-url",
                            "https://example.invalid/node-snapshot",
                            "--output",
                            str(output_path),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            profile = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertNotIn("runtime_backend", profile)
            self.assertEqual(profile["p2p"]["seeds"], ["local-seed@127.0.0.1:26656"])
            self.assertEqual(
                profile["snapshot_url"],
                "https://example.invalid/node-snapshot",
            )
            self.assertNotIn("tracer_mode", profile)

    def test_network_join_rejects_negative_parallel_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(_manifest_payload()),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "parallel_execution_workers must be a non-negative integer",
            ):
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(configs_dir),
                        "--network",
                        "canonical",
                        "--parallel-execution-workers",
                        "-1",
                    ]
                )

    def test_network_join_rejects_non_positive_simulation_settings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(_manifest_payload()),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "simulation_max_concurrency must be a positive integer",
            ):
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(configs_dir),
                        "--network",
                        "canonical",
                        "--simulation-max-concurrency",
                        "0",
                    ]
                )

    def test_network_join_rejects_invalid_free_fee_caps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(_manifest_payload()),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "free_block_max_chi must be greater than or equal to free_tx_max_chi",
            ):
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(configs_dir),
                        "--network",
                        "canonical",
                        "--tx-fee-mode",
                        "free_metered",
                        "--free-tx-max-chi",
                        "1000",
                        "--free-block-max-chi",
                        "999",
                    ]
                )

    def test_network_join_rejects_zero_parallel_execution_min_transactions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(_manifest_payload()),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "parallel_execution_min_transactions must be a positive integer",
            ):
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(configs_dir),
                        "--network",
                        "canonical",
                        "--parallel-execution-min-transactions",
                        "0",
                    ]
                )

    def test_network_join_can_generate_validator_key_material(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(_manifest_payload()),
                encoding="utf-8",
            )

            output_path = base_dir / "nodes" / "validator-1.json"
            with patch.dict("os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}):
                with redirect_stdout(io.StringIO()):
                    exit_code = main(
                        [
                            "network",
                            "join",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                            "--network",
                            "canonical",
                            "--generate-validator-key",
                            "--output",
                            str(output_path),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            profile = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(
                profile["validator_key_ref"],
                "keys/validator-1/validator_key_info.json",
            )
            self.assertTrue(
                (base_dir / "keys" / "validator-1" / "priv_validator_key.json").exists()
            )
            self.assertTrue(
                (base_dir / "keys" / "validator-1" / "validator_key_info.json").exists()
            )

    def test_network_join_requires_known_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaisesRegex(FileNotFoundError, "network manifest"):
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        tmp_dir,
                        "--network",
                        "does-not-exist",
                    ]
                )

    def test_network_join_rejects_validator_key_dir_without_generation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaisesRegex(
                ValueError,
                "--validator-key-dir requires --generate-validator-key",
            ):
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        tmp_dir,
                        "--network",
                        "devnet",
                        "--validator-key-dir",
                        "keys/validator-1",
                    ]
                )

    def test_network_join_rejects_restore_snapshot_without_init_node(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaisesRegex(
                ValueError,
                "--restore-snapshot requires --init-node",
            ):
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        tmp_dir,
                        "--network",
                        "devnet",
                        "--restore-snapshot",
                    ]
                )

    def test_network_join_can_initialize_node_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()

            genesis_source = TEST_FIXTURE_GENESIS

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-testnet-12",
                        "--genesis-source",
                        str(genesis_source),
                        "--output",
                        str(base_dir / "networks" / "local-dev" / "manifest.json"),
                    ]
                )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--generate-validator-key",
                        "--init-node",
                        "--home",
                        str(base_dir / ".cometbft"),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                        "--parallel-execution-enabled",
                        "--parallel-execution-workers",
                        "5",
                        "--parallel-execution-min-transactions",
                        "11",
                    ]
                )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            self.assertTrue(result["node_initialized"])
            home = Path(result["node_init"]["home"])
            self.assertTrue((home / "config" / "config.toml").exists())
            self.assertTrue((home / "config" / "xian.toml").exists())
            self.assertTrue((home / "config" / "genesis.json").exists())
            config_toml = (home / "config" / "config.toml").read_text(encoding="utf-8")
            xian_toml = (home / "config" / "xian.toml").read_text(encoding="utf-8")
            profile = json.loads(
                (base_dir / "nodes" / "validator-1.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                profile["validator_key_ref"],
                "keys/validator-1/validator_key_info.json",
            )
            self.assertNotIn("[xian]", config_toml)
            self.assertIn('metrics_host = "127.0.0.1"', xian_toml)

    def test_network_create_can_bootstrap_local_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            home = base_dir / ".cometbft"
            stdout = io.StringIO()

            fake_genesis = {
                "chain_id": "xian-local-1",
                "validators": [],
                "abci_genesis": {
                    "genesis": [
                        {
                            "key": "currency.balances:founder",
                            "value": ContractingDecimal("1000000.5"),
                        },
                        {
                            "key": "currency.streams:example",
                            "value": Datetime(
                                year=2026,
                                month=1,
                                day=1,
                                hour=0,
                                minute=0,
                            ),
                        },
                    ]
                },
            }
            genesis_builder = type(
                "GenesisBuilder",
                (),
                {
                    "build_local_network_genesis": staticmethod(
                        unittest.mock.Mock(return_value=fake_genesis)
                    )
                },
            )()

            with patch(
                "xian_cli.commands.node_context.get_genesis_builder_module",
                return_value=genesis_builder,
            ):
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "network",
                            "create",
                            "local-dev",
                            "--base-dir",
                            str(base_dir),
                            "--chain-id",
                            "xian-local-1",
                            "--generate-validator-key",
                            "--bootstrap-node",
                            "validator-1",
                            "--init-node",
                            "--home",
                            str(home),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            manifest_path = base_dir / "networks" / "local-dev" / "manifest.json"
            profile_path = base_dir / "nodes" / "validator-1.json"
            genesis_path = base_dir / "networks" / "local-dev" / "genesis.json"

            self.assertEqual(
                result["manifest_path"],
                str(manifest_path.resolve()),
            )
            self.assertEqual(
                result["profile_path"],
                str(profile_path.resolve()),
            )
            self.assertTrue(result["node_initialized"])
            self.assertTrue(genesis_path.exists())
            self.assertTrue((home / "config" / "config.toml").exists())
            self.assertTrue((home / "config" / "xian.toml").exists())
            self.assertTrue(
                (base_dir / "keys" / "validator-1" / "validator_key_info.json").exists()
            )

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            generated_genesis = json.loads(genesis_path.read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["genesis"],
                {"kind": "source", "source": "./genesis.json"},
            )
            self.assertEqual(
                profile["validator_key_ref"],
                "keys/validator-1/validator_key_info.json",
            )
            self.assertEqual(
                generated_genesis["abci_genesis"]["genesis"][0]["value"],
                {"__fixed__": "1000000.5"},
            )
            self.assertEqual(
                generated_genesis["abci_genesis"]["genesis"][1]["value"],
                {"__time__": [2026, 1, 1, 0, 0, 0, 0]},
            )
            (genesis_builder.build_local_network_genesis.assert_called_once())

    def test_network_create_can_generate_multiple_initial_validators(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            templates_dir = configs_dir / "templates"
            templates_dir.mkdir(parents=True)
            (templates_dir / "consortium-2.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "consortium-2",
                        "display_name": "Consortium 2",
                        "description": "Two-validator template",
                        "block_policy_mode": "idle_interval",
                        "block_policy_interval": "10s",
                        "operator_profile": "shared_network",
                        "monitoring_profile": "bds",
                        "bootstrap_node_name": "validator-1",
                        "additional_validator_names": ["validator-2"],
                        "services": _template_services(
                            bds_enabled=True,
                            dashboard_enabled=True,
                            monitoring_enabled=True,
                            dashboard_host="0.0.0.0",
                            dashboard_port=18080,
                        ),
                        "pruning_enabled": True,
                        "blocks_to_keep": 12345,
                    }
                ),
                encoding="utf-8",
            )
            fake_genesis = {
                "chain_id": "xian-local-1",
                "validators": [
                    {"name": "validator-1"},
                    {"name": "validator-2"},
                ],
                "abci_genesis": {},
            }
            genesis_builder = type(
                "GenesisBuilder",
                (),
                {
                    "build_local_network_genesis": staticmethod(
                        unittest.mock.Mock(return_value=fake_genesis)
                    )
                },
            )()

            stdout = io.StringIO()
            with patch(
                "xian_cli.commands.node_context.get_genesis_builder_module",
                return_value=genesis_builder,
            ):
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "network",
                            "create",
                            "local-dev",
                            "--base-dir",
                            str(base_dir),
                            "--configs-dir",
                            str(configs_dir),
                            "--chain-id",
                            "xian-local-1",
                            "--template",
                            "consortium-2",
                            "--generate-validator-key",
                        ]
                    )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            self.assertEqual(len(result["validators"]), 2)
            self.assertEqual(
                [item["name"] for item in result["validators"]],
                ["validator-1", "validator-2"],
            )
            self.assertTrue(
                (base_dir / "keys" / "validator-2" / "validator_key_info.json").exists()
            )
            validator_two_profile = json.loads(
                (base_dir / "nodes" / "validator-2.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                validator_two_profile["validator_key_ref"],
                "keys/validator-2/validator_key_info.json",
            )
            self.assertEqual(validator_two_profile["operator_profile"], "shared_network")
            self.assertEqual(validator_two_profile["monitoring_profile"], "bds")
            self.assertTrue(validator_two_profile["pruning_enabled"])
            self.assertEqual(validator_two_profile["blocks_to_keep"], 12345)
            self.assertTrue(validator_two_profile["services"]["bds"]["enabled"])
            self.assertTrue(validator_two_profile["services"]["dashboard"]["enabled"])
            self.assertTrue(validator_two_profile["services"]["monitoring"]["enabled"])
            genesis_builder.build_local_network_genesis.assert_called_once()
            kwargs = genesis_builder.build_local_network_genesis.call_args.kwargs
            self.assertEqual(len(kwargs["validators"]), 2)


class AbciBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        abci_bridge.get_node_setup_module.cache_clear()
        abci_bridge.get_node_admin_module.cache_clear()
        abci_bridge.get_genesis_builder_module.cache_clear()

    def tearDown(self) -> None:
        abci_bridge.get_node_setup_module.cache_clear()
        abci_bridge.get_node_admin_module.cache_clear()
        abci_bridge.get_genesis_builder_module.cache_clear()

    def test_bridge_imports_installed_modules(self) -> None:
        node_setup_module = abci_bridge.get_node_setup_module()
        node_admin_module = abci_bridge.get_node_admin_module()

        self.assertEqual(node_setup_module.__name__, "xian.node_setup")
        self.assertEqual(node_admin_module.__name__, "xian.node_admin")

    def test_bridge_errors_when_helpers_are_unavailable(self) -> None:
        original_import_module = importlib.import_module

        def fake_import_module(name: str):
            if name.startswith("xian."):
                raise ModuleNotFoundError(name="xian")
            return original_import_module(name)

        with patch(
            "xian_cli.abci_bridge.import_module",
            side_effect=fake_import_module,
        ):
            with self.assertRaisesRegex(RuntimeError, "xian-abci helpers"):
                abci_bridge.get_node_setup_module()
            with self.assertRaisesRegex(RuntimeError, "xian-abci helpers"):
                abci_bridge.get_node_admin_module()
            with self.assertRaisesRegex(RuntimeError, "xian-abci helpers"):
                abci_bridge.get_genesis_builder_module()


class NodeInitTests(unittest.TestCase):
    def test_node_init_materializes_home_from_canonical_network_manifest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "nodes").mkdir()
            (base_dir / "keys" / "validator-1").mkdir(parents=True)

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "keys",
                        "validator",
                        "generate",
                        "--out-dir",
                        str(base_dir / "keys" / "validator-1"),
                    ]
                )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(WORKSPACE_ROOT / "xian-configs"),
                        "--network",
                        "devnet",
                        "--validator-key-ref",
                        str(base_dir / "keys" / "validator-1" / "validator_key_info.json"),
                        "--init-node",
                        "--home",
                        str(base_dir / ".cometbft"),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            home = Path(result["node_init"]["home"])
            rendered_genesis = json.loads(
                (home / "config" / "genesis.json").read_text(encoding="utf-8")
            )

            self.assertEqual(rendered_genesis["chain_id"], "xian-devnet-1")
            self.assertEqual(
                result["node_init"]["effective_genesis_source"],
                "bundle:devnet",
            )
            self.assertEqual(len(rendered_genesis["validators"]), 2)
            self.assertEqual(
                rendered_genesis["abci_genesis"]["origin"],
                {"sender": "", "signature": ""},
            )

    def test_node_init_materializes_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            (base_dir / "keys" / "validator-1").mkdir(parents=True)

            genesis_source = TEST_FIXTURE_GENESIS

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-testnet-12",
                        "--genesis-source",
                        str(genesis_source),
                        "--seed",
                        "seed1@127.0.0.1:26656",
                        "--output",
                        str(base_dir / "networks" / "local-dev" / "manifest.json"),
                    ]
                )
                main(
                    [
                        "keys",
                        "validator",
                        "generate",
                        "--out-dir",
                        str(base_dir / "keys" / "validator-1"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--validator-key-ref",
                        str(base_dir / "keys" / "validator-1" / "validator_key_info.json"),
                        "--home",
                        str(base_dir / ".cometbft"),
                        "--transaction-trace-logging",
                        "--app-log-level",
                        "WARNING",
                        "--app-log-json",
                        "--app-log-rotation-hours",
                        "8",
                        "--app-log-retention-days",
                        "21",
                        "--simulation-enabled",
                        "--simulation-max-concurrency",
                        "4",
                        "--simulation-timeout-ms",
                        "3200",
                        "--simulation-max-chi",
                        "700000",
                        "--tx-fee-mode",
                        "free_metered",
                        "--free-tx-max-chi",
                        "350000",
                        "--free-block-max-chi",
                        "1400000",
                        "--parallel-execution-enabled",
                        "--parallel-execution-workers",
                        "5",
                        "--parallel-execution-min-transactions",
                        "11",
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            profile_path = base_dir / "nodes" / "validator-1.json"
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["p2p"]["persistent_peers"] = ["peer1@127.0.0.1:26656"]
            profile["services"]["bds"].update(
                {
                    "enabled": True,
                    "host": "postgres",
                    "port": 5544,
                    "database": "xian_index",
                    "user": "indexer",
                    "password": "secret",
                    "pool_min_size": 2,
                    "pool_max_size": 6,
                    "statement_timeout_ms": 5000,
                    "acquire_timeout_ms": 15000,
                    "application_name": "xian-bds-test",
                    "queue_max_size": 321,
                    "catchup_enabled": False,
                    "catchup_poll_seconds": 2.5,
                    "rpc_url": "http://rpc.internal:26657",
                    "spool_dir": "/var/lib/xian/bds-spool",
                    "spool_warn_entries": 512,
                    "spool_warn_bytes": 1_073_741_824,
                    "disk_free_warn_bytes": 4_294_967_296,
                }
            )
            profile["advanced"]["metrics"]["bds_refresh_seconds"] = 7.5
            profile["advanced"]["parallel_execution"]["max_speculative_waves"] = 6
            profile_path.write_text(json.dumps(profile), encoding="utf-8")

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "node",
                        "init",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                    ]
                )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            home = Path(result["home"])
            self.assertTrue((home / "config" / "config.toml").exists())
            self.assertTrue((home / "config" / "xian.toml").exists())
            self.assertTrue((home / "config" / "genesis.json").exists())
            self.assertTrue((home / "config" / "priv_validator_key.json").exists())
            self.assertTrue((home / "config" / "node_key.json").exists())
            self.assertTrue((home / "data" / "priv_validator_state.json").exists())
            config_toml = (home / "config" / "config.toml").read_text(encoding="utf-8")
            xian_toml = (home / "config" / "xian.toml").read_text(encoding="utf-8")
            self.assertNotIn("[xian]", config_toml)
            self.assertIn("transaction_trace_logging = true", xian_toml)
            self.assertIn('app_log_level = "WARNING"', xian_toml)
            self.assertIn("app_log_json = true", xian_toml)
            self.assertIn("app_log_rotation_hours = 8", xian_toml)
            self.assertIn("app_log_retention_days = 21", xian_toml)
            self.assertIn("simulation_enabled = true", xian_toml)
            self.assertIn("simulation_max_concurrency = 4", xian_toml)
            self.assertIn("simulation_timeout_ms = 3200", xian_toml)
            self.assertIn("simulation_max_chi = 700000", xian_toml)
            self.assertIn('tx_fee_mode = "free_metered"', xian_toml)
            self.assertIn("free_tx_max_chi = 350000", xian_toml)
            self.assertIn("free_block_max_chi = 1400000", xian_toml)
            self.assertIn("parallel_execution_enabled = true", xian_toml)
            self.assertIn("parallel_execution_workers = 5", xian_toml)
            self.assertIn(
                "parallel_execution_min_transactions = 11",
                xian_toml,
            )
            rendered_config = tomllib.loads(config_toml)
            rendered_xian = tomllib.loads(xian_toml)
            self.assertEqual(
                rendered_config["p2p"]["persistent_peers"],
                "peer1@127.0.0.1:26656",
            )
            self.assertTrue(rendered_xian["bds_enabled"])
            self.assertEqual(rendered_xian["metrics_bds_refresh_seconds"], 7.5)
            self.assertEqual(rendered_xian["tx_fee_mode"], "free_metered")
            self.assertEqual(rendered_xian["free_tx_max_chi"], 350000)
            self.assertEqual(rendered_xian["free_block_max_chi"], 1400000)
            self.assertEqual(rendered_xian["parallel_execution_max_speculative_waves"], 6)
            self.assertEqual(rendered_xian["bds"]["host"], "postgres")
            self.assertEqual(rendered_xian["bds"]["port"], 5544)
            self.assertEqual(rendered_xian["bds"]["database"], "xian_index")
            self.assertEqual(rendered_xian["bds"]["user"], "indexer")
            self.assertEqual(rendered_xian["bds"]["password"], "secret")
            self.assertEqual(rendered_xian["bds"]["pool_min_size"], 2)
            self.assertEqual(rendered_xian["bds"]["pool_max_size"], 6)
            self.assertEqual(rendered_xian["bds"]["statement_timeout_ms"], 5000)
            self.assertEqual(rendered_xian["bds"]["acquire_timeout_ms"], 15000)
            self.assertEqual(rendered_xian["bds"]["application_name"], "xian-bds-test")
            self.assertEqual(rendered_xian["bds"]["queue_max_size"], 321)
            self.assertFalse(rendered_xian["bds"]["catchup_enabled"])
            self.assertEqual(rendered_xian["bds"]["catchup_poll_seconds"], 2.5)
            self.assertEqual(rendered_xian["bds"]["rpc_url"], "http://rpc.internal:26657")
            self.assertEqual(rendered_xian["bds"]["spool_dir"], "/var/lib/xian/bds-spool")
            self.assertEqual(rendered_xian["bds"]["spool_warn_entries"], 512)
            self.assertEqual(rendered_xian["bds"]["spool_warn_bytes"], 1_073_741_824)
            self.assertEqual(rendered_xian["bds"]["disk_free_warn_bytes"], 4_294_967_296)

    def test_node_init_supports_remote_genesis_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            (base_dir / "keys" / "validator-1").mkdir(parents=True)
            (base_dir / "xian-stack").mkdir()

            genesis_payload = {
                "chain_id": "xian-remote-1",
                "validators": [],
                "abci_genesis": {},
            }
            remote_genesis_source = "https://example.invalid/genesis.json"
            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "remote-dev",
                        "--chain-id",
                        "xian-remote-1",
                        "--genesis-source",
                        remote_genesis_source,
                        "--output",
                        str(base_dir / "networks" / "remote-dev" / "manifest.json"),
                    ]
                )
                main(
                    [
                        "keys",
                        "validator",
                        "generate",
                        "--out-dir",
                        str(base_dir / "keys" / "validator-1"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "remote-dev",
                        "--validator-key-ref",
                        str(base_dir / "keys" / "validator-1" / "validator_key_info.json"),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch(
                "xian_cli.commands.node_context.fetch_json",
                return_value=genesis_payload,
            ):
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "node",
                            "init",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            home = Path(result["home"])
            rendered_genesis = json.loads(
                (home / "config" / "genesis.json").read_text(encoding="utf-8")
            )
            self.assertEqual(rendered_genesis["chain_id"], "xian-remote-1")
            self.assertEqual(
                home,
                (base_dir / "xian-stack" / ".cometbft").resolve(),
            )

    def test_node_init_prefers_profile_genesis_source_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            (base_dir / "keys" / "validator-1").mkdir(parents=True)
            (base_dir / "xian-stack").mkdir()

            local_genesis_path = base_dir / "genesis-local.json"
            local_genesis_path.write_text(
                json.dumps(
                    {
                        "chain_id": "xian-wrong-1",
                        "validators": [],
                        "abci_genesis": {},
                    }
                ),
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "override-dev",
                        "--chain-id",
                        "xian-override-1",
                        "--genesis-source",
                        str(local_genesis_path),
                        "--output",
                        str(base_dir / "networks" / "override-dev" / "manifest.json"),
                    ]
                )
                main(
                    [
                        "keys",
                        "validator",
                        "generate",
                        "--out-dir",
                        str(base_dir / "keys" / "validator-1"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "override-dev",
                        "--validator-key-ref",
                        str(base_dir / "keys" / "validator-1" / "validator_key_info.json"),
                        "--genesis-source",
                        "https://example.invalid/override-genesis.json",
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch(
                "xian_cli.commands.node_context.fetch_json",
                return_value={
                    "chain_id": "xian-override-1",
                    "validators": [],
                    "abci_genesis": {},
                },
            ):
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "node",
                            "init",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            home = Path(result["home"])
            rendered_genesis = json.loads(
                (home / "config" / "genesis.json").read_text(encoding="utf-8")
            )
            self.assertEqual(rendered_genesis["chain_id"], "xian-override-1")

    def test_node_init_accepts_raw_priv_validator_key_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            key_dir = base_dir / "keys" / "validator-1"
            key_dir.mkdir(parents=True)

            genesis_source = TEST_FIXTURE_GENESIS

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-testnet-12",
                        "--genesis-source",
                        str(genesis_source),
                        "--output",
                        str(base_dir / "networks" / "local-dev" / "manifest.json"),
                    ]
                )
                main(
                    [
                        "keys",
                        "validator",
                        "generate",
                        "--out-dir",
                        str(key_dir),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--validator-key-ref",
                        str(key_dir / "priv_validator_key.json"),
                        "--home",
                        str(base_dir / ".cometbft"),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "node",
                        "init",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                    ]
                )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            home = Path(result["home"])
            self.assertTrue((home / "config" / "priv_validator_key.json").exists())

    def test_node_init_rejects_chain_id_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            (base_dir / "keys" / "validator-1").mkdir(parents=True)

            genesis_source = TEST_FIXTURE_GENESIS

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "bad-chain",
                        "--chain-id",
                        "xian-does-not-match",
                        "--genesis-source",
                        str(genesis_source),
                        "--output",
                        str(base_dir / "networks" / "bad-chain" / "manifest.json"),
                    ]
                )
                main(
                    [
                        "keys",
                        "validator",
                        "generate",
                        "--out-dir",
                        str(base_dir / "keys" / "validator-1"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "bad-chain",
                        "--validator-key-ref",
                        str(base_dir / "keys" / "validator-1" / "validator_key_info.json"),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            with self.assertRaisesRegex(ValueError, "does not match manifest"):
                main(
                    [
                        "node",
                        "init",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                    ]
                )

    def test_node_init_uses_xian_configs_when_local_manifest_is_missing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "nodes").mkdir()
            key_dir = base_dir / "keys" / "validator-1"
            key_dir.mkdir(parents=True)
            stack_dir = base_dir / "xian-stack"
            stack_dir.mkdir()

            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)

            genesis_payload = {
                "chain_id": "xian-canonical-1",
                "validators": [],
                "abci_genesis": {},
            }
            (network_dir / "genesis.json").write_text(
                json.dumps(genesis_payload),
                encoding="utf-8",
            )
            (network_dir / "manifest.json").write_text(
                json.dumps(
                    _manifest_payload(
                        p2p_seeds=["seed-1@127.0.0.1:26656"],
                    )
                ),
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "keys",
                        "validator",
                        "generate",
                        "--out-dir",
                        str(key_dir),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "canonical",
                        "--validator-key-ref",
                        str(key_dir / "validator_key_info.json"),
                        "--stack-dir",
                        str(stack_dir),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch.dict("os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}):
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "node",
                            "init",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            home = Path(result["home"])
            rendered_genesis = json.loads(
                (home / "config" / "genesis.json").read_text(encoding="utf-8")
            )
            rendered_config = (home / "config" / "config.toml").read_text(encoding="utf-8")

            self.assertEqual(rendered_genesis["chain_id"], "xian-canonical-1")
            self.assertIn("seed-1@127.0.0.1:26656", rendered_config)

    def test_node_init_can_restore_snapshot_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            key_dir = base_dir / "keys" / "validator-1"
            key_dir.mkdir(parents=True)
            home = base_dir / ".cometbft"

            genesis_source = TEST_FIXTURE_GENESIS

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-testnet-12",
                        "--genesis-source",
                        str(genesis_source),
                        "--snapshot-url",
                        "https://example.invalid/snapshot-manifest.json",
                        "--snapshot-signing-key",
                        "a" * 64,
                        "--output",
                        str(base_dir / "networks" / "local-dev" / "manifest.json"),
                    ]
                )
                main(
                    [
                        "keys",
                        "validator",
                        "generate",
                        "--out-dir",
                        str(key_dir),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--validator-key-ref",
                        str(key_dir / "validator_key_info.json"),
                        "--home",
                        str(home),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            snapshot_mock = unittest.mock.Mock(return_value="snapshot.tar.gz")
            node_admin = type(
                "NodeAdmin",
                (),
                {"apply_snapshot_archive": staticmethod(snapshot_mock)},
            )()
            stdout = io.StringIO()
            with patch(
                "xian_cli.commands.node_context.get_node_admin_module",
                return_value=node_admin,
            ):
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "node",
                            "init",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                            "--restore-snapshot",
                        ]
                    )

            self.assertEqual(exit_code, 0)
            snapshot_mock.assert_called_once_with(
                "https://example.invalid/snapshot-manifest.json",
                home,
                trusted_manifest_public_keys=["a" * 64],
                expected_chain_id="xian-testnet-12",
            )
            result = json.loads(stdout.getvalue())
            self.assertTrue(result["snapshot_restored"])
            self.assertEqual(
                result["effective_snapshot_url"],
                "https://example.invalid/snapshot-manifest.json",
            )
            self.assertEqual(
                result["snapshot"]["snapshot_archive_name"],
                "snapshot.tar.gz",
            )


class NodeRuntimeTests(unittest.TestCase):
    def test_node_start_uses_xian_stack_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            stack_dir = base_dir / "xian-stack"
            stack_dir.mkdir()
            config_dir = stack_dir / ".cometbft" / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text("", encoding="utf-8")
            (config_dir / "xian.toml").write_text("", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--genesis-source",
                        str(TEST_FIXTURE_GENESIS),
                        "--output",
                        str(base_dir / "networks" / "local-dev" / "manifest.json"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--stack-dir",
                        str(stack_dir),
                        "--enable-dashboard",
                        "--enable-monitoring",
                        "--dashboard-host",
                        "0.0.0.0",
                        "--dashboard-port",
                        "18080",
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch("xian_cli.commands.node.start_xian_stack_node") as start_node:
                start_node.return_value = {
                    "stack_dir": str(stack_dir),
                    "container_target": "abci-up",
                    "node_target": "node-start",
                    "rpc_checked": False,
                }
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "node",
                            "start",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                            "--skip-health-check",
                        ]
                    )

            self.assertEqual(exit_code, 0)
            self.assertEqual(start_node.call_count, 1)
            kwargs = start_node.call_args.kwargs
            self.assertEqual(kwargs["stack_dir"], stack_dir.resolve())
            self.assertEqual(
                kwargs["cometbft_home"],
                (stack_dir / ".cometbft").resolve(),
            )
            self.assertFalse(kwargs["bds_enabled"])
            self.assertTrue(kwargs["dashboard_enabled"])
            self.assertTrue(kwargs["monitoring_enabled"])
            self.assertEqual(kwargs["dashboard_host"], "0.0.0.0")
            self.assertEqual(kwargs["dashboard_port"], 18080)
            self.assertFalse(kwargs["wait_for_rpc"])
            self.assertEqual(kwargs["node_image_mode"], "local_build")
            self.assertIsNone(kwargs["node_integrated_image"])
            self.assertIsNone(kwargs["node_split_image"])

    def test_node_start_inherits_registry_images_from_network(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks" / "mainnet").mkdir(parents=True)
            (base_dir / "nodes").mkdir()
            stack_dir = base_dir / "xian-stack"
            stack_dir.mkdir()
            config_dir = stack_dir / ".cometbft" / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text("", encoding="utf-8")
            (config_dir / "xian.toml").write_text("", encoding="utf-8")

            (base_dir / "networks" / "mainnet" / "manifest.json").write_text(
                json.dumps(
                    _manifest_payload(
                        name="mainnet",
                        chain_id="xian-1",
                        node_image_mode="registry",
                        node_integrated_image=(CANONICAL_RELEASE_INTEGRATED_IMAGE),
                        node_split_image=CANONICAL_RELEASE_SPLIT_IMAGE,
                        block_policy_mode="on_demand",
                        block_policy_interval="0s",
                    )
                ),
                encoding="utf-8",
            )
            (base_dir / "nodes" / "validator-1.json").write_text(
                json.dumps(
                    _profile_payload(
                        network="mainnet",
                        stack_dir=str(stack_dir),
                        home=None,
                        pruning_enabled=False,
                        blocks_to_keep=100000,
                        block_policy_mode="on_demand",
                        block_policy_interval="0s",
                        parallel_execution_workers=4,
                    )
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with patch("xian_cli.commands.node.start_xian_stack_node") as start_node:
                start_node.return_value = {
                    "stack_dir": str(stack_dir),
                    "container_target": "abci-up",
                    "node_target": "node-start",
                    "rpc_checked": False,
                }
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "node",
                            "start",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                            "--skip-health-check",
                        ]
                    )

            self.assertEqual(exit_code, 0)
            kwargs = start_node.call_args.kwargs
            self.assertEqual(kwargs["node_image_mode"], "registry")
            self.assertEqual(
                kwargs["node_integrated_image"],
                CANONICAL_RELEASE_INTEGRATED_IMAGE,
            )
            self.assertEqual(
                kwargs["node_split_image"],
                CANONICAL_RELEASE_SPLIT_IMAGE,
            )

    def test_node_status_surfaces_release_manifest_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks" / "mainnet").mkdir(parents=True)
            (base_dir / "nodes").mkdir()
            stack_dir = base_dir / "xian-stack"
            stack_dir.mkdir()

            (base_dir / "networks" / "mainnet" / "manifest.json").write_text(
                json.dumps(
                    _manifest_payload(
                        name="mainnet",
                        chain_id="xian-1",
                        node_image_mode="registry",
                        node_integrated_image=(CANONICAL_RELEASE_INTEGRATED_IMAGE),
                        node_split_image=CANONICAL_RELEASE_SPLIT_IMAGE,
                        node_release_manifest=(CANONICAL_NODE_RELEASE_MANIFEST),
                        block_policy_mode="on_demand",
                        block_policy_interval="0s",
                    )
                ),
                encoding="utf-8",
            )
            (base_dir / "nodes" / "validator-1.json").write_text(
                json.dumps(
                    _profile_payload(
                        network="mainnet",
                        node_image_mode="registry",
                        node_integrated_image=(CANONICAL_RELEASE_INTEGRATED_IMAGE),
                        node_split_image=CANONICAL_RELEASE_SPLIT_IMAGE,
                        node_release_manifest=(CANONICAL_NODE_RELEASE_MANIFEST),
                        stack_dir=str(stack_dir),
                        home=None,
                        pruning_enabled=False,
                        blocks_to_keep=100000,
                        block_policy_mode="on_demand",
                        block_policy_interval="0s",
                        parallel_execution_workers=4,
                    )
                ),
                encoding="utf-8",
            )

            backend_status = {
                "backend_running": True,
                "compose_services": [
                    {
                        "service": "abci",
                        "state": "running",
                        "image": CANONICAL_RELEASE_INTEGRATED_IMAGE,
                    }
                ],
            }
            rpc_status = {
                "result": {
                    "sync_info": {
                        "latest_block_height": "123",
                        "catching_up": False,
                    },
                    "node_info": {
                        "network": "xian-1",
                        "other": {"n_peers": "4"},
                    },
                }
            }

            stdout = io.StringIO()
            with patch(
                "xian_cli.commands.node.get_xian_stack_node_status",
                return_value=backend_status,
            ):
                with patch(
                    "xian_cli.commands.node.fetch_json",
                    return_value=rpc_status,
                ):
                    with redirect_stdout(stdout):
                        exit_code = main(
                            [
                                "node",
                                "status",
                                "validator-1",
                                "--base-dir",
                                str(base_dir),
                            ]
                        )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(
                payload["node_release_manifest"],
                _normalized_release_manifest(CANONICAL_NODE_RELEASE_MANIFEST),
            )
            self.assertEqual(
                payload["summary"]["release_manifest_refs"]["xian-abci"],
                CANONICAL_NODE_RELEASE_MANIFEST["components"]["xian-abci"]["ref"],
            )
            self.assertEqual(
                payload["summary"]["runtime_service_images"]["abci"],
                CANONICAL_RELEASE_INTEGRATED_IMAGE,
            )
            self.assertEqual(
                payload["summary"]["node_integrated_image"],
                CANONICAL_RELEASE_INTEGRATED_IMAGE,
            )
            self.assertEqual(
                payload["summary"]["node_split_image"],
                CANONICAL_RELEASE_SPLIT_IMAGE,
            )

    def test_node_stop_uses_xian_stack_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            stack_dir = base_dir / "xian-stack"
            stack_dir.mkdir()

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--genesis-source",
                        str(TEST_FIXTURE_GENESIS),
                        "--output",
                        str(base_dir / "networks" / "local-dev" / "manifest.json"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--stack-dir",
                        str(stack_dir),
                        "--enable-bds",
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch("xian_cli.commands.node.stop_xian_stack_node") as stop_node:
                stop_node.return_value = {
                    "stack_dir": str(stack_dir),
                    "container_target": "abci-bds-down",
                }
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "node",
                            "stop",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stop_node.call_count, 1)
            kwargs = stop_node.call_args.kwargs
            self.assertEqual(kwargs["stack_dir"], stack_dir.resolve())
            self.assertEqual(
                kwargs["cometbft_home"],
                default_home_for_backend(
                    base_dir=base_dir,
                    stack_dir=stack_dir.resolve(),
                ),
            )
            self.assertTrue(kwargs["bds_enabled"])
            self.assertFalse(kwargs["monitoring_enabled"])

    def test_node_start_requires_initialized_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            stack_dir = base_dir / "xian-stack"
            stack_dir.mkdir()

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--output",
                        str(base_dir / "networks" / "local-dev" / "manifest.json"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--template",
                        "single-node-indexed",
                        "--stack-dir",
                        str(stack_dir),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            with self.assertRaisesRegex(
                FileNotFoundError,
                "run `xian node init",
            ):
                main(
                    [
                        "node",
                        "start",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                    ]
                )

    def test_node_status_reports_initialized_home_and_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            stack_dir = base_dir / "xian-stack"
            stack_dir.mkdir()
            home = stack_dir / ".cometbft"
            config_dir = home / "config"
            data_dir = home / "data"
            config_dir.mkdir(parents=True)
            data_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text("", encoding="utf-8")
            (config_dir / "xian.toml").write_text("", encoding="utf-8")
            (config_dir / "genesis.json").write_text("{}", encoding="utf-8")
            (config_dir / "node_key.json").write_text(
                json.dumps({"node_id": "node-123"}),
                encoding="utf-8",
            )
            (data_dir / "priv_validator_state.json").write_text(
                "{}",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--output",
                        str(base_dir / "networks" / "local-dev" / "manifest.json"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--template",
                        "single-node-indexed",
                        "--stack-dir",
                        str(stack_dir),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch(
                "xian_cli.commands.node.fetch_json",
                return_value={
                    "result": {
                        "node_info": {
                            "network": "xian",
                            "other": {"n_peers": "2"},
                        },
                        "sync_info": {
                            "latest_block_height": "12",
                            "latest_block_time": "2026-04-18T22:00:00Z",
                            "catching_up": False,
                        },
                    }
                },
            ):
                with patch(
                    "xian_cli.commands.node.get_xian_stack_node_status",
                    return_value={
                        "backend_running": True,
                        "node_id": "node-123",
                        "dashboard_reachable": True,
                        "prometheus_reachable": True,
                        "grafana_reachable": True,
                        "endpoints": {
                            "rpc": "http://127.0.0.1:26657",
                            "dashboard": "http://127.0.0.1:8080",
                            "prometheus": "http://127.0.0.1:9090",
                        },
                    },
                ):
                    with redirect_stdout(stdout):
                        exit_code = main(
                            [
                                "node",
                                "status",
                                "validator-1",
                                "--base-dir",
                                str(base_dir),
                            ]
                        )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            self.assertTrue(result["initialized"])
            self.assertTrue(result["rpc_reachable"])
            self.assertEqual(result["node_id"], "node-123")
            self.assertEqual(result["stack_dir"], str(stack_dir.resolve()))
            self.assertTrue(result["backend_checked"])
            self.assertTrue(result["backend_running"])
            self.assertEqual(result["summary"]["state"], "ready")
            self.assertEqual(result["summary"]["rpc_network"], "xian")
            self.assertEqual(result["summary"]["rpc_height"], "12")
            self.assertIsInstance(result["summary"]["rpc_block_age_seconds"], float)
            self.assertGreaterEqual(result["summary"]["rpc_block_age_seconds"], 0.0)
            self.assertEqual(result["summary"]["peer_count"], "2")
            self.assertTrue(result["summary"]["dashboard_reachable"])
            self.assertTrue(result["summary"]["prometheus_reachable"])
            self.assertEqual(
                result["endpoints"]["dashboard"],
                "http://127.0.0.1:8080",
            )

    def test_node_endpoints_reports_effective_operator_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            stack_dir = base_dir / "xian-stack"
            stack_dir.mkdir()

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--genesis-source",
                        str(TEST_FIXTURE_GENESIS),
                        "--output",
                        str(base_dir / "networks" / "local-dev" / "manifest.json"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--template",
                        "single-node-indexed",
                        "--stack-dir",
                        str(stack_dir),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch(
                "xian_cli.commands.node.get_xian_stack_node_endpoints",
                return_value={
                    "endpoints": {
                        "rpc": "http://127.0.0.1:26657",
                        "rpc_status": "http://127.0.0.1:26657/status",
                        "xian_metrics": "http://127.0.0.1:9108/metrics",
                        "prometheus": "http://127.0.0.1:9090",
                        "grafana": "http://127.0.0.1:3000",
                    }
                },
            ):
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "node",
                            "endpoints",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            self.assertTrue(result["dashboard_enabled"])
            self.assertTrue(result["monitoring_enabled"])
            self.assertEqual(
                result["endpoints"]["xian_metrics"],
                "http://127.0.0.1:9108/metrics",
            )
            self.assertEqual(
                result["endpoints"]["prometheus"],
                "http://127.0.0.1:9090",
            )

    def test_node_health_reports_runtime_and_statesync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            stack_dir = base_dir / "xian-stack"
            stack_dir.mkdir()
            home = stack_dir / ".cometbft"
            config_dir = home / "config"
            data_dir = home / "data"
            config_dir.mkdir(parents=True)
            data_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text(
                f"""
[statesync]
enable = true
rpc_servers = "http://rpc-1.example:26657,http://rpc-2.example:26657"
trust_height = 120
trust_hash = "{"ab" * 32}"
trust_period = "336h0m0s"
""",
                encoding="utf-8",
            )
            (config_dir / "xian.toml").write_text("", encoding="utf-8")
            (config_dir / "genesis.json").write_text("{}", encoding="utf-8")
            (config_dir / "node_key.json").write_text(
                json.dumps({"node_id": "node-123"}),
                encoding="utf-8",
            )
            (data_dir / "priv_validator_state.json").write_text(
                "{}",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--snapshot-url",
                        "https://example.invalid/snapshot.tar.gz",
                        "--output",
                        str(base_dir / "networks" / "local-dev" / "manifest.json"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--stack-dir",
                        str(stack_dir),
                        "--home",
                        str(home),
                        "--enable-dashboard",
                        "--enable-monitoring",
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch(
                "xian_cli.commands.node.get_xian_stack_node_health",
                return_value={
                    "state": "healthy",
                    "checks": {"backend": {"ok": True}},
                    "endpoints": {
                        "rpc": "http://127.0.0.1:26657",
                        "xian_metrics": "http://127.0.0.1:9108/metrics",
                    },
                },
            ):
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "node",
                            "health",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            self.assertIsNone(result["operator_profile"])
            self.assertIsNone(result["monitoring_profile"])
            self.assertEqual(result["health"]["state"], "healthy")
            self.assertEqual(result["statesync"]["state"], "configured")
            self.assertTrue(result["statesync"]["ready"])
            self.assertEqual(
                result["effective_snapshot_url"],
                "https://example.invalid/snapshot.tar.gz",
            )
            self.assertEqual(
                result["endpoints"]["xian_metrics"],
                "http://127.0.0.1:9108/metrics",
            )


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_workspace_and_node_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            stack_dir = base_dir / "xian-stack"
            configs_dir.mkdir()
            stack_dir.mkdir()
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            home = base_dir / ".cometbft"
            config_dir = home / "config"
            data_dir = home / "data"
            config_dir.mkdir(parents=True)
            data_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text("", encoding="utf-8")
            (config_dir / "xian.toml").write_text("", encoding="utf-8")
            (config_dir / "genesis.json").write_text("{}", encoding="utf-8")
            (config_dir / "node_key.json").write_text(
                json.dumps({"node_id": "node-123"}),
                encoding="utf-8",
            )
            (data_dir / "priv_validator_state.json").write_text(
                "{}",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--output",
                        str(base_dir / "networks" / "local-dev" / "manifest.json"),
                    ]
                )
                main(
                    [
                        "keys",
                        "validator",
                        "generate",
                        "--out-dir",
                        str(base_dir / "keys" / "validator-1"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--stack-dir",
                        str(stack_dir),
                        "--home",
                        str(home),
                        "--validator-key-ref",
                        str(base_dir / "keys" / "validator-1" / "validator_key_info.json"),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch(
                "xian_cli.commands.doctor.get_node_setup_module",
                return_value=type("NodeSetup", (), {"__name__": "node_setup"})(),
            ):
                with patch(
                    "xian_cli.commands.doctor.get_node_admin_module",
                    return_value=type("NodeAdmin", (), {"__name__": "node_admin"})(),
                ):
                    with patch(
                        "xian_cli.commands.doctor.get_genesis_builder_module",
                        return_value=type(
                            "GenesisBuilder",
                            (),
                            {"__name__": "genesis_builder"},
                        )(),
                    ):
                        with redirect_stdout(stdout):
                            with patch(
                                "xian_cli.commands.node.get_xian_stack_node_status",
                                return_value={
                                    "backend_running": True,
                                    "node_id": "node-123",
                                },
                            ):
                                exit_code = main(
                                    [
                                        "doctor",
                                        "validator-1",
                                        "--base-dir",
                                        str(base_dir),
                                        "--configs-dir",
                                        str(configs_dir),
                                        "--stack-dir",
                                        str(stack_dir),
                                        "--skip-live-checks",
                                    ]
                                )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            self.assertTrue(result["ok"])
            check_names = {check["name"] for check in result["checks"]}
            self.assertIn("configs_dir", check_names)
            self.assertIn("stack_dir", check_names)
            self.assertIn("node_status", check_names)
            self.assertIn("node_artifacts", check_names)
            self.assertIn("endpoints", check_names)

    def test_doctor_can_run_live_health_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            stack_dir = base_dir / "xian-stack"
            configs_dir.mkdir()
            stack_dir.mkdir()
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            home = base_dir / ".cometbft"
            config_dir = home / "config"
            data_dir = home / "data"
            config_dir.mkdir(parents=True)
            data_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text("", encoding="utf-8")
            (config_dir / "xian.toml").write_text("", encoding="utf-8")
            (config_dir / "genesis.json").write_text("{}", encoding="utf-8")
            (config_dir / "node_key.json").write_text(
                json.dumps({"node_id": "node-123"}),
                encoding="utf-8",
            )
            (data_dir / "priv_validator_state.json").write_text(
                "{}",
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--genesis-source",
                        str(TEST_FIXTURE_GENESIS),
                        "--output",
                        str(base_dir / "networks" / "local-dev" / "manifest.json"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--stack-dir",
                        str(stack_dir),
                        "--enable-dashboard",
                        "--enable-monitoring",
                        "--home",
                        str(home),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch(
                "xian_cli.commands.doctor.get_node_setup_module",
                return_value=type("NodeSetup", (), {"__name__": "node_setup"})(),
            ):
                with patch(
                    "xian_cli.commands.doctor.get_node_admin_module",
                    return_value=type("NodeAdmin", (), {"__name__": "node_admin"})(),
                ):
                    with patch(
                        "xian_cli.commands.doctor.get_genesis_builder_module",
                        return_value=type(
                            "GenesisBuilder",
                            (),
                            {"__name__": "genesis_builder"},
                        )(),
                    ):
                        with patch(
                            "xian_cli.commands.node.fetch_json",
                            return_value={
                                "result": {
                                    "node_info": {
                                        "network": "xian",
                                        "other": {"n_peers": "3"},
                                    },
                                    "sync_info": {
                                        "latest_block_height": "42",
                                        "catching_up": False,
                                    },
                                }
                            },
                        ):
                            with patch(
                                "xian_cli.commands.node.get_xian_stack_node_status",
                                return_value={
                                    "backend_running": True,
                                    "node_id": "node-123",
                                    "dashboard_reachable": True,
                                    "prometheus_reachable": True,
                                    "grafana_reachable": True,
                                    "endpoints": {
                                        "rpc": "http://127.0.0.1:26657",
                                        "dashboard": "http://127.0.0.1:8080",
                                        "prometheus": "http://127.0.0.1:9090",
                                        "grafana": "http://127.0.0.1:3000",
                                    },
                                },
                            ):
                                with redirect_stdout(stdout):
                                    exit_code = main(
                                        [
                                            "doctor",
                                            "validator-1",
                                            "--base-dir",
                                            str(base_dir),
                                            "--configs-dir",
                                            str(configs_dir),
                                            "--stack-dir",
                                            str(stack_dir),
                                        ]
                                    )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            self.assertTrue(result["ok"])
            check_names = {check["name"] for check in result["checks"]}
            self.assertIn("backend", check_names)
            self.assertIn("rpc", check_names)
            self.assertIn("statesync", check_names)
            self.assertIn("snapshot_bootstrap", check_names)
            self.assertIn("dashboard", check_names)
            self.assertIn("prometheus", check_names)
            self.assertIn("grafana", check_names)

    def test_doctor_returns_nonzero_when_workspace_check_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "doctor",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(base_dir / "missing-configs"),
                        "--stack-dir",
                        str(base_dir / "missing-stack"),
                    ]
                )

            self.assertEqual(exit_code, 1)
            result = json.loads(stdout.getvalue())
            self.assertFalse(result["ok"])
            failed_checks = {check["name"]: check for check in result["checks"] if not check["ok"]}
            self.assertIn("configs_dir", failed_checks)
            self.assertIn("stack_dir", failed_checks)
            self.assertIn(
                "does not exist",
                failed_checks["configs_dir"]["detail"],
            )


class SnapshotCommandTests(unittest.TestCase):
    def test_snapshot_restore_uses_effective_snapshot_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            home = base_dir / ".cometbft"
            config_dir = home / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text("", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--snapshot-url",
                        "https://example.invalid/network-snapshot-manifest.json",
                        "--snapshot-signing-key",
                        "b" * 64,
                        "--output",
                        str(base_dir / "networks" / "local-dev" / "manifest.json"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--home",
                        str(home),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            snapshot_mock = unittest.mock.Mock(return_value="snapshot.tar.gz")
            node_admin = type(
                "NodeAdmin",
                (),
                {"apply_snapshot_archive": staticmethod(snapshot_mock)},
            )()
            stdout = io.StringIO()
            with patch(
                "xian_cli.commands.node_context.get_node_admin_module",
                return_value=node_admin,
            ):
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "snapshot",
                            "restore",
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            snapshot_mock.assert_called_once_with(
                "https://example.invalid/network-snapshot-manifest.json",
                home,
                trusted_manifest_public_keys=["b" * 64],
                expected_chain_id="xian-local-1",
            )
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["home"], str(home))
            self.assertEqual(
                result["snapshot_url"],
                "https://example.invalid/network-snapshot-manifest.json",
            )
            self.assertEqual(
                result["snapshot_archive_name"],
                "snapshot.tar.gz",
            )

    def test_snapshot_restore_requires_initialized_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            home = base_dir / ".cometbft"

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--snapshot-url",
                        "https://example.invalid/network-snapshot.tar.gz",
                        "--output",
                        str(base_dir / "networks" / "local-dev" / "manifest.json"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--home",
                        str(home),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            with self.assertRaisesRegex(FileNotFoundError, "run `xian node init"):
                main(
                    [
                        "snapshot",
                        "restore",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                    ]
                )

    def test_recovery_validate_reports_plan_and_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            home = base_dir / ".cometbft"
            config_dir = home / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text("", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--output",
                        str(base_dir / "networks" / "local-dev" / "manifest.json"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--home",
                        str(home),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            plan_path = base_dir / "recovery-plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "metering-incident-20260327",
                        "chain_id": "xian-local-1",
                        "target_height": 120,
                        "trusted_block_hash": "ABCD1234",
                        "trusted_app_hash": "DCBA4321",
                        "reason": "Recover pre-divergence state",
                        "artifact": {
                            "kind": "snapshot_url",
                            "uri": "https://example.invalid/recovery.tar.gz",
                            "sha256": "ab" * 32,
                        },
                    }
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "recovery",
                        "validate",
                        str(plan_path),
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--rpc-url",
                        "http://127.0.0.1:1/status",
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["plan"]["name"], "metering-incident-20260327")
            self.assertEqual(payload["node"]["home"], str(home))
            self.assertTrue(payload["validation"]["requires_manual_hash_confirmation"])

    def test_recovery_apply_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            home = base_dir / ".cometbft"
            config_dir = home / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text("", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--output",
                        str(base_dir / "networks" / "local-dev" / "manifest.json"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--home",
                        str(home),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            plan_path = base_dir / "recovery-plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "metering-incident-20260327",
                        "chain_id": "xian-local-1",
                        "target_height": 120,
                        "trusted_block_hash": "ABCD1234",
                        "trusted_app_hash": "DCBA4321",
                        "reason": "Recover pre-divergence state",
                        "artifact": {
                            "kind": "snapshot_url",
                            "uri": "https://example.invalid/recovery.tar.gz",
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "pass --yes"):
                with redirect_stdout(io.StringIO()):
                    main(
                        [
                            "recovery",
                            "apply",
                            str(plan_path),
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                        ]
                    )

    def test_recovery_apply_stops_backs_up_and_restores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            stack_dir = base_dir / "xian-stack"
            stack_dir.mkdir()
            home = base_dir / ".cometbft"
            config_dir = home / "config"
            config_dir.mkdir(parents=True)
            (config_dir / "config.toml").write_text("", encoding="utf-8")

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--output",
                        str(base_dir / "networks" / "local-dev" / "manifest.json"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                        "--network",
                        "local-dev",
                        "--stack-dir",
                        str(stack_dir),
                        "--home",
                        str(home),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            plan_path = base_dir / "recovery-plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "metering-incident-20260327",
                        "chain_id": "xian-local-1",
                        "target_height": 120,
                        "trusted_block_hash": "ABCD1234",
                        "trusted_app_hash": "DCBA4321",
                        "reason": "Recover pre-divergence state",
                        "artifact": {
                            "kind": "snapshot_url",
                            "uri": "https://example.invalid/recovery.tar.gz",
                            "sha256": "ab" * 32,
                        },
                        "follow_up_state_patch": {
                            "patch_id": "metering-fix-20260327",
                            "bundle_hash": "ff" * 32,
                            "activation_height": 140,
                        },
                    }
                ),
                encoding="utf-8",
            )

            stop_mock = unittest.mock.Mock(return_value={"status": "stopped"})
            backup_mock = unittest.mock.Mock(
                return_value=str(base_dir / "recovery-backups" / "backup.tar.gz")
            )
            snapshot_mock = unittest.mock.Mock(return_value="snapshot.tar.gz")
            node_admin = type(
                "NodeAdmin",
                (),
                {"apply_snapshot_archive": staticmethod(snapshot_mock)},
            )()
            stdout = io.StringIO()
            with (
                patch(
                    "xian_cli.commands.recovery.stop_xian_stack_node",
                    stop_mock,
                ),
                patch(
                    "xian_cli.commands.recovery.shutil.make_archive",
                    backup_mock,
                ),
                patch(
                    "xian_cli.commands.recovery.get_node_admin_module",
                    return_value=node_admin,
                ),
            ):
                with redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "recovery",
                            "apply",
                            str(plan_path),
                            "validator-1",
                            "--base-dir",
                            str(base_dir),
                            "--rpc-url",
                            "http://127.0.0.1:1/status",
                            "--yes",
                        ]
                    )

            self.assertEqual(exit_code, 0)
            stop_mock.assert_called_once()
            backup_mock.assert_called_once()
            snapshot_mock.assert_called_once_with(
                "https://example.invalid/recovery.tar.gz",
                home,
                expected_sha256="ab" * 32,
            )
            payload = json.loads(stdout.getvalue())
            self.assertFalse(payload["dry_run"])
            self.assertTrue(payload["stopped_node"])
            self.assertEqual(
                payload["backup_archive"],
                str(base_dir / "recovery-backups" / "backup.tar.gz"),
            )
            self.assertEqual(
                payload["snapshot_restore"]["snapshot_archive_name"],
                "snapshot.tar.gz",
            )


class RuntimeHelperTests(unittest.TestCase):
    def test_resolve_stack_dir_uses_workspace_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            resolved = resolve_stack_dir(Path(tmp_dir))

        expected = (Path(__file__).resolve().parents[2] / "xian-stack").resolve()
        self.assertEqual(resolved, expected)

    def test_resolve_stack_dir_rejects_missing_explicit_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing = Path(tmp_dir) / "missing-stack"

            with self.assertRaisesRegex(
                FileNotFoundError,
                "xian-stack directory does not exist",
            ):
                resolve_stack_dir(Path(tmp_dir), explicit=missing)

    def test_wait_for_rpc_ready_retries_until_result_is_available(self) -> None:
        expected_payload = {"result": {"node_info": {"network": "xian"}}}

        with patch(
            "xian_cli.runtime.fetch_json",
            side_effect=[URLError("down"), expected_payload],
        ) as fetch_json_mock:
            with patch("xian_cli.runtime.time.sleep") as sleep_mock:
                payload = wait_for_rpc_ready(
                    timeout_seconds=1.0,
                    poll_interval=0.01,
                )

        self.assertEqual(payload, expected_payload)
        self.assertEqual(fetch_json_mock.call_count, 2)
        self.assertEqual(sleep_mock.call_count, 1)

    def test_start_xian_stack_node_uses_backend_command(self) -> None:
        stack_dir = Path("/tmp/xian-stack")
        cometbft_home = Path("/tmp/xian-home")
        rpc_status = {"result": {"sync_info": {"catching_up": False}}}

        with patch("xian_cli.runtime.run_backend_command") as run_backend_command:
            run_backend_command.return_value = {"rpc_status": rpc_status}
            result = start_xian_stack_node(
                stack_dir=stack_dir,
                cometbft_home=cometbft_home,
                bds_enabled=True,
                dashboard_enabled=True,
                monitoring_enabled=True,
                dashboard_host="0.0.0.0",
                dashboard_port=18080,
                wait_for_rpc=True,
                rpc_timeout_seconds=12.5,
            )

        run_backend_command.assert_called_once_with(
            stack_dir,
            "start",
            cometbft_home=cometbft_home,
            node_image_mode="local_build",
            node_integrated_image=None,
            node_split_image=None,
            bds_enabled=True,
            dashboard_enabled=True,
            monitoring_enabled=True,
            dashboard_host="0.0.0.0",
            dashboard_port=18080,
            intentkit_enabled=False,
            intentkit_network_id=None,
            intentkit_host="127.0.0.1",
            intentkit_port=38000,
            intentkit_api_port=38080,
            dex_automation_enabled=False,
            dex_automation_host="127.0.0.1",
            dex_automation_port=38280,
            dex_automation_config=None,
            shielded_relayer_enabled=False,
            shielded_relayer_host="127.0.0.1",
            shielded_relayer_port=38180,
            stream_stderr=True,
            wait_for_health=True,
            rpc_timeout_seconds=12.5,
            rpc_url="http://127.0.0.1:26657/status",
        )
        self.assertEqual(result["rpc_status"], rpc_status)

    def test_start_xian_stack_node_passes_registry_image_config(self) -> None:
        stack_dir = Path("/tmp/xian-stack")

        with patch("xian_cli.runtime.run_backend_command") as run_backend_command:
            run_backend_command.return_value = {"backend_running": True}
            start_xian_stack_node(
                stack_dir=stack_dir,
                node_image_mode="registry",
                node_integrated_image="ghcr.io/xian-technology/xian-node@sha256:abc",
                node_split_image="ghcr.io/xian-technology/xian-node-split@sha256:def",
                bds_enabled=False,
                wait_for_rpc=False,
            )

        run_backend_command.assert_called_once_with(
            stack_dir,
            "start",
            cometbft_home=None,
            node_image_mode="registry",
            node_integrated_image="ghcr.io/xian-technology/xian-node@sha256:abc",
            node_split_image="ghcr.io/xian-technology/xian-node-split@sha256:def",
            bds_enabled=False,
            dashboard_enabled=False,
            monitoring_enabled=False,
            dashboard_host="127.0.0.1",
            dashboard_port=8080,
            intentkit_enabled=False,
            intentkit_network_id=None,
            intentkit_host="127.0.0.1",
            intentkit_port=38000,
            intentkit_api_port=38080,
            dex_automation_enabled=False,
            dex_automation_host="127.0.0.1",
            dex_automation_port=38280,
            dex_automation_config=None,
            shielded_relayer_enabled=False,
            shielded_relayer_host="127.0.0.1",
            shielded_relayer_port=38180,
            stream_stderr=True,
            wait_for_health=False,
            rpc_timeout_seconds=90.0,
            rpc_url="http://127.0.0.1:26657/status",
        )

    def test_stop_xian_stack_node_uses_backend_command(self) -> None:
        stack_dir = Path("/tmp/xian-stack")
        cometbft_home = Path("/tmp/xian-home")

        with patch("xian_cli.runtime.run_backend_command") as run_backend_command:
            run_backend_command.return_value = {"container_target": "abci-down"}
            result = stop_xian_stack_node(
                stack_dir=stack_dir,
                cometbft_home=cometbft_home,
                bds_enabled=False,
                dashboard_enabled=True,
                monitoring_enabled=True,
                dashboard_host="0.0.0.0",
                dashboard_port=18080,
            )

        run_backend_command.assert_called_once_with(
            stack_dir,
            "stop",
            cometbft_home=cometbft_home,
            node_image_mode="local_build",
            node_integrated_image=None,
            node_split_image=None,
            bds_enabled=False,
            dashboard_enabled=True,
            monitoring_enabled=True,
            dashboard_host="0.0.0.0",
            dashboard_port=18080,
            intentkit_enabled=False,
            intentkit_network_id=None,
            intentkit_host="127.0.0.1",
            intentkit_port=38000,
            intentkit_api_port=38080,
            dex_automation_enabled=False,
            dex_automation_host="127.0.0.1",
            dex_automation_port=38280,
            dex_automation_config=None,
            shielded_relayer_enabled=False,
            shielded_relayer_host="127.0.0.1",
            shielded_relayer_port=38180,
        )
        self.assertEqual(result["container_target"], "abci-down")

    def test_get_xian_stack_node_status_uses_backend_command(self) -> None:
        stack_dir = Path("/tmp/xian-stack")
        cometbft_home = Path("/tmp/xian-home")
        with patch("xian_cli.runtime.run_backend_command") as run_backend_command:
            run_backend_command.return_value = {
                "backend_running": True,
                "node_id": "abc123",
            }
            result = get_xian_stack_node_status(
                stack_dir=stack_dir,
                cometbft_home=cometbft_home,
                bds_enabled=True,
                dashboard_enabled=True,
                monitoring_enabled=True,
                dashboard_host="0.0.0.0",
                dashboard_port=18080,
            )

        self.assertTrue(result["backend_running"])
        self.assertEqual(result["node_id"], "abc123")
        run_backend_command.assert_called_once_with(
            stack_dir,
            "status",
            cometbft_home=cometbft_home,
            node_image_mode="local_build",
            node_integrated_image=None,
            node_split_image=None,
            bds_enabled=True,
            dashboard_enabled=True,
            monitoring_enabled=True,
            dashboard_host="0.0.0.0",
            dashboard_port=18080,
            intentkit_enabled=False,
            intentkit_network_id=None,
            intentkit_host="127.0.0.1",
            intentkit_port=38000,
            intentkit_api_port=38080,
            dex_automation_enabled=False,
            dex_automation_host="127.0.0.1",
            dex_automation_port=38280,
            dex_automation_config=None,
            shielded_relayer_enabled=False,
            shielded_relayer_host="127.0.0.1",
            shielded_relayer_port=38180,
        )

    def test_get_xian_stack_node_endpoints_uses_backend_command(self) -> None:
        stack_dir = Path("/tmp/xian-stack")
        cometbft_home = Path("/tmp/xian-home")
        with patch("xian_cli.runtime.run_backend_command") as run_backend_command:
            run_backend_command.return_value = {"endpoints": {"rpc": "http://127.0.0.1:26657"}}
            result = get_xian_stack_node_endpoints(
                stack_dir=stack_dir,
                cometbft_home=cometbft_home,
                bds_enabled=True,
                dashboard_enabled=True,
                monitoring_enabled=True,
                dashboard_host="0.0.0.0",
                dashboard_port=18080,
            )

        self.assertEqual(
            result["endpoints"]["rpc"],
            "http://127.0.0.1:26657",
        )
        run_backend_command.assert_called_once_with(
            stack_dir,
            "endpoints",
            cometbft_home=cometbft_home,
            node_image_mode="local_build",
            node_integrated_image=None,
            node_split_image=None,
            bds_enabled=True,
            dashboard_enabled=True,
            monitoring_enabled=True,
            dashboard_host="0.0.0.0",
            dashboard_port=18080,
            intentkit_enabled=False,
            intentkit_network_id=None,
            intentkit_host="127.0.0.1",
            intentkit_port=38000,
            intentkit_api_port=38080,
            dex_automation_enabled=False,
            dex_automation_host="127.0.0.1",
            dex_automation_port=38280,
            dex_automation_config=None,
            shielded_relayer_enabled=False,
            shielded_relayer_host="127.0.0.1",
            shielded_relayer_port=38180,
        )

    def test_get_xian_stack_node_health_uses_backend_command(self) -> None:
        stack_dir = Path("/tmp/xian-stack")
        cometbft_home = Path("/tmp/xian-home")
        with patch("xian_cli.runtime.run_backend_command") as run_backend_command:
            run_backend_command.return_value = {"state": "healthy"}
            result = get_xian_stack_node_health(
                stack_dir=stack_dir,
                cometbft_home=cometbft_home,
                bds_enabled=True,
                dashboard_enabled=True,
                monitoring_enabled=True,
                dashboard_host="0.0.0.0",
                dashboard_port=18080,
                rpc_url="http://127.0.0.1:26657/status",
                check_disk=False,
            )

        self.assertEqual(result["state"], "healthy")
        run_backend_command.assert_called_once_with(
            stack_dir,
            "health",
            cometbft_home=cometbft_home,
            node_image_mode="local_build",
            node_integrated_image=None,
            node_split_image=None,
            bds_enabled=True,
            dashboard_enabled=True,
            monitoring_enabled=True,
            dashboard_host="0.0.0.0",
            dashboard_port=18080,
            intentkit_enabled=False,
            intentkit_network_id=None,
            intentkit_host="127.0.0.1",
            intentkit_port=38000,
            intentkit_api_port=38080,
            dex_automation_enabled=False,
            dex_automation_host="127.0.0.1",
            dex_automation_port=38280,
            dex_automation_config=None,
            shielded_relayer_enabled=False,
            shielded_relayer_host="127.0.0.1",
            shielded_relayer_port=38180,
            rpc_url="http://127.0.0.1:26657/status",
            check_disk=False,
        )

    def test_fallback_node_endpoints_normalizes_wildcard_dashboard_host(
        self,
    ) -> None:
        profile = {
            "services": _services_payload(
                dashboard_enabled=True,
                dashboard_host="0.0.0.0",
                dashboard_port=18080,
            ),
        }

        endpoints = _fallback_node_endpoints(
            rpc_status_url="http://127.0.0.1:26657/status",
            profile=profile,
        )

        self.assertEqual(endpoints["dashboard"], "http://127.0.0.1:18080")
        self.assertEqual(
            endpoints["dashboard_status"],
            "http://127.0.0.1:18080/api/status",
        )

    def test_run_backend_command_surfaces_backend_stderr(self) -> None:
        stack_dir = Path("/tmp/xian-stack")
        error = subprocess.CalledProcessError(
            1,
            ["python3", "backend.py", "start"],
            output='{"state":"failed"}',
            stderr="compose interpolation failed",
        )

        with patch(
            "xian_cli.runtime.subprocess.run",
            side_effect=error,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "xian-stack backend command failed \\(start\\): compose interpolation failed",
            ):
                run_backend_command(stack_dir, "start")

    def test_run_backend_command_sends_structured_request(self) -> None:
        stack_dir = Path("/tmp/xian-stack")
        completed = subprocess.CompletedProcess(
            ["python3", "backend.py", "--request-json", "-"],
            0,
            stdout='{"ok": true}',
            stderr="",
        )

        with patch(
            "xian_cli.runtime.subprocess.run",
            return_value=completed,
        ) as run_mock:
            result = run_backend_command(
                stack_dir,
                "start",
                dex_automation_enabled=True,
                dex_automation_host="0.0.0.0",
                dex_automation_port=39123,
                dex_automation_config="/tmp/dex.yaml",
                shielded_relayer_enabled=True,
                shielded_relayer_host="0.0.0.0",
                shielded_relayer_port=39180,
            )

        self.assertTrue(result["ok"])
        command = run_mock.call_args.args[0]
        self.assertEqual(command[-2:], ["--request-json", "-"])
        request = json.loads(run_mock.call_args.kwargs["input"])
        self.assertEqual(request["schema_version"], 1)
        self.assertEqual(request["command"], "start")
        self.assertTrue(request["options"]["dex_automation"])
        self.assertEqual(request["options"]["dex_automation_host"], "0.0.0.0")
        self.assertEqual(request["options"]["dex_automation_port"], 39123)
        self.assertEqual(request["options"]["dex_automation_config"], "/tmp/dex.yaml")
        self.assertTrue(request["options"]["shielded_relayer"])
        self.assertEqual(request["options"]["shielded_relayer_host"], "0.0.0.0")
        self.assertEqual(request["options"]["shielded_relayer_port"], 39180)

    def test_run_backend_command_can_stream_backend_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            stack_dir = Path(tmp_dir)
            scripts_dir = stack_dir / "scripts"
            scripts_dir.mkdir()
            backend_script = scripts_dir / "backend.py"
            backend_script.write_text(
                "\n".join(
                    [
                        "import json",
                        "import sys",
                        "sys.stdin.read()",
                        "print('compose progress', file=sys.stderr)",
                        "print(json.dumps({'ok': True}))",
                    ]
                ),
                encoding="utf-8",
            )

            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = run_backend_command(
                    stack_dir,
                    "start",
                    stream_stderr=True,
                )

        self.assertTrue(result["ok"])
        self.assertIn("compose progress", stderr.getvalue())


class ConfigRepoTests(unittest.TestCase):
    def test_read_network_manifest_requires_explicit_schema_version(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            manifest_path = Path(tmp_dir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "name": "mainnet",
                        "chain_id": "xian-1",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "unsupported schema_version"):
                read_network_manifest(manifest_path)

    def test_read_node_profile_requires_explicit_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            profile_path = Path(tmp_dir) / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "name": "validator-1",
                        "network": "mainnet",
                        "moniker": "validator-1",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "unsupported schema_version"):
                read_node_profile(profile_path)

    def test_read_node_profile_rejects_boolean_port_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            profile_path = Path(tmp_dir) / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "validator-1",
                        "network": "mainnet",
                        "moniker": "validator-1",
                        "services": {
                            "dashboard": {
                                "enabled": True,
                                "port": True,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "port must be an integer",
            ):
                read_node_profile(profile_path)

    def test_read_node_profile_populates_declarative_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            profile_path = Path(tmp_dir) / "profile.json"
            profile_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "validator-1",
                        "network": "mainnet",
                        "moniker": "validator-1",
                    }
                ),
                encoding="utf-8",
            )

            profile = read_node_profile(profile_path)

        self.assertEqual(profile["p2p"]["seeds"], [])
        self.assertEqual(profile["p2p"]["persistent_peers"], [])
        self.assertIsNone(profile["genesis"])
        self.assertFalse(profile["services"]["bds"]["enabled"])
        self.assertEqual(profile["services"]["bds"]["queue_max_size"], 128)
        self.assertTrue(profile["services"]["bds"]["catchup_enabled"])
        self.assertFalse(profile["services"]["dashboard"]["enabled"])
        self.assertFalse(profile["services"]["monitoring"]["enabled"])
        self.assertFalse(profile["services"]["intentkit"]["enabled"])
        self.assertFalse(profile["services"]["dex_automation"]["enabled"])
        self.assertFalse(profile["services"]["shielded_relayer"]["enabled"])
        self.assertEqual(profile["tx_fee_mode"], "paid_metered")
        self.assertEqual(profile["free_tx_max_chi"], 1_000_000)
        self.assertEqual(profile["free_block_max_chi"], 20_000_000)
        self.assertEqual(profile["parallel_execution_workers"], 4)
        self.assertEqual(
            profile["advanced"]["metrics"]["host"],
            "127.0.0.1",
        )
        self.assertEqual(
            profile["advanced"]["parallel_execution"]["max_speculative_waves"],
            4,
        )

    def test_read_node_profile_rejects_parallel_enabled_with_zero_workers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            profile_path = Path(tmp_dir) / "profile.json"
            profile_path.write_text(
                json.dumps(
                    _profile_payload(
                        parallel_execution_enabled=True,
                        parallel_execution_workers=0,
                    )
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                (
                    "parallel_execution_workers must be greater than zero "
                    "when parallel_execution_enabled is true"
                ),
            ):
                read_node_profile(profile_path)

    def test_read_node_profile_rejects_invalid_free_fee_caps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            profile_path = Path(tmp_dir) / "profile.json"
            profile_path.write_text(
                json.dumps(
                    _profile_payload(
                        tx_fee_mode="free_metered",
                        free_tx_max_chi=1000,
                        free_block_max_chi=999,
                    )
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "free_block_max_chi must be greater than or equal to free_tx_max_chi",
            ):
                read_node_profile(profile_path)

    def test_read_configs_reject_unknown_schema_fields(self) -> None:
        cases = [
            (
                "manifest.json",
                _manifest_payload(mode="join"),
                read_network_manifest,
                "network manifest has unknown field\\(s\\): mode",
            ),
            (
                "manifest.json",
                _manifest_payload(
                    genesis={
                        "kind": "bundle",
                        "bundle": "devnet",
                        "preset": "devnet",
                    }
                ),
                read_network_manifest,
                "genesis has unknown field\\(s\\): preset",
            ),
            (
                "manifest.json",
                {
                    **_manifest_payload(),
                    "p2p": {
                        "seeds": [],
                        "persistent_peers": [],
                        "seed_nodes": [],
                    },
                },
                read_network_manifest,
                "p2p has unknown field\\(s\\): seed_nodes",
            ),
            (
                "profile.json",
                _profile_payload(service_node=True),
                read_node_profile,
                "node profile has unknown field\\(s\\): service_node",
            ),
            (
                "profile.json",
                _profile_payload(services={"bds": {"enabled": True, "worker_count": 8}}),
                read_node_profile,
                "services.bds has unknown field\\(s\\): worker_count",
            ),
            (
                "profile.json",
                _profile_payload(advanced={"metrics": {"enabled": True, "interval": 5}}),
                read_node_profile,
                "advanced.metrics has unknown field\\(s\\): interval",
            ),
            (
                "template.json",
                {
                    "schema_version": 1,
                    "name": "single-node-dev",
                    "display_name": "Single Node Dev",
                    "description": "Local dev",
                    "service_node": True,
                },
                read_network_template,
                "network template has unknown field\\(s\\): service_node",
            ),
        ]
        for filename, payload, reader, pattern in cases:
            with self.subTest(filename=filename, pattern=pattern):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    path = Path(tmp_dir) / filename
                    path.write_text(json.dumps(payload), encoding="utf-8")

                    with self.assertRaisesRegex(ValueError, pattern):
                        reader(path)

    def test_read_network_template_requires_explicit_schema_version(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            template_path = Path(tmp_dir) / "template.json"
            template_path.write_text(
                json.dumps(
                    {
                        "name": "single-node-dev",
                        "display_name": "Single-Node Dev",
                        "description": "Local dev template",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "unsupported schema_version"):
                read_network_template(template_path)

    def test_contract_pack_list_reads_canonical_packs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            pack_dir = configs_dir / "contract-packs" / "dex"
            pack_dir.mkdir(parents=True)
            (pack_dir / "contract-pack.json").write_text(
                json.dumps(
                    {
                        "schema": "xian.contract_pack.v1",
                        "schema_version": 1,
                        "name": "dex",
                        "display_name": "DEX Contract Pack",
                        "category": "protocol",
                        "maturity": "candidate",
                        "description": "DEX contracts",
                        "source_owner_repo": "xian-dex",
                        "docs_path": "/contract-packs/dex",
                        "default_recipe": "core",
                        "contract_paths": [],
                        "contract_bundle_paths": [],
                        "recipes": [
                            {
                                "name": "core",
                                "display_name": "Core",
                                "summary": "Deploy core contracts",
                                "install": {
                                    "kind": ("xian-stack.localnet-dex-bootstrap"),
                                    "deploy_helper": True,
                                    "seed_demo_pool": False,
                                    "top_up_liquidity": False,
                                    "emit_test_swap": False,
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "contract-pack",
                        "list",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(configs_dir),
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload[0]["name"], "dex")
            self.assertEqual(payload[0]["default_recipe"], "core")

    def test_contract_pack_validate_checks_contract_bundle_sources(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            pack_dir = configs_dir / "contract-packs" / "dex"
            source_path = pack_dir / "contracts" / "con_demo.s.py"
            source_path.parent.mkdir(parents=True)
            source = "value = Variable()\n"
            source_path.write_text(source, encoding="utf-8")
            bundle_path = pack_dir / "contract-bundle.json"
            bundle_path.write_text(
                json.dumps(
                    {
                        "schema": "xian.contract_bundle.v1",
                        "schema_version": 1,
                        "name": "demo",
                        "display_name": "Demo",
                        "version": "0.1.0",
                        "contracts": [
                            {
                                "name": "con_demo",
                                "role": "demo",
                                "path": "contracts/con_demo.s.py",
                                "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                                "deploy_order": 10,
                                "default_chi": 100000,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (pack_dir / "contract-pack.json").write_text(
                json.dumps(
                    {
                        "schema": "xian.contract_pack.v1",
                        "schema_version": 1,
                        "name": "dex",
                        "display_name": "DEX Contract Pack",
                        "category": "protocol",
                        "maturity": "candidate",
                        "description": "DEX contracts",
                        "source_owner_repo": "xian-dex",
                        "docs_path": "/contract-packs/dex",
                        "default_recipe": "core",
                        "contract_paths": ["contract-packs/dex/contracts/con_demo.s.py"],
                        "contract_bundle_paths": ["contract-packs/dex/contract-bundle.json"],
                        "recipes": [
                            {
                                "name": "core",
                                "display_name": "Core",
                                "summary": "Deploy core contracts",
                                "install": {
                                    "kind": ("xian-stack.localnet-dex-bootstrap"),
                                    "deploy_helper": True,
                                    "seed_demo_pool": False,
                                    "top_up_liquidity": False,
                                    "emit_test_swap": False,
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "contract-pack",
                        "validate",
                        "dex",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(configs_dir),
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["contract_bundle_count"], 1)

    def test_contract_pack_install_dex_dry_run_uses_pack_bundle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            stack_dir = base_dir / "xian-stack"
            (stack_dir / "scripts").mkdir(parents=True)
            (stack_dir / "scripts" / "backend.py").write_text("# test backend\n", encoding="utf-8")
            pack_dir = configs_dir / "contract-packs" / "dex"
            source_path = pack_dir / "contracts" / "con_demo.s.py"
            source_path.parent.mkdir(parents=True)
            source = "value = Variable()\n"
            source_path.write_text(source, encoding="utf-8")
            (pack_dir / "contract-bundle.json").write_text(
                json.dumps(
                    {
                        "schema": "xian.contract_bundle.v1",
                        "schema_version": 1,
                        "name": "demo",
                        "display_name": "Demo",
                        "version": "0.1.0",
                        "contracts": [
                            {
                                "name": "con_demo",
                                "role": "demo",
                                "path": "contracts/con_demo.s.py",
                                "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (pack_dir / "contract-pack.json").write_text(
                json.dumps(
                    {
                        "schema": "xian.contract_pack.v1",
                        "schema_version": 1,
                        "name": "dex",
                        "display_name": "DEX Contract Pack",
                        "category": "protocol",
                        "maturity": "candidate",
                        "description": "DEX contracts",
                        "source_owner_repo": "xian-dex",
                        "docs_path": "/contract-packs/dex",
                        "default_recipe": "local-demo",
                        "contract_paths": ["contract-packs/dex/contracts/con_demo.s.py"],
                        "contract_bundle_paths": ["contract-packs/dex/contract-bundle.json"],
                        "recipes": [
                            {
                                "name": "local-demo",
                                "display_name": "Local Demo",
                                "summary": "Deploy demo contracts",
                                "install": {
                                    "kind": ("xian-stack.localnet-dex-bootstrap"),
                                    "deploy_helper": True,
                                    "seed_demo_pool": True,
                                    "top_up_liquidity": False,
                                    "emit_test_swap": False,
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "contract-pack",
                        "install",
                        "dex",
                        "--dry-run",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(configs_dir),
                        "--stack-dir",
                        str(stack_dir),
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["contract_pack"], "dex")
            self.assertEqual(payload["recipe"], "local-demo")
            self.assertIn("--seed-demo-pool", payload["command"])
            self.assertEqual(
                payload["bundle"],
                str((pack_dir / "contract-bundle.json").resolve()),
            )

    def test_contract_pack_install_external_dry_run_reports_owner_command(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            owner_repo = base_dir / "xian-stable-protocol"
            owner_repo.mkdir()
            pack_dir = configs_dir / "contract-packs" / "stable-protocol"
            source_path = pack_dir / "contracts" / "stable_token.s.py"
            source_path.parent.mkdir(parents=True)
            source = "balances = Hash(default_value=0)\n"
            source_path.write_text(source, encoding="utf-8")
            (pack_dir / "contract-bundle.json").write_text(
                json.dumps(
                    {
                        "schema": "xian.contract_bundle.v1",
                        "schema_version": 1,
                        "name": "stable",
                        "display_name": "Stable",
                        "version": "0.1.0",
                        "contracts": [
                            {
                                "name": "stable_token",
                                "role": "stable_token",
                                "path": "contracts/stable_token.s.py",
                                "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (pack_dir / "contract-pack.json").write_text(
                json.dumps(
                    {
                        "schema": "xian.contract_pack.v1",
                        "schema_version": 1,
                        "name": "stable-protocol",
                        "display_name": "Stable Protocol Contract Pack",
                        "category": "protocol",
                        "maturity": "candidate",
                        "description": "Stable protocol contracts",
                        "source_owner_repo": "xian-stable-protocol",
                        "docs_path": "/contract-packs/stable-protocol",
                        "default_recipe": "core",
                        "contract_paths": [
                            "contract-packs/stable-protocol/contracts/stable_token.s.py"
                        ],
                        "contract_bundle_paths": [
                            "contract-packs/stable-protocol/contract-bundle.json"
                        ],
                        "recipes": [
                            {
                                "name": "core",
                                "display_name": "Core",
                                "summary": "Run owner bootstrap",
                                "install": {
                                    "kind": "external",
                                    "repo": "xian-stable-protocol",
                                    "command": ("uv run python scripts/bootstrap_protocol.py"),
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "contract-pack",
                        "install",
                        "stable-protocol",
                        "--dry-run",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(configs_dir),
                        "--repo-dir",
                        str(owner_repo),
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["dry_run"])
            self.assertEqual(payload["contract_pack"], "stable-protocol")
            self.assertEqual(payload["recipe"], "core")
            self.assertEqual(payload["repo"], "xian-stable-protocol")
            self.assertEqual(payload["cwd"], str(owner_repo.resolve()))
            self.assertEqual(
                payload["command"],
                ["uv", "run", "python", "scripts/bootstrap_protocol.py"],
            )

    def test_example_starter_returns_selected_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            example_dir = configs_dir / "examples" / "dex-demo"
            example_dir.mkdir(parents=True)
            (example_dir / "example.json").write_text(
                json.dumps(
                    {
                        "schema": "xian.example.v1",
                        "schema_version": 1,
                        "name": "dex-demo",
                        "display_name": "DEX Demo Example",
                        "description": "DEX demo",
                        "use_case": "Run a DEX demo network.",
                        "recommended_local_template": "single-node-indexed",
                        "docs_path": "/contract-packs/dex",
                        "example_dir": "xian-dex",
                        "contract_packs": [{"name": "dex", "recipe": "local-demo"}],
                        "services": ["dex-automation"],
                        "contract_paths": [],
                        "contract_bundle_paths": ["contract-packs/dex/contract-bundle.json"],
                        "starter_flows": [
                            {
                                "name": "local",
                                "display_name": "Local Starter",
                                "template": "single-node-indexed",
                                "summary": "Local flow",
                                "network_name": "dex-local",
                                "node_name": "validator-1",
                                "steps": [
                                    {
                                        "title": "Install contract pack",
                                        "commands": ["uv run xian contract-pack install dex"],
                                        "notes": [],
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "example",
                        "starter",
                        "dex-demo",
                        "--flow",
                        "local",
                        "--base-dir",
                        str(base_dir),
                        "--configs-dir",
                        str(configs_dir),
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["name"], "dex-demo")
            self.assertEqual(payload["contract_packs"][0]["name"], "dex")
            self.assertEqual(payload["flow"]["template"], "single-node-indexed")

    def test_contract_bundle_validate_checks_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            bundle_dir = Path(tmp_dir)
            source_path = bundle_dir / "contracts" / "con_demo.s.py"
            source_path.parent.mkdir()
            source = "value = Variable()\n"
            source_path.write_text(source, encoding="utf-8")
            bundle_path = bundle_dir / "contract-bundle.json"
            bundle_path.write_text(
                json.dumps(
                    {
                        "schema": "xian.contract_bundle.v1",
                        "schema_version": 1,
                        "name": "demo",
                        "display_name": "Demo",
                        "version": "0.1.0",
                        "contracts": [
                            {
                                "name": "con_demo",
                                "role": "demo",
                                "path": "contracts/con_demo.s.py",
                                "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                                "deploy_order": 10,
                                "default_chi": 100000,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["contract", "bundle", "validate", str(bundle_path)])

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["name"], "demo")
            self.assertEqual(payload["contracts"][0]["role"], "demo")

    def test_contract_build_artifacts_emits_xian_vm_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            source_path = Path(tmp_dir) / "con_counter.s.py"
            source_path.write_text(
                "\n".join(
                    [
                        "counter = Variable()",
                        "",
                        "@construct",
                        "def seed():",
                        "    counter.set(0)",
                        "",
                        "@export",
                        "def get():",
                        "    return counter.get()",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(["contract", "build-artifacts", str(source_path)])

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["format"], "xian_contract_artifact_v1")
            self.assertEqual(payload["module_name"], "con_counter")
            self.assertEqual(payload["vm_profile"], "xian_vm_v1")
            self.assertIn("vm_ir_json", payload)
            self.assertIn("vm_ir_sha256", payload["hashes"])

    def test_contract_build_artifacts_writes_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            source_path = tmp_path / "counter.py"
            output_path = tmp_path / "artifacts.json"
            source_path.write_text(
                "\n".join(
                    [
                        "counter = Variable()",
                        "",
                        "@export",
                        "def get():",
                        "    return counter.get()",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "contract",
                        "build-artifacts",
                        str(source_path),
                        "--name",
                        "con_counter",
                        "--output",
                        str(output_path),
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(stdout.getvalue(), "")
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["module_name"], "con_counter")
            self.assertEqual(payload["vm_profile"], "xian_vm_v1")

    def test_contract_build_artifacts_reads_stdin_and_preserves_hashes(
        self,
    ) -> None:
        source = "\n".join(
            [
                "balances = Hash(default_value=0)",
                "allowances = Hash(default_value=0)",
                "",
                "@construct",
                "def seed(owner: str, supply: int):",
                "    balances[owner] = supply",
                "",
                "@export",
                "def approve(spender: str, amount: int):",
                "    assert amount > 0, 'amount must be positive!'",
                "    allowances[ctx.caller, spender] = amount",
                "    return allowances[ctx.caller, spender]",
                "",
                "@export",
                "def transfer_from(owner: str, to: str, amount: int):",
                "    assert allowances[owner, ctx.caller] >= amount, (",
                "        'allowance too low!'",
                "    )",
                "    assert balances[owner] >= amount, 'balance too low!'",
                "    allowances[owner, ctx.caller] -= amount",
                "    balances[owner] -= amount",
                "    balances[to] += amount",
                "    return balances[to]",
                "",
            ]
        )

        stdout = io.StringIO()
        with patch("sys.stdin", io.StringIO(source)), redirect_stdout(stdout):
            exit_code = main(
                [
                    "contract",
                    "build-artifacts",
                    "-",
                    "--name",
                    "con_allowance_probe",
                ]
            )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["format"], "xian_contract_artifact_v1")
        self.assertEqual(payload["module_name"], "con_allowance_probe")
        self.assertNotIn("runtime_code", payload)
        self.assertEqual(
            payload["hashes"]["source_sha256"],
            hashlib.sha256(payload["source"].encode()).hexdigest(),
        )
        self.assertEqual(
            payload["hashes"]["vm_ir_sha256"],
            hashlib.sha256(payload["vm_ir_json"].encode()).hexdigest(),
        )

    def test_read_contract_pack_requires_explicit_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            pack_path = Path(tmp_dir) / "contract-pack.json"
            pack_path.write_text(
                json.dumps(
                    {
                        "schema": "xian.contract_pack.v1",
                        "name": "dex",
                        "display_name": "DEX Contract Pack",
                        "category": "protocol",
                        "maturity": "candidate",
                        "description": "DEX contracts",
                        "source_owner_repo": "xian-dex",
                        "docs_path": "/contract-packs/dex",
                        "default_recipe": "core",
                        "contract_paths": [],
                        "contract_bundle_paths": [],
                        "recipes": [
                            {
                                "name": "core",
                                "display_name": "Core",
                                "summary": "Deploy core contracts",
                                "install": {"kind": "external"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "unsupported schema_version"):
                read_contract_pack(pack_path)

    def test_read_contract_pack_rejects_unknown_nested_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            pack_path = Path(tmp_dir) / "contract-pack.json"
            pack_path.write_text(
                json.dumps(
                    {
                        "schema": "xian.contract_pack.v1",
                        "schema_version": 1,
                        "name": "dex",
                        "display_name": "DEX Contract Pack",
                        "category": "protocol",
                        "maturity": "candidate",
                        "description": "DEX contracts",
                        "source_owner_repo": "xian-dex",
                        "docs_path": "/contract-packs/dex",
                        "default_recipe": "core",
                        "contract_paths": [],
                        "contract_bundle_paths": [],
                        "recipes": [
                            {
                                "name": "core",
                                "display_name": "Core",
                                "summary": "Deploy core contracts",
                                "install": {
                                    "kind": "external",
                                    "repo": "xian-dex",
                                    "command": "make bootstrap",
                                    "silent": True,
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "contract pack recipe install has unknown field\\(s\\): silent",
            ):
                read_contract_pack(pack_path)

    def test_read_example_requires_explicit_schema_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            example_path = Path(tmp_dir) / "example.json"
            example_path.write_text(
                json.dumps(
                    {
                        "schema": "xian.example.v1",
                        "name": "dex-demo",
                        "display_name": "DEX Demo Example",
                        "description": "DEX demo",
                        "use_case": "Run a DEX demo network.",
                        "recommended_local_template": "single-node-indexed",
                        "docs_path": "/contract-packs/dex",
                        "example_dir": "xian-dex",
                        "contract_packs": [],
                        "services": [],
                        "contract_paths": [],
                        "contract_bundle_paths": [],
                        "starter_flows": [
                            {
                                "name": "local",
                                "display_name": "Local",
                                "template": "single-node-indexed",
                                "summary": "Local flow",
                                "steps": [
                                    {
                                        "title": "Start",
                                        "commands": ["make localnet-up"],
                                        "notes": [],
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "unsupported schema_version"):
                read_example(example_path)

    def test_read_example_rejects_unknown_nested_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            example_path = Path(tmp_dir) / "example.json"
            example_path.write_text(
                json.dumps(
                    {
                        "schema": "xian.example.v1",
                        "schema_version": 1,
                        "name": "dex-demo",
                        "display_name": "DEX Demo Example",
                        "description": "DEX demo",
                        "use_case": "Run a DEX demo network.",
                        "recommended_local_template": "single-node-indexed",
                        "docs_path": "/examples/dex-demo",
                        "example_dir": "xian-dex",
                        "contract_packs": [
                            {
                                "name": "dex",
                                "recipe": "local-demo",
                                "template": "single-node-indexed",
                            }
                        ],
                        "services": [],
                        "contract_paths": [],
                        "contract_bundle_paths": [],
                        "starter_flows": [
                            {
                                "name": "local",
                                "display_name": "Local",
                                "template": "single-node-indexed",
                                "summary": "Local flow",
                                "steps": [
                                    {
                                        "title": "Start",
                                        "commands": ["make localnet-up"],
                                        "notes": [],
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "example contract pack reference has unknown field\\(s\\): template",
            ):
                read_example(example_path)

    def test_resolve_configs_dir_uses_workspace_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            resolved = resolve_configs_dir(Path(tmp_dir))

        expected = (WORKSPACE_ROOT / "xian-configs").resolve()
        self.assertEqual(resolved, expected)

    def test_resolve_configs_dir_rejects_missing_explicit_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            missing = Path(tmp_dir) / "missing-configs"

            with self.assertRaisesRegex(
                FileNotFoundError,
                "xian-configs directory does not exist",
            ):
                resolve_configs_dir(Path(tmp_dir), explicit=missing)

    def test_resolve_contract_pack_path_prefers_canonical_configs_repo(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            pack_path = configs_dir / "contract-packs" / "dex" / "contract-pack.json"
            pack_path.parent.mkdir(parents=True)
            pack_path.write_text("{}", encoding="utf-8")

            resolved = resolve_contract_pack_path(
                base_dir=base_dir,
                pack_name="dex",
                configs_dir=configs_dir,
            )

        self.assertEqual(resolved, pack_path.resolve())

    def test_resolve_example_path_prefers_canonical_configs_repo(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            example_path = configs_dir / "examples" / "dex-demo" / "example.json"
            example_path.parent.mkdir(parents=True)
            example_path.write_text("{}", encoding="utf-8")

            resolved = resolve_example_path(
                base_dir=base_dir,
                example_name="dex-demo",
                configs_dir=configs_dir,
            )

        self.assertEqual(resolved, example_path.resolve())

    def test_resolve_network_manifest_path_prefers_local_network_dir(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            manifest_path = base_dir / "networks" / "mainnet" / "manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text("{}", encoding="utf-8")

            resolved = resolve_network_manifest_path(
                base_dir=base_dir,
                network_name="mainnet",
            )

        self.assertEqual(resolved, manifest_path.resolve())

    def test_resolve_network_manifest_path_prefers_canonical_configs_repo(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            manifest_path = configs_dir / "networks" / "mainnet" / "manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text("{}", encoding="utf-8")

            resolved = resolve_network_manifest_path(
                base_dir=base_dir,
                network_name="mainnet",
                configs_dir=configs_dir,
            )

        self.assertEqual(resolved, manifest_path.resolve())

    def test_resolve_network_template_path_prefers_canonical_configs_repo(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            template_path = configs_dir / "templates" / "single-node-dev.json"
            template_path.parent.mkdir(parents=True)
            template_path.write_text("{}", encoding="utf-8")

            resolved = resolve_network_template_path(
                base_dir=base_dir,
                template_name="single-node-dev",
                configs_dir=configs_dir,
            )

        self.assertEqual(resolved, template_path.resolve())


if __name__ == "__main__":
    unittest.main()
