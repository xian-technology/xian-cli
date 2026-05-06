from __future__ import annotations

import io
import json
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from xian_cli.cli import main


class GoldenSetupPlanTests(unittest.TestCase):
    def test_network_create_init_renders_from_profile_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            home = base_dir / "node-home"
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "network",
                        "create",
                        "golden-local",
                        "--base-dir",
                        str(base_dir),
                        "--chain-id",
                        "xian-golden-1",
                        "--bootstrap-node",
                        "node-0",
                        "--generate-validator-key",
                        "--init-node",
                        "--home",
                        str(home),
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            manifest_path = Path(payload["manifest_path"])
            profile_path = Path(payload["profile_path"])

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            cometbft_config = tomllib.loads(
                (home / "config" / "config.toml").read_text(encoding="utf-8")
            )
            xian_config = tomllib.loads(
                (home / "config" / "xian.toml").read_text(encoding="utf-8")
            )

            self.assertEqual(manifest["chain_id"], "xian-golden-1")
            self.assertEqual(profile["network"], "golden-local")
            self.assertEqual(profile["runtime_backend"], "xian-stack")
            self.assertEqual(cometbft_config["moniker"], "node-0")
            self.assertEqual(cometbft_config["p2p"]["seeds"], "")
            self.assertEqual(xian_config["tracer_mode"], "python_line_v1")
            self.assertEqual(xian_config["metrics_host"], "0.0.0.0")
            self.assertTrue((home / "config" / "genesis.json").exists())


if __name__ == "__main__":
    unittest.main()
