"""How `xian setup node` turns flags and wizard answers into a setup plan."""

import io
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import pytest

from xian_cli.commands.setup import _resolve_plan
from xian_cli.parser import build_parser

CONFIGS_DIR = Path(__file__).resolve().parents[2] / "xian-configs"


class TtyInput(io.StringIO):
    def isatty(self) -> bool:
        return True


class PipeInput(io.StringIO):
    def isatty(self) -> bool:
        return False


def plan_for(tmp_path: Path, *flags: str, answers: str | None = None):
    args = build_parser().parse_args(
        [
            "setup",
            "node",
            "--base-dir",
            str(tmp_path),
            "--configs-dir",
            str(CONFIGS_DIR),
            *flags,
        ]
    )
    stdin = PipeInput("") if answers is None else TtyInput(answers)
    with patch("sys.stdin", stdin), redirect_stderr(io.StringIO()):
        return _resolve_plan(args)


def test_non_interactive_join_defaults(tmp_path):
    plan = plan_for(tmp_path, "--plan")

    assert (plan.mode, plan.network, plan.name, plan.chain_id) == (
        "join",
        "testnet",
        "validator-1",
        None,
    )
    assert plan.template == "single-node-indexed"
    assert plan.key_mode == "generate"
    assert plan.validator_key_ref is None
    assert plan.restore_snapshot is False
    assert plan.start is False
    assert plan.base_dir == tmp_path.resolve()


def test_non_interactive_local_defaults(tmp_path):
    plan = plan_for(tmp_path, "--plan", "--mode", "local", "--network", "My Lab")

    assert plan.mode == "local"
    assert plan.network == "My Lab"
    assert plan.chain_id == "xian-my-lab-1"
    assert plan.template == "single-node-dev"
    assert plan.restore_snapshot is False


@pytest.mark.parametrize(
    ("flags", "restore"),
    [
        (["--restore-snapshot"], True),
        (["--bootstrap-mode", "snapshot"], True),
        (["--bootstrap-mode", "genesis"], False),
    ],
)
def test_join_snapshot_restore_follows_the_flags(tmp_path, flags, restore):
    assert plan_for(tmp_path, "--plan", *flags).restore_snapshot is restore


def test_explicit_flags_win(tmp_path):
    plan = plan_for(
        tmp_path,
        "--yes",
        "--mode",
        "local",
        "--network",
        "lab",
        "--name",
        "node-a",
        "--chain-id",
        "lab-9",
        "--preset",
        "indexed",
        "--template",
        "single-node-dev",
        "--validator-key-ref",
        "keys/a.json",
        "--start",
    )

    assert (plan.network, plan.name, plan.chain_id) == ("lab", "node-a", "lab-9")
    assert plan.template == "single-node-dev"
    assert plan.key_mode == "existing"
    assert plan.validator_key_ref == Path("keys/a.json")
    assert plan.start is True


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        ([], "requires --yes or --plan in non-interactive mode"),
        (
            ["--plan", "--mode", "local", "--network-manifest", "m.json"],
            "--network-manifest is only valid when --mode join",
        ),
        (
            ["--plan", "--mode", "local", "--restore-snapshot"],
            "snapshot restore is only valid when --mode join",
        ),
        (
            ["--plan", "--mode", "local", "--bootstrap-mode", "snapshot"],
            "snapshot restore is only valid when --mode join",
        ),
        (
            ["--plan", "--validator-selection-mode", "manual"],
            "--validator-selection-mode is only valid with --mode local",
        ),
        (
            [
                "--plan",
                "--mode",
                "local",
                "--validator-selection-mode",
                "manual",
                "--genesis-source",
                "./g.json",
            ],
            "--validator-selection-mode only applies when generating a local genesis",
        ),
        (
            ["--plan", "--restore-snapshot", "--bootstrap-mode", "genesis"],
            "pass either --bootstrap-mode genesis or --restore-snapshot, not both",
        ),
        (
            ["--plan", "--key-mode", "existing"],
            "--key-mode existing requires --validator-key-ref",
        ),
        (
            ["--plan", "--tx-fee-mode", "paid_metered"],
            "tx_fee_mode choose the chain fee policy at genesis; use them only with --mode local",
        ),
    ],
)
def test_inconsistent_setup_flags_are_rejected(tmp_path, flags, message):
    with pytest.raises(ValueError, match=message):
        plan_for(tmp_path, *flags)


def test_wizard_defaults_join_and_starts(tmp_path):
    plan = plan_for(tmp_path, answers="")

    assert (plan.mode, plan.network, plan.name) == ("join", "testnet", "validator-1")
    assert plan.template == "single-node-indexed"
    assert plan.key_mode == "generate"
    assert plan.restore_snapshot is False
    assert plan.start is True


def test_wizard_answers_shape_a_local_plan(tmp_path):
    plan = plan_for(tmp_path, answers="2\nlab\nnode-a\n\n2\nexisting\n\nn\n")

    assert (plan.mode, plan.network, plan.name) == ("local", "lab", "node-a")
    assert plan.chain_id == "xian-lab-1"
    assert plan.template == "single-node-indexed"
    assert plan.key_mode == "existing"
    assert plan.validator_key_ref == Path("./keys/node-a/validator_key_info.json")
    assert plan.start is False


def test_wizard_can_choose_a_snapshot_join(tmp_path):
    plan = plan_for(tmp_path, answers="join\n\n\nbasic\n\ny\n")

    assert plan.mode == "join"
    assert plan.template == "single-node-dev"
    assert plan.restore_snapshot is True
    assert plan.start is True
