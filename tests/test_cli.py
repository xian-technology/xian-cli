from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from xian_cli.cli import main
from xian_cli.cometbft import generate_validator_material


class ValidatorKeyTests(unittest.TestCase):
    def test_generate_validator_material_shape(self) -> None:
        payload = generate_validator_material()
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
            metadata_payload = json.loads(metadata_path.read_text(encoding="utf-8"))

            self.assertEqual(
                priv_validator_payload["address"],
                metadata_payload["priv_validator_key"]["address"],
            )


class NetworkManifestTests(unittest.TestCase):
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
            self.assertEqual(manifest["name"], "local-dev")
            self.assertEqual(manifest["chain_id"], "xian-local-1")
            self.assertEqual(manifest["mode"], "join")
            self.assertEqual(manifest["runtime_backend"], "xian-stack")

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
                        "--output",
                        str(output_path),
                    ]
                )
            self.assertEqual(exit_code, 0)
            profile = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(profile["name"], "validator-1")
            self.assertEqual(profile["network"], "mainnet")
            self.assertEqual(profile["moniker"], "validator-1")
            self.assertEqual(profile["seeds"], ["abc@127.0.0.1:26656"])
            self.assertTrue(profile["service_node"])


class NodeInitTests(unittest.TestCase):
    def test_node_init_materializes_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            (base_dir / "keys" / "validator-1").mkdir(parents=True)

            genesis_source = (
                Path(__file__).resolve().parents[2]
                / "xian-abci"
                / "src"
                / "xian"
                / "tools"
                / "genesis"
                / "genesis-devnet.json"
            )

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
                        str(base_dir / "networks" / "local-dev.json"),
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
            self.assertTrue((home / "config" / "priv_validator_key.json").exists())
            self.assertTrue((home / "config" / "node_key.json").exists())
            self.assertTrue((home / "data" / "priv_validator_state.json").exists())

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
                        str(base_dir / "networks" / "remote-dev.json"),
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
            self.assertEqual(home, (base_dir / "xian-stack" / ".cometbft"))


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
                        str(base_dir / "networks" / "local-dev.json"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--network",
                        "local-dev",
                        "--stack-dir",
                        str(stack_dir),
                        "--output",
                        str(base_dir / "nodes" / "validator-1.json"),
                    ]
                )

            stdout = io.StringIO()
            with patch("xian_cli.cli.start_xian_stack_node") as start_node:
                start_node.return_value = {
                    "stack_dir": str(stack_dir),
                    "container_target": "abci-up",
                    "node_target": "up",
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
                        str(base_dir / "networks" / "local-dev.json"),
                    ]
                )
                main(
                    [
                        "network",
                        "join",
                        "validator-1",
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


if __name__ == "__main__":
    unittest.main()
