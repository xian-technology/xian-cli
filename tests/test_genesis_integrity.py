"""A network pins the SHA-256 of its genesis; nodes refuse any other genesis.

``genesis_sha256`` is the SHA-256 of the exact ``config/genesis.json`` bytes
``node init`` installs, so an operator can check a running home with
``sha256sum <home>/config/genesis.json``.
"""

import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from xian_cli.abci_bridge import get_node_setup_module
from xian_cli.cli import main
from xian_cli.models import normalize_network_manifest

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
CONFIGS_DIR = WORKSPACE_ROOT / "xian-configs"
GenesisIntegrityError = get_node_setup_module().GenesisIntegrityError


def run_cli(*argv: str) -> dict:
    with redirect_stdout(io.StringIO()) as stdout:
        assert main(list(argv)) == 0
    return json.loads(stdout.getvalue())


def installed_sha256(home: Path) -> str:
    return hashlib.sha256((home / "config/genesis.json").read_bytes()).hexdigest()


def create_network(base: Path, *extra: str) -> dict:
    run_cli(
        "network",
        "create",
        "shared",
        "--base-dir",
        str(base),
        "--chain-id",
        "shared-1",
        "--configs-dir",
        str(CONFIGS_DIR),
        *extra,
    )
    return json.loads((base / "networks/shared/manifest.json").read_text())


def join(base: Path, node: str, *extra: str) -> None:
    run_cli(
        "network",
        "join",
        node,
        "--base-dir",
        str(base),
        "--network",
        "shared",
        "--configs-dir",
        str(CONFIGS_DIR),
        "--generate-validator-key",
        "--home",
        str(base / node),
        "--init-node",
        *extra,
    )


def test_manifest_pin_must_be_a_sha256():
    manifest = {"schema_version": 1, "name": "n", "chain_id": "n-1"}
    genesis = {"genesis": {"kind": "source", "source": "./genesis.json"}}

    assert normalize_network_manifest(manifest | genesis)["genesis_sha256"] is None
    pinned = normalize_network_manifest(manifest | genesis | {"genesis_sha256": "ab" * 32})
    assert pinned["genesis_sha256"] == "ab" * 32
    with pytest.raises(ValueError, match="genesis_sha256 must be a 64-character"):
        normalize_network_manifest(manifest | genesis | {"genesis_sha256": "abc"})


@pytest.mark.parametrize(
    "creation",
    [(), ("--generate-validator-key", "--validator", "founder")],
    ids=["bundle", "generated"],
)
def test_created_network_pins_the_genesis_every_joining_node_installs(tmp_path, creation):
    manifest = create_network(tmp_path, *creation)

    join(tmp_path, "node-a")

    assert manifest["genesis_sha256"] == installed_sha256(tmp_path / "node-a")


def test_node_refuses_a_genesis_that_does_not_match_the_pin(tmp_path):
    create_network(tmp_path, "--generate-validator-key", "--validator", "founder")
    genesis_path = tmp_path / "networks/shared/genesis.json"
    genesis = json.loads(genesis_path.read_text())
    genesis["genesis_time"] = "2030-01-01T00:00:00.000000Z"
    genesis_path.write_text(json.dumps(genesis, indent=2))

    with pytest.raises(GenesisIntegrityError, match="genesis_sha256"):
        join(tmp_path, "node-a")

    assert not (tmp_path / "node-a/config/genesis.json").exists()


def test_operator_bundle_ships_the_pin_and_participants_install_that_genesis(tmp_path):
    coordinator = tmp_path / "coordinator"
    manifest = create_network(coordinator, "--generate-validator-key", "--validator", "founder")
    bundle = tmp_path / "bundle"

    run_cli(
        "network",
        "package-operator-bundle",
        "shared",
        "--base-dir",
        str(coordinator),
        "--output",
        str(bundle),
        "--bootstrap-seed",
        "abc@localhost:26656",
    )

    bundled = json.loads((bundle / "networks/shared/manifest.json").read_text())
    index = json.loads((bundle / "operator-bundle.json").read_text())
    assert bundled["genesis_sha256"] == manifest["genesis_sha256"]
    assert index["genesis_sha256"] == manifest["genesis_sha256"]
    assert manifest["genesis_sha256"] in (bundle / "README.md").read_text()
    join(bundle, "participant")
    assert installed_sha256(bundle / "participant") == manifest["genesis_sha256"]


def test_operator_bundle_pins_an_unpinned_genesis_it_ships(tmp_path):
    manifest = create_network(tmp_path, "--generate-validator-key", "--validator", "founder")
    manifest_path = tmp_path / "networks/shared/manifest.json"
    pinned = manifest.pop("genesis_sha256")
    manifest_path.write_text(json.dumps(manifest))
    bundle = tmp_path / "bundle"

    run_cli(
        "network",
        "package-operator-bundle",
        "shared",
        "--base-dir",
        str(tmp_path),
        "--output",
        str(bundle),
    )

    assert json.loads((bundle / "operator-bundle.json").read_text())["genesis_sha256"] == pinned


def test_operator_bundle_refuses_a_genesis_that_does_not_match_the_pin(tmp_path):
    create_network(tmp_path, "--generate-validator-key", "--validator", "founder")
    genesis_path = tmp_path / "networks/shared/genesis.json"
    genesis = json.loads(genesis_path.read_text())
    genesis["genesis_time"] = "2030-01-01T00:00:00.000000Z"
    genesis_path.write_text(json.dumps(genesis))
    bundle = tmp_path / "bundle"

    with pytest.raises(GenesisIntegrityError, match="genesis_sha256"):
        main(
            [
                "network",
                "package-operator-bundle",
                "shared",
                "--base-dir",
                str(tmp_path),
                "--output",
                str(bundle),
            ]
        )

    assert not (bundle / "networks/shared/genesis.json").exists()
