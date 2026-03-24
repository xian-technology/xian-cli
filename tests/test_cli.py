from __future__ import annotations

import importlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

import xian_cli.abci_bridge as abci_bridge
from xian_cli.cli import main
from xian_cli.config_repo import (
    resolve_configs_dir,
    resolve_network_manifest_path,
    resolve_network_template_path,
)
from xian_cli.models import (
    read_network_manifest,
    read_network_template,
    read_node_profile,
)
from xian_cli.runtime import (
    get_xian_stack_node_status,
    resolve_stack_dir,
    start_xian_stack_node,
    stop_xian_stack_node,
    wait_for_rpc_ready,
)

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_DEVNET_GENESIS = (
    WORKSPACE_ROOT / "xian-configs" / "networks" / "devnet" / "genesis.json"
)


class ValidatorKeyTests(unittest.TestCase):
    def test_generate_validator_material_shape(self) -> None:
        payload = (
            abci_bridge.get_node_setup_module().generate_validator_material()
        )
        self.assertEqual(len(payload["validator_private_key_hex"]), 64)
        self.assertEqual(len(payload["validator_public_key_hex"]), 64)
        self.assertIn("address", payload["priv_validator_key"])
        self.assertIn("pub_key", payload["priv_validator_key"])
        self.assertIn("priv_key", payload["priv_validator_key"])

    def test_generate_validator_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    ["keys", "validator", "generate", "--out-dir", tmp_dir]
                )
            self.assertEqual(exit_code, 0)

            output_dir = Path(tmp_dir)
            priv_validator_path = output_dir / "priv_validator_key.json"
            metadata_path = output_dir / "validator_key_info.json"

            self.assertTrue(priv_validator_path.exists())
            self.assertTrue(metadata_path.exists())

            priv_validator_payload = json.loads(
                priv_validator_path.read_text(encoding="utf-8")
            )
            metadata_payload = json.loads(
                metadata_path.read_text(encoding="utf-8")
            )

            self.assertEqual(
                priv_validator_payload["address"],
                metadata_payload["priv_validator_key"]["address"],
            )


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
                        "runtime_backend": "xian-stack",
                        "block_policy_mode": "on_demand",
                        "block_policy_interval": "0s",
                        "tracer_mode": "python_line_v1",
                        "bootstrap_node_name": "validator-1",
                        "additional_validator_names": [],
                        "service_node": False,
                        "dashboard_enabled": True,
                        "monitoring_enabled": False,
                        "dashboard_host": "0.0.0.0",
                        "dashboard_port": 18080,
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
            self.assertTrue(payload[0]["dashboard_enabled"])

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
            self.assertEqual(manifest["mode"], "create")
            self.assertEqual(manifest["runtime_backend"], "xian-stack")
            self.assertEqual(manifest["block_policy_mode"], "on_demand")
            self.assertEqual(manifest["block_policy_interval"], "0s")
            self.assertEqual(manifest["tracer_mode"], "python_line_v1")

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
            manifest_path = (
                base_dir / "networks" / "local-dev" / "manifest.json"
            )
            self.assertEqual(
                result["manifest_path"],
                str(manifest_path.resolve()),
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["mode"], "create")
            self.assertIsNone(manifest["genesis_source"])
            self.assertEqual(manifest["block_policy_mode"], "on_demand")
            self.assertEqual(manifest["block_policy_interval"], "0s")
            self.assertEqual(manifest["tracer_mode"], "python_line_v1")

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
                        "runtime_backend": "xian-stack",
                        "block_policy_mode": "on_demand",
                        "block_policy_interval": "0s",
                        "tracer_mode": "native_instruction_v1",
                        "bootstrap_node_name": "validator-1",
                        "additional_validator_names": [],
                        "service_node": True,
                        "dashboard_enabled": True,
                        "monitoring_enabled": True,
                        "dashboard_host": "0.0.0.0",
                        "dashboard_port": 18080,
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
                        str(CANONICAL_DEVNET_GENESIS),
                    ]
                )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            manifest = json.loads(
                (
                    base_dir / "networks" / "local-dev" / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            profile = json.loads(
                (base_dir / "nodes" / "validator-1.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(result["template"], "single-node-indexed")
            self.assertEqual(manifest["tracer_mode"], "native_instruction_v1")
            self.assertTrue(profile["service_node"])
            self.assertTrue(profile["dashboard_enabled"])
            self.assertTrue(profile["monitoring_enabled"])
            self.assertEqual(profile["dashboard_host"], "0.0.0.0")
            self.assertEqual(profile["dashboard_port"], 18080)

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
                        "mainnet",
                        "--seed",
                        "abc@127.0.0.1:26656",
                        "--service-node",
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
            self.assertEqual(profile["network"], "mainnet")
            self.assertEqual(profile["moniker"], "validator-1")
            self.assertEqual(profile["seeds"], ["abc@127.0.0.1:26656"])
            self.assertTrue(profile["service_node"])
            self.assertTrue(profile["dashboard_enabled"])
            self.assertEqual(profile["dashboard_host"], "0.0.0.0")
            self.assertEqual(profile["dashboard_port"], 18080)
            self.assertEqual(profile["block_policy_mode"], "on_demand")
            self.assertEqual(profile["block_policy_interval"], "0s")
            self.assertEqual(profile["tracer_mode"], "python_line_v1")

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
                        str(CANONICAL_DEVNET_GENESIS),
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
            self.assertTrue(profile["dashboard_enabled"])
            self.assertEqual(profile["dashboard_host"], "0.0.0.0")
            self.assertEqual(profile["dashboard_port"], 18080)

    def test_network_join_uses_canonical_manifest_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "canonical",
                        "chain_id": "xian-canonical-1",
                        "mode": "join",
                        "runtime_backend": "xian-stack",
                        "genesis_source": "./genesis.json",
                        "snapshot_url": "https://example.invalid/snapshot",
                        "seed_nodes": ["seed-1@127.0.0.1:26656"],
                        "block_policy_mode": "periodic",
                        "block_policy_interval": "10s",
                        "tracer_mode": "native_instruction_v1",
                    }
                ),
                encoding="utf-8",
            )

            output_path = base_dir / "nodes" / "validator-1.json"
            with patch.dict(
                "os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}
            ):
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
            self.assertEqual(profile["runtime_backend"], "xian-stack")
            self.assertEqual(profile["seeds"], [])
            self.assertIsNone(profile["snapshot_url"])
            self.assertEqual(profile["block_policy_mode"], "periodic")
            self.assertEqual(profile["block_policy_interval"], "10s")
            self.assertEqual(profile["tracer_mode"], "native_instruction_v1")

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
                    {
                        "schema_version": 1,
                        "name": "canonical",
                        "chain_id": "xian-canonical-1",
                        "mode": "join",
                        "runtime_backend": "xian-stack",
                        "genesis_source": "./genesis.json",
                        "snapshot_url": None,
                        "seed_nodes": [],
                        "block_policy_mode": "on_demand",
                        "block_policy_interval": "0s",
                        "tracer_mode": "python_line_v1",
                    }
                ),
                encoding="utf-8",
            )
            (templates_dir / "embedded-backend.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "embedded-backend",
                        "display_name": "Embedded Backend",
                        "description": "App backend defaults",
                        "runtime_backend": "xian-stack",
                        "block_policy_mode": "on_demand",
                        "block_policy_interval": "0s",
                        "tracer_mode": "native_instruction_v1",
                        "bootstrap_node_name": "validator-1",
                        "additional_validator_names": [],
                        "service_node": True,
                        "dashboard_enabled": False,
                        "monitoring_enabled": True,
                        "dashboard_host": "127.0.0.1",
                        "dashboard_port": 8080,
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
                        "embedded-backend",
                        "--output",
                        str(output_path),
                    ]
                )

            self.assertEqual(exit_code, 0)
            profile = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertTrue(profile["service_node"])
            self.assertTrue(profile["monitoring_enabled"])
            self.assertFalse(profile["dashboard_enabled"])
            self.assertEqual(profile["tracer_mode"], "native_instruction_v1")

    def test_network_join_allows_block_policy_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "canonical",
                        "chain_id": "xian-canonical-1",
                        "mode": "join",
                        "runtime_backend": "xian-stack",
                        "genesis_source": "./genesis.json",
                        "snapshot_url": None,
                        "seed_nodes": [],
                        "block_policy_mode": "on_demand",
                        "block_policy_interval": "0s",
                        "tracer_mode": "python_line_v1",
                    }
                ),
                encoding="utf-8",
            )

            output_path = base_dir / "nodes" / "validator-1.json"
            with patch.dict(
                "os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}
            ):
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
                            "--tracer-mode",
                            "native_instruction_v1",
                            "--output",
                            str(output_path),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            profile = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(profile["block_policy_mode"], "idle_interval")
            self.assertEqual(profile["block_policy_interval"], "10s")
            self.assertEqual(profile["tracer_mode"], "native_instruction_v1")

    def test_network_join_allows_node_local_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "canonical",
                        "chain_id": "xian-canonical-1",
                        "mode": "join",
                        "runtime_backend": "xian-stack",
                        "genesis_source": "./genesis.json",
                        "snapshot_url": None,
                        "seed_nodes": ["seed-1@127.0.0.1:26656"],
                        "tracer_mode": "python_line_v1",
                    }
                ),
                encoding="utf-8",
            )

            output_path = base_dir / "nodes" / "validator-1.json"
            with patch.dict(
                "os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}
            ):
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
            self.assertEqual(profile["runtime_backend"], "xian-stack")
            self.assertEqual(profile["seeds"], ["local-seed@127.0.0.1:26656"])
            self.assertEqual(
                profile["snapshot_url"],
                "https://example.invalid/node-snapshot",
            )
            self.assertEqual(profile["tracer_mode"], "python_line_v1")

    def test_network_join_can_generate_validator_key_material(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            network_dir = configs_dir / "networks" / "canonical"
            network_dir.mkdir(parents=True)
            (network_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "name": "canonical",
                        "chain_id": "xian-canonical-1",
                        "mode": "join",
                        "runtime_backend": "xian-stack",
                        "genesis_source": "./genesis.json",
                        "snapshot_url": None,
                        "seed_nodes": [],
                        "tracer_mode": "python_line_v1",
                    }
                ),
                encoding="utf-8",
            )

            output_path = base_dir / "nodes" / "validator-1.json"
            with patch.dict(
                "os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}
            ):
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
                (
                    base_dir
                    / "keys"
                    / "validator-1"
                    / "priv_validator_key.json"
                ).exists()
            )
            self.assertTrue(
                (
                    base_dir
                    / "keys"
                    / "validator-1"
                    / "validator_key_info.json"
                ).exists()
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
                        "mainnet",
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
                        "mainnet",
                        "--restore-snapshot",
                    ]
                )

    def test_network_join_can_initialize_node_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()

            genesis_source = CANONICAL_DEVNET_GENESIS

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
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
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
                    ]
                )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            self.assertTrue(result["node_initialized"])
            home = Path(result["node_init"]["home"])
            self.assertTrue((home / "config" / "config.toml").exists())
            self.assertTrue((home / "config" / "genesis.json").exists())
            profile = json.loads(
                (base_dir / "nodes" / "validator-1.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                profile["validator_key_ref"],
                "keys/validator-1/validator_key_info.json",
            )

    def test_network_create_can_bootstrap_local_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            home = base_dir / ".cometbft"
            stdout = io.StringIO()

            fake_genesis = {
                "chain_id": "xian-local-1",
                "validators": [],
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

            with patch(
                "xian_cli.cli.get_genesis_builder_module",
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
            manifest_path = (
                base_dir / "networks" / "local-dev" / "manifest.json"
            )
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
            self.assertTrue(
                (
                    base_dir
                    / "keys"
                    / "validator-1"
                    / "validator_key_info.json"
                ).exists()
            )

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["genesis_source"], "./genesis.json")
            self.assertEqual(
                profile["validator_key_ref"],
                "keys/validator-1/validator_key_info.json",
            )
            (genesis_builder.build_local_network_genesis.assert_called_once())

    def test_network_create_can_generate_multiple_initial_validators(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
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
                "xian_cli.cli.get_genesis_builder_module",
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
                            "--validator",
                            "validator-2",
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
                (
                    base_dir
                    / "keys"
                    / "validator-2"
                    / "validator_key_info.json"
                ).exists()
            )
            validator_two_profile = json.loads(
                (base_dir / "nodes" / "validator-2.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                validator_two_profile["validator_key_ref"],
                "keys/validator-2/validator_key_info.json",
            )
            genesis_builder.build_local_network_genesis.assert_called_once()
            kwargs = (
                genesis_builder.build_local_network_genesis.call_args.kwargs
            )
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
    def test_node_init_materializes_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            (base_dir / "keys" / "validator-1").mkdir(parents=True)

            genesis_source = CANONICAL_DEVNET_GENESIS

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
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
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
                        str(
                            base_dir
                            / "keys"
                            / "validator-1"
                            / "validator_key_info.json"
                        ),
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
            self.assertTrue((home / "config" / "config.toml").exists())
            self.assertTrue((home / "config" / "genesis.json").exists())
            self.assertTrue(
                (home / "config" / "priv_validator_key.json").exists()
            )
            self.assertTrue((home / "config" / "node_key.json").exists())
            self.assertTrue(
                (home / "data" / "priv_validator_state.json").exists()
            )

    def test_node_init_supports_remote_genesis_url(self) -> None:
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
            genesis_url = "https://example.invalid/genesis.json"
            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "remote-dev",
                        "--chain-id",
                        "xian-remote-1",
                        "--genesis-source",
                        genesis_url,
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "remote-dev"
                            / "manifest.json"
                        ),
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
                        str(
                            base_dir
                            / "keys"
                            / "validator-1"
                            / "validator_key_info.json"
                        ),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch("xian_cli.cli.fetch_json", return_value=genesis_payload):
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

    def test_node_init_prefers_profile_genesis_url_override(self) -> None:
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
                        str(
                            base_dir
                            / "networks"
                            / "override-dev"
                            / "manifest.json"
                        ),
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
                        str(
                            base_dir
                            / "keys"
                            / "validator-1"
                            / "validator_key_info.json"
                        ),
                        "--genesis-url",
                        "https://example.invalid/override-genesis.json",
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch(
                "xian_cli.cli.fetch_json",
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

            genesis_source = CANONICAL_DEVNET_GENESIS

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
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
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
            self.assertTrue(
                (home / "config" / "priv_validator_key.json").exists()
            )

    def test_node_init_rejects_chain_id_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            (base_dir / "keys" / "validator-1").mkdir(parents=True)

            genesis_source = CANONICAL_DEVNET_GENESIS

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
                        str(
                            base_dir
                            / "networks"
                            / "bad-chain"
                            / "manifest.json"
                        ),
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
                        str(
                            base_dir
                            / "keys"
                            / "validator-1"
                            / "validator_key_info.json"
                        ),
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
                    {
                        "schema_version": 1,
                        "name": "canonical",
                        "chain_id": "xian-canonical-1",
                        "mode": "join",
                        "runtime_backend": "xian-stack",
                        "genesis_source": "./genesis.json",
                        "snapshot_url": None,
                        "seed_nodes": ["seed-1@127.0.0.1:26656"],
                    }
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
            with patch.dict(
                "os.environ", {"XIAN_CONFIGS_DIR": str(configs_dir)}
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
            rendered_config = (home / "config" / "config.toml").read_text(
                encoding="utf-8"
            )

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

            genesis_source = CANONICAL_DEVNET_GENESIS

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
                        "https://example.invalid/snapshot.tar.gz",
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
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
                "xian_cli.cli.get_node_admin_module",
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
                "https://example.invalid/snapshot.tar.gz",
                home,
            )
            result = json.loads(stdout.getvalue())
            self.assertTrue(result["snapshot_restored"])
            self.assertEqual(
                result["effective_snapshot_url"],
                "https://example.invalid/snapshot.tar.gz",
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

            with redirect_stdout(io.StringIO()):
                main(
                    [
                        "network",
                        "create",
                        "local-dev",
                        "--chain-id",
                        "xian-local-1",
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
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
            with patch("xian_cli.cli.start_xian_stack_node") as start_node:
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
            self.assertFalse(kwargs["service_node"])
            self.assertTrue(kwargs["dashboard_enabled"])
            self.assertTrue(kwargs["monitoring_enabled"])
            self.assertEqual(kwargs["dashboard_host"], "0.0.0.0")
            self.assertEqual(kwargs["dashboard_port"], 18080)
            self.assertFalse(kwargs["wait_for_rpc"])

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
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
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
                        "--service-node",
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch("xian_cli.cli.stop_xian_stack_node") as stop_node:
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
            self.assertTrue(kwargs["service_node"])
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
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
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
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
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
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch(
                "xian_cli.cli.fetch_json",
                return_value={"result": {"node_info": {"network": "xian"}}},
            ):
                with patch(
                    "xian_cli.cli.get_xian_stack_node_status",
                    return_value={
                        "backend_running": True,
                        "node_id": "node-123",
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
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
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
                        str(
                            base_dir
                            / "keys"
                            / "validator-1"
                            / "validator_key_info.json"
                        ),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch(
                "xian_cli.cli.get_node_setup_module",
                return_value=type(
                    "NodeSetup", (), {"__name__": "node_setup"}
                )(),
            ):
                with patch(
                    "xian_cli.cli.get_node_admin_module",
                    return_value=type(
                        "NodeAdmin", (), {"__name__": "node_admin"}
                    )(),
                ):
                    with patch(
                        "xian_cli.cli.get_genesis_builder_module",
                        return_value=type(
                            "GenesisBuilder",
                            (),
                            {"__name__": "genesis_builder"},
                        )(),
                    ):
                        with redirect_stdout(stdout):
                            with patch(
                                "xian_cli.cli.get_xian_stack_node_status",
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
                                    ]
                                )

            self.assertEqual(exit_code, 0)
            result = json.loads(stdout.getvalue())
            self.assertTrue(result["ok"])
            check_names = {check["name"] for check in result["checks"]}
            self.assertIn("configs_dir", check_names)
            self.assertIn("stack_dir", check_names)
            self.assertIn("node_status", check_names)


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
                        "https://example.invalid/network-snapshot.tar.gz",
                        "--output",
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
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
                "xian_cli.cli.get_node_admin_module",
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
                "https://example.invalid/network-snapshot.tar.gz",
                home,
            )
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["home"], str(home))
            self.assertEqual(
                result["snapshot_url"],
                "https://example.invalid/network-snapshot.tar.gz",
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
                        str(
                            base_dir
                            / "networks"
                            / "local-dev"
                            / "manifest.json"
                        ),
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

            with self.assertRaisesRegex(
                FileNotFoundError, "run `xian node init"
            ):
                main(
                    [
                        "snapshot",
                        "restore",
                        "validator-1",
                        "--base-dir",
                        str(base_dir),
                    ]
                )


class RuntimeHelperTests(unittest.TestCase):
    def test_resolve_stack_dir_uses_workspace_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            resolved = resolve_stack_dir(Path(tmp_dir))

        expected = (
            Path(__file__).resolve().parents[2] / "xian-stack"
        ).resolve()
        self.assertEqual(resolved, expected)

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
        rpc_status = {"result": {"sync_info": {"catching_up": False}}}

        with patch(
            "xian_cli.runtime.run_backend_command"
        ) as run_backend_command:
            run_backend_command.return_value = {"rpc_status": rpc_status}
            result = start_xian_stack_node(
                stack_dir=stack_dir,
                service_node=True,
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
            service_node=True,
            dashboard_enabled=True,
            monitoring_enabled=True,
            dashboard_host="0.0.0.0",
            dashboard_port=18080,
            wait_for_health=True,
            rpc_timeout_seconds=12.5,
            rpc_url="http://127.0.0.1:26657/status",
        )
        self.assertEqual(result["rpc_status"], rpc_status)

    def test_stop_xian_stack_node_uses_backend_command(self) -> None:
        stack_dir = Path("/tmp/xian-stack")

        with patch(
            "xian_cli.runtime.run_backend_command"
        ) as run_backend_command:
            run_backend_command.return_value = {"container_target": "abci-down"}
            result = stop_xian_stack_node(
                stack_dir=stack_dir,
                service_node=False,
                dashboard_enabled=True,
                monitoring_enabled=True,
                dashboard_host="0.0.0.0",
                dashboard_port=18080,
            )

        run_backend_command.assert_called_once_with(
            stack_dir,
            "stop",
            service_node=False,
            dashboard_enabled=True,
            monitoring_enabled=True,
            dashboard_host="0.0.0.0",
            dashboard_port=18080,
        )
        self.assertEqual(result["container_target"], "abci-down")

    def test_get_xian_stack_node_status_uses_backend_command(self) -> None:
        stack_dir = Path("/tmp/xian-stack")
        with patch(
            "xian_cli.runtime.run_backend_command"
        ) as run_backend_command:
            run_backend_command.return_value = {
                "backend_running": True,
                "node_id": "abc123",
            }
            result = get_xian_stack_node_status(
                stack_dir=stack_dir,
                service_node=True,
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
            service_node=True,
            dashboard_enabled=True,
            monitoring_enabled=True,
            dashboard_host="0.0.0.0",
            dashboard_port=18080,
        )


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
                        "runtime_backend": "xian-stack",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError, "unsupported schema_version"
            ):
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

            with self.assertRaisesRegex(
                ValueError, "unsupported schema_version"
            ):
                read_node_profile(profile_path)

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
                        "runtime_backend": "xian-stack",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError, "unsupported schema_version"
            ):
                read_network_template(template_path)

    def test_resolve_configs_dir_uses_workspace_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            resolved = resolve_configs_dir(Path(tmp_dir))

        expected = (WORKSPACE_ROOT / "xian-configs").resolve()
        self.assertEqual(resolved, expected)

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
            manifest_path = (
                configs_dir / "networks" / "mainnet" / "manifest.json"
            )
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
