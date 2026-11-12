"""The fee policy is chain state chosen at genesis, never node-local config."""

import io
import json
import tomllib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from xian_cli.cli import main

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
CONFIGS_DIR = WORKSPACE_ROOT / "xian-configs"
FEE_POLICY_KEY = "__fee_policy"
FREE_POLICY = {
    "tx_fee_mode": "free_metered",
    "max_tx_chi": 150000,
    "max_block_chi": 400000,
}
FREE_FLAGS = (
    "--tx-fee-mode",
    "free_metered",
    "--max-tx-chi",
    "150000",
    "--max-block-chi",
    "400000",
)
# Every chain fee mode carries hard chi caps; these are the genesis defaults.
DEFAULT_POLICY = {"tx_fee_mode": "paid_metered", "max_tx_chi": 200000, "max_block_chi": 500000}
FEE_KEYS = {"tx_fee_mode", "max_tx_chi", "max_block_chi"}


def run_cli(*argv: str) -> dict | None:
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        assert main(list(argv)) == 0
    try:
        return json.loads(stdout.getvalue())
    except json.JSONDecodeError:
        return None


def genesis_fee_policy(path: Path):
    genesis = json.loads(path.read_text())
    entries = {item["key"]: item["value"] for item in genesis["abci_genesis"]["genesis"]}
    return entries.get(FEE_POLICY_KEY)


def manifest(base_dir: Path, name: str = "shared") -> dict:
    return json.loads((base_dir / "networks" / name / "manifest.json").read_text())


def create(base_dir: Path, *extra: str) -> None:
    run_cli(
        "network",
        "create",
        "shared",
        "--base-dir",
        str(base_dir),
        "--chain-id",
        "shared-1",
        "--configs-dir",
        str(CONFIGS_DIR),
        *extra,
    )


def join(base_dir: Path, node: str = "node-a") -> Path:
    home = base_dir / node
    run_cli(
        "network",
        "join",
        node,
        "--base-dir",
        str(base_dir),
        "--network",
        "shared",
        "--configs-dir",
        str(CONFIGS_DIR),
        "--generate-validator-key",
        "--home",
        str(home),
        "--init-node",
    )
    return home


def test_bundle_network_records_the_fee_policy_and_genesis_carries_it(tmp_path):
    create(tmp_path, *FREE_FLAGS)

    assert manifest(tmp_path)["fee_policy"] == FREE_POLICY
    home = join(tmp_path)
    assert genesis_fee_policy(home / "config/genesis.json") == FREE_POLICY
    xian_config = tomllib.loads((home / "config/xian.toml").read_text())
    assert not FEE_KEYS & set(xian_config)


def test_default_bundle_network_is_paid_metered(tmp_path):
    create(tmp_path)

    assert manifest(tmp_path)["fee_policy"] == DEFAULT_POLICY
    home = join(tmp_path)
    assert genesis_fee_policy(home / "config/genesis.json") == DEFAULT_POLICY


def test_generated_local_genesis_carries_the_fee_policy(tmp_path):
    create(tmp_path, "--generate-validator-key", "--bootstrap-node", "validator-1", *FREE_FLAGS)

    generated = tmp_path / "networks/shared/genesis.json"
    assert genesis_fee_policy(generated) == FREE_POLICY
    # The generated genesis file is the single source of the policy.
    assert manifest(tmp_path)["genesis"] == {"kind": "source", "source": "./genesis.json"}
    assert manifest(tmp_path)["fee_policy"] is None


def test_paid_networks_carry_the_chosen_chi_caps(tmp_path):
    create(tmp_path, "--max-tx-chi", "100000")

    expected = DEFAULT_POLICY | {"max_tx_chi": 100000}
    assert manifest(tmp_path)["fee_policy"] == expected
    home = join(tmp_path)
    assert genesis_fee_policy(home / "config/genesis.json") == expected


@pytest.mark.parametrize(
    ("flags", "message"),
    (
        (("--max-tx-chi", "600000"), "max_block_chi must be greater than or equal to max_tx_chi"),
        (("--max-block-chi", "10000001"), "max_block_chi must not exceed 10000000 chi"),
        (("--max-tx-chi", "0"), "max_tx_chi must be an integer greater than zero"),
    ),
)
def test_invalid_chi_caps_are_rejected_when_the_network_is_created(tmp_path, flags, message):
    with pytest.raises(ValueError, match=message):
        create(tmp_path, *flags)
    assert not (tmp_path / "networks/shared/manifest.json").exists()


def test_fee_policy_cannot_be_applied_to_an_external_genesis(tmp_path):
    genesis = tmp_path / "external.json"
    genesis.write_text("{}")
    with pytest.raises(ValueError, match="genesis"):
        create(tmp_path, "--genesis-source", str(genesis), "--tx-fee-mode", "free_metered")


def test_joining_node_cannot_choose_the_fee_policy(tmp_path):
    create(tmp_path)
    with pytest.raises(SystemExit), redirect_stderr(io.StringIO()):
        main(
            [
                "network",
                "join",
                "node-a",
                "--base-dir",
                str(tmp_path),
                "--network",
                "shared",
                "--tx-fee-mode",
                "free_metered",
            ]
        )


