import hashlib
import json
from pathlib import Path

import pytest

from xian_cli.cli import main


def test_convert_reconcile_and_build_without_changing_address(tmp_path, capsys):
    address = "AB" * 32
    source = tmp_path / "legacy.json"
    source.write_text(
        json.dumps(
            [
                {"key": "currency.balances:" + address, "value": '"3E+0"'},
                {"key": "currency.balances:" + address + ":spender", "value": '"1000000"'},
            ]
        )
    )
    inventory = tmp_path / "contracts.json"
    inventory.write_text('["currency"]')
    snapshot = tmp_path / "snapshot.json"
    assert (
        main(
            [
                "network",
                "convert-legacy-balances",
                "--contract-inventory",
                str(inventory),
                "--state",
                str(source),
                "--output",
                str(snapshot),
                "--chain-id",
                "old-local",
                "--height",
                "123",
                "--block-hash",
                "ab" * 32,
                "--total-supply",
                "3",
                "--scope",
                "complete",
            ]
        )
        == 0
    )
    conversion = json.loads(capsys.readouterr().out)
    assert len(conversion["excluded"]) == 1
    assert conversion["source_consensus_verified"] is False
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "schema": "xian.balance_migration.v1",
                "source": {
                    "chain_id": "old-local",
                    "height": 123,
                    "block_hash": "ab" * 32,
                    "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                },
                "target_chain_id": "new-local",
                "contract_dispositions": {},
            }
        )
    )
    bundle = tmp_path / "bundle"
    configs = Path(__file__).resolve().parents[2] / "xian-configs" / "contracts"
    assert (
        main(
            [
                "network",
                "migrate-balances",
                "--snapshot",
                str(snapshot),
                "--plan",
                str(plan),
                "--contracts-dir",
                str(configs),
                "--output-dir",
                str(bundle),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["launch_approved"] is False
    output = tmp_path / "genesis.json"
    command = [
        "network",
        "build-genesis",
        "--contracts-dir",
        str(bundle),
        "--bundle",
        "mainnet",
        "--chain-id",
        "new-local",
        "--genesis-time",
        "2026-01-01T00:00:00Z",
        "--output",
        str(output),
    ]
    assert main(command) == 0
    state = {
        row["key"]: row["value"]
        for row in json.loads(output.read_text())["abci_genesis"]["genesis"]
    }
    assert state["_accounts.policy"]["enrollment_open"] is False
    assert "currency.balances:" + address in state
    assert not any(key.startswith("currency.approvals:") for key in state)
    original = output.read_bytes()
    with pytest.raises(ValueError, match="must not exist"):
        main(command)
    assert output.read_bytes() == original
