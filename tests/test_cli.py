from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

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

    def test_network_join_writes_node_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "validator-1.json"
            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "network",
                        "join",
                        "validator-1",
                        "--chain-id",
                        "xian-mainnet-1",
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
            self.assertEqual(profile["moniker"], "validator-1")
            self.assertEqual(profile["seeds"], ["abc@127.0.0.1:26656"])
            self.assertTrue(profile["service_node"])


if __name__ == "__main__":
    unittest.main()
