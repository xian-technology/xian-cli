from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import call, patch
from urllib.error import URLError

import xian_cli.abci_bridge as abci_bridge
from xian_cli.cli import main
from xian_cli.cometbft import generate_validator_material
from xian_cli.config_repo import (
    resolve_configs_dir,
    resolve_network_manifest_path,
)
from xian_cli.runtime import (
    resolve_stack_dir,
    start_xian_stack_node,
    stop_xian_stack_node,
    wait_for_rpc_ready,
)

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
LEGACY_GENESIS_DIR = WORKSPACE_ROOT / "xian-configs" / "legacy" / "genesis"


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
            metadata_payload = json.loads(
                metadata_path.read_text(encoding="utf-8")
            )

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

    def test_network_join_uses_canonical_manifest_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            (configs_dir / "networks").mkdir(parents=True)
            (configs_dir / "networks" / "canonical.json").write_text(
                json.dumps(
                    {
                        "name": "canonical",
                        "chain_id": "xian-canonical-1",
                        "mode": "join",
                        "runtime_backend": "custom-backend",
                        "genesis_source": (
                            "../legacy/genesis/genesis-canonical.json"
                        ),
                        "snapshot_url": "https://example.invalid/snapshot",
                        "seed_nodes": ["seed-1@127.0.0.1:26656"],
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
            self.assertEqual(profile["runtime_backend"], "custom-backend")
            self.assertEqual(profile["seeds"], [])
            self.assertIsNone(profile["snapshot_url"])

    def test_network_join_allows_node_local_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            (configs_dir / "networks").mkdir(parents=True)
            (configs_dir / "networks" / "canonical.json").write_text(
                json.dumps(
                    {
                        "name": "canonical",
                        "chain_id": "xian-canonical-1",
                        "mode": "join",
                        "runtime_backend": "xian-stack",
                        "genesis_source": (
                            "../legacy/genesis/genesis-canonical.json"
                        ),
                        "snapshot_url": None,
                        "seed_nodes": ["seed-1@127.0.0.1:26656"],
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
                            "--runtime-backend",
                            "custom-backend",
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
            self.assertEqual(profile["runtime_backend"], "custom-backend")
            self.assertEqual(profile["seeds"], ["local-seed@127.0.0.1:26656"])
            self.assertEqual(
                profile["snapshot_url"],
                "https://example.invalid/node-snapshot",
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


class AbciBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_sys_path = list(sys.path)
        self.original_modules = {
            name: sys.modules.pop(name, None)
            for name in ("xian", "xian.node_setup")
        }
        self.workspace_src = (
            Path(__file__).resolve().parents[2] / "xian-abci" / "src"
        ).resolve()
        sys.path[:] = [
            path
            for path in self.original_sys_path
            if Path(path).resolve() != self.workspace_src
        ]
        abci_bridge.get_node_setup_module.cache_clear()

    def tearDown(self) -> None:
        abci_bridge.get_node_setup_module.cache_clear()
        for name in ("xian", "xian.node_setup"):
            sys.modules.pop(name, None)
        for name, module in self.original_modules.items():
            if module is not None:
                sys.modules[name] = module
        sys.path[:] = self.original_sys_path

    def test_bridge_uses_workspace_fallback(self) -> None:
        module = abci_bridge.get_node_setup_module()

        self.assertEqual(module.__name__, "xian.node_setup")
        self.assertEqual(Path(sys.path[0]).resolve(), self.workspace_src)

    def test_bridge_errors_when_helpers_are_unavailable(self) -> None:
        original_exists = Path.exists

        def fake_exists(path: Path) -> bool:
            if path.resolve() == self.workspace_src:
                return False
            return original_exists(path)

        with patch.object(
            Path,
            "exists",
            autospec=True,
            side_effect=fake_exists,
        ):
            with self.assertRaisesRegex(RuntimeError, "xian-abci helpers"):
                abci_bridge.get_node_setup_module()


class NodeInitTests(unittest.TestCase):
    def test_node_init_materializes_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            (base_dir / "networks").mkdir()
            (base_dir / "nodes").mkdir()
            (base_dir / "keys" / "validator-1").mkdir(parents=True)

            genesis_source = LEGACY_GENESIS_DIR / "genesis-devnet.json"

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
            self.assertEqual(home, (base_dir / "xian-stack" / ".cometbft"))

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
                        str(base_dir / "networks" / "override-dev.json"),
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

            genesis_source = LEGACY_GENESIS_DIR / "genesis-devnet.json"

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
                        str(base_dir / "networks" / "local-dev.json"),
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

            genesis_source = LEGACY_GENESIS_DIR / "genesis-devnet.json"

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
                        str(base_dir / "networks" / "bad-chain.json"),
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
            (configs_dir / "networks").mkdir(parents=True)
            legacy_genesis_dir = configs_dir / "legacy" / "genesis"
            legacy_genesis_dir.mkdir(parents=True)

            genesis_payload = {
                "chain_id": "xian-canonical-1",
                "validators": [],
                "abci_genesis": {},
            }
            (legacy_genesis_dir / "genesis-canonical.json").write_text(
                json.dumps(genesis_payload),
                encoding="utf-8",
            )
            (configs_dir / "networks" / "canonical.json").write_text(
                json.dumps(
                    {
                        "name": "canonical",
                        "chain_id": "xian-canonical-1",
                        "mode": "join",
                        "runtime_backend": "xian-stack",
                        "genesis_source": (
                            "../legacy/genesis/genesis-canonical.json"
                        ),
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
                        str(base_dir / "networks" / "local-dev.json"),
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

    def test_start_xian_stack_node_runs_expected_make_targets(self) -> None:
        stack_dir = Path("/tmp/xian-stack")
        rpc_status = {"result": {"sync_info": {"catching_up": False}}}

        with patch("xian_cli.runtime.run_make_target") as run_make_target:
            with patch(
                "xian_cli.runtime.wait_for_rpc_ready",
                return_value=rpc_status,
            ) as wait_for_rpc:
                result = start_xian_stack_node(
                    stack_dir=stack_dir,
                    service_node=True,
                    wait_for_rpc=True,
                    rpc_timeout_seconds=12.5,
                )

        self.assertEqual(
            run_make_target.call_args_list,
            [
                call(stack_dir, "abci-bds-up"),
                call(stack_dir, "node-start-bds"),
            ],
        )
        wait_for_rpc.assert_called_once_with(timeout_seconds=12.5)
        self.assertEqual(result["rpc_status"], rpc_status)

    def test_stop_xian_stack_node_runs_expected_make_targets(self) -> None:
        stack_dir = Path("/tmp/xian-stack")

        with patch("xian_cli.runtime.run_make_target") as run_make_target:
            result = stop_xian_stack_node(
                stack_dir=stack_dir,
                service_node=False,
            )

        self.assertEqual(
            run_make_target.call_args_list,
            [
                call(stack_dir, "node-stop"),
                call(stack_dir, "abci-down"),
            ],
        )
        self.assertEqual(result["container_target"], "abci-down")


class ConfigRepoTests(unittest.TestCase):
    def test_resolve_configs_dir_uses_workspace_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            resolved = resolve_configs_dir(Path(tmp_dir))

        expected = (WORKSPACE_ROOT / "xian-configs").resolve()
        self.assertEqual(resolved, expected)

    def test_resolve_network_manifest_path_prefers_canonical_configs_repo(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            configs_dir = base_dir / "xian-configs"
            networks_dir = configs_dir / "networks"
            networks_dir.mkdir(parents=True)
            manifest_path = networks_dir / "mainnet.json"
            manifest_path.write_text("{}", encoding="utf-8")

            resolved = resolve_network_manifest_path(
                base_dir=base_dir,
                network_name="mainnet",
                configs_dir=configs_dir,
            )

        self.assertEqual(resolved, manifest_path.resolve())


if __name__ == "__main__":
    unittest.main()
