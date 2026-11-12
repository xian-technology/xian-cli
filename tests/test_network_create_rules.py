"""Argument rules and outputs of `xian network create`, pinned before restructuring it."""

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from xian_cli.cli import main


def create(tmp_path: Path, *extra: str) -> dict:
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        exit_code = main(
            [
                "network",
                "create",
                "local-dev",
                "--base-dir",
                str(tmp_path),
                "--chain-id",
                "xian-local-1",
                *extra,
            ]
        )
    assert exit_code == 0
    return json.loads(stdout.getvalue())


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (
            ["--generate-validator-key", "--validator-key-ref", "keys/v.json"],
            "pass either --validator-key-ref or --generate-validator-key, not both",
        ),
        (["--validator-key-dir", "keys"], "--validator-key-dir requires --generate-validator-key"),
        (
            ["--validator-selection-mode", "manual", "--genesis-source", "./genesis.json"],
            "--validator-selection-mode only applies when generating a local genesis",
        ),
        (["--init-node"], "--init-node requires --bootstrap-node"),
        (
            ["--validator-selection-mode", "manual"],
            "--validator-selection-mode requires generated local genesis",
        ),
        (
            ["--validator-selection-mode", "manual", "--bootstrap-node", "validator-1"],
            "--validator-selection-mode requires generated local genesis",
        ),
        (
            ["--bootstrap-node", "validator-1"],
            "local network creation without --genesis-source requires validator key material",
        ),
        (
            ["--bootstrap-node", "validator-1", "--genesis-source", "./genesis.json"],
            "--bootstrap-node requires validator key material",
        ),
        (
            ["--validator", "validator-2", "--bootstrap-node", "validator-1"],
            "multi-validator network creation currently requires --generate-validator-key",
        ),
    ],
)
def test_network_create_rejects_inconsistent_arguments(tmp_path, extra, message):
    with pytest.raises(ValueError, match=message):
        create(tmp_path, *extra)


def test_dry_run_reports_the_validator_policy_and_fee_policy_without_writing(tmp_path):
    plan = create(
        tmp_path,
        "--bootstrap-node",
        "validator-1",
        "--generate-validator-key",
        "--validator-selection-mode",
        "auto_top_n",
        "--dry-run",
    )

    assert plan["validator_policy"] == {"selection_mode": "auto_top_n"}
    assert plan["fee_policy"] == {"tx_fee_mode": "paid_metered"}
    assert plan["genesis"] == {"kind": "bundle", "bundle": "local"}
    assert plan["bootstrap_node"] == "validator-1"
    assert plan["generate_validator_key"] is True
    assert plan["init_node"] is False
    assert not (tmp_path / "keys").exists()
    assert not (tmp_path / "networks").exists()


def test_dry_run_for_an_external_genesis_has_no_fee_policy(tmp_path):
    plan = create(tmp_path, "--genesis-source", "./genesis.json", "--dry-run")

    assert plan["genesis"] == {"kind": "source", "source": "./genesis.json"}
    assert "fee_policy" not in plan
    assert "validator_policy" not in plan


def test_generated_genesis_records_how_it_was_built(tmp_path):
    result = create(
        tmp_path,
        "--bootstrap-node",
        "validator-1",
        "--validator",
        "validator-2",
        "--generate-validator-key",
        "--validator-selection-mode",
        "hybrid",
    )

    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert result["genesis"] == {"kind": "source", "source": "./genesis.json"}
    assert result["generated_genesis_path"] == str(
        Path(result["manifest_path"]).parent / "genesis.json"
    )
    assert result["validator_policy"] == {"selection_mode": "hybrid"}
    assert manifest["genesis_build"] == {
        "bundle": "local",
        "generated_by": "xian network create",
        "validator_policy": {"selection_mode": "hybrid"},
    }
    assert manifest["fee_policy"] is None
    assert [v["name"] for v in result["validators"]] == ["validator-1", "validator-2"]
    assert result["profile_path"] == result["validators"][0]["profile_path"]
    assert result["validator_key_ref"] == result["validators"][0]["validator_key_ref"]
    bootstrap = json.loads(Path(result["validators"][0]["profile_path"]).read_text())
    joiner = json.loads(Path(result["validators"][1]["profile_path"]).read_text())
    assert bootstrap["network"] == joiner["network"] == "local-dev"
    assert "node_initialized" not in result


def test_bundle_network_without_validators_pins_a_genesis_time(tmp_path):
    result = create(tmp_path)

    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert result["genesis"]["kind"] == "bundle"
    assert result["genesis"]["genesis_time"].endswith("Z")
    assert manifest["genesis"] == result["genesis"]
    assert manifest["fee_policy"] == {"tx_fee_mode": "paid_metered"}
    assert manifest["genesis_build"] is None
    assert result["validators"] == []
    assert "profile_path" not in result
    assert "generated_genesis_path" not in result
