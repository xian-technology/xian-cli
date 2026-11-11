"""Every node that joins one bundle network must materialize the same genesis."""

import io
import json
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

from xian_cli.cli import main

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
CONFIGS_DIR = WORKSPACE_ROOT / "xian-configs"


def run_cli(*argv: str) -> None:
    with redirect_stdout(io.StringIO()):
        assert main(list(argv)) == 0


def test_network_create_pins_bundle_genesis_time(tmp_path):
    run_cli("network", "create", "shared", "--base-dir", str(tmp_path), "--chain-id", "shared-1")

    manifest = json.loads((tmp_path / "networks/shared/manifest.json").read_text())

    genesis_time = manifest["genesis"]["genesis_time"]
    assert datetime.fromisoformat(genesis_time).utcoffset().total_seconds() == 0


def test_nodes_joining_a_created_bundle_network_share_one_genesis(tmp_path):
    run_cli("network", "create", "shared", "--base-dir", str(tmp_path), "--chain-id", "shared-1")
    for node in ("node-a", "node-b"):
        run_cli(
            "network",
            "join",
            node,
            "--base-dir",
            str(tmp_path),
            "--network",
            "shared",
            "--configs-dir",
            str(CONFIGS_DIR),
            "--generate-validator-key",
            "--home",
            str(tmp_path / node),
            "--init-node",
        )

    genesis_a = (tmp_path / "node-a/config/genesis.json").read_bytes()
    genesis_b = (tmp_path / "node-b/config/genesis.json").read_bytes()
    assert genesis_a == genesis_b