def test_profiles_with_node_local_fee_settings_are_rejected(tmp_path):
    create(tmp_path)
    run_cli(
        "network",
        "join",
        "node-a",
        "--base-dir",
        str(tmp_path),
        "--network",
        "shared",
        "--generate-validator-key",
        "--home",
        str(tmp_path / "node-a-home"),
    )
    profile_path = tmp_path / "nodes/node-a.json"
    profile = json.loads(profile_path.read_text())
    profile["tx_fee_mode"] = "free_metered"
    profile_path.write_text(json.dumps(profile))

    with pytest.raises(ValueError, match="fee_policy"):
        main(["node", "init", "node-a", "--base-dir", str(tmp_path)])


def test_source_genesis_manifest_rejects_a_fee_policy(tmp_path):
    create(tmp_path, "--generate-validator-key", "--bootstrap-node", "validator-1")
    path = tmp_path / "networks/shared/manifest.json"
    payload = json.loads(path.read_text())
    payload["fee_policy"] = {"tx_fee_mode": "free_metered"}
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="fee_policy"):
        main(["node", "init", "validator-1", "--base-dir", str(tmp_path)])


def test_operator_bundle_materializes_the_policy_into_its_genesis(tmp_path):
    create(tmp_path, *FREE_FLAGS)
    output = tmp_path / "bundle"
    run_cli(
        "network",
        "package-operator-bundle",
        "shared",
        "--base-dir",
        str(tmp_path),
        "--configs-dir",
        str(CONFIGS_DIR),
        "--output",
        str(output),
    )

    bundled = json.loads((output / "manifest.json").read_text())
    assert bundled["genesis"] == {"kind": "source", "source": "./genesis.json"}
    assert bundled["fee_policy"] is None
    assert genesis_fee_policy(output / "genesis.json") == FREE_POLICY


def test_setup_node_join_mode_rejects_fee_policy_flags(tmp_path):
    with pytest.raises(ValueError, match="--mode local"):
        main(
            [
                "setup",
                "node",
                "--mode",
                "join",
                "--network",
                "testnet",
                "--name",
                "node-a",
                "--key-mode",
                "generate",
                "--base-dir",
                str(tmp_path),
                "--no-start",
                "--plan",
                "--tx-fee-mode",
                "free_metered",
            ]
        )


def test_setup_node_local_mode_puts_the_fee_policy_into_network_creation(tmp_path):
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        main(
            [
                "setup",
                "node",
                "--mode",
                "local",
                "--network",
                "devnet-local",
                "--name",
                "node-a",
                "--key-mode",
                "generate",
                "--base-dir",
                str(tmp_path),
                "--no-start",
                "--plan",
                *FREE_FLAGS,
            ]
        )
    create_command = json.loads(stdout.getvalue())["steps"][0]["command"]
    assert create_command[:3] == ["xian", "network", "create"]
    for flag in FREE_FLAGS:
        assert flag in create_command


def test_template_fee_policy_seeds_a_new_bundle_network(tmp_path):
    templates = tmp_path / "configs" / "templates"
    templates.mkdir(parents=True)
    (templates / "free-dev.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "free-dev",
                "display_name": "Free development",
                "description": "metered without fees",
                **FREE_POLICY,
            }
        )
    )
    run_cli(
        "network",
        "create",
        "shared",
        "--base-dir",
        str(tmp_path),
        "--chain-id",
        "shared-1",
        "--configs-dir",
        str(tmp_path / "configs"),
        "--template",
        "free-dev",
        "--max-tx-chi",
        "300000",
    )

    # Each field: the flag wins, then the template, then the chain default.
    assert manifest(tmp_path)["fee_policy"] == FREE_POLICY | {"max_tx_chi": 300000}


def test_template_caps_apply_to_paid_networks_too(tmp_path):
    templates = tmp_path / "configs" / "templates"
    templates.mkdir(parents=True)
    (templates / "tight.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "tight",
                "display_name": "Tight caps",
                "description": "paid with small caps",
                "max_block_chi": 300000,
            }
        )
    )
    run_cli(
        "network",
        "create",
        "shared",
        "--base-dir",
        str(tmp_path),
        "--chain-id",
        "shared-1",
        "--configs-dir",
        str(tmp_path / "configs"),
        "--template",
        "tight",
    )

    assert manifest(tmp_path)["fee_policy"] == DEFAULT_POLICY | {"max_block_chi": 300000}


@pytest.mark.parametrize(
    ("fee_policy", "message"),
    (
        ({"tx_fee_mode": "free"}, "tx_fee_mode must be one of"),
        (
            {"tx_fee_mode": "free_metered", "free_tx_max_chi": 1},
            "unknown fee policy settings: free_tx_max_chi",
        ),
        ({"tx_fee_mode": "paid_metered", "max_block_chi": 10_000_001}, "must not exceed"),
    ),
)
def test_bundle_manifest_fee_policy_follows_the_node_rules(tmp_path, fee_policy, message):
    create(tmp_path)
    path = tmp_path / "networks/shared/manifest.json"
    payload = json.loads(path.read_text())
    payload["fee_policy"] = fee_policy
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match=message):
        join(tmp_path)
