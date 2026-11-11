"""Snapshot restore replaces node data, so it must never run under a live node."""

import io
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from xian_cli.cli import main

SNAPSHOT_URL = "https://example.invalid/snapshot-manifest.json"


def run_cli(*argv: str) -> None:
    with redirect_stdout(io.StringIO()):
        assert main(list(argv)) == 0


@pytest.fixture
def initialized_node(tmp_path):
    stack_dir = tmp_path / "stack"
    stack_dir.mkdir()
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "config.toml").write_text("", encoding="utf-8")
    run_cli(
        "network",
        "create",
        "local-dev",
        "--base-dir",
        str(tmp_path),
        "--chain-id",
        "local-dev-1",
        "--snapshot-url",
        SNAPSHOT_URL,
        "--snapshot-signing-key",
        "b" * 64,
    )
    run_cli(
        "network",
        "join",
        "validator-1",
        "--base-dir",
        str(tmp_path),
        "--network",
        "local-dev",
        "--home",
        str(home),
        "--stack-dir",
        str(stack_dir),
    )
    return SimpleNamespace(base_dir=tmp_path, stack_dir=stack_dir, home=home)


def restore(node, status):
    apply_snapshot = Mock(return_value="snapshot.tar.gz")
    node_status = Mock(return_value=status)
    with (
        patch(
            "xian_cli.commands.node_context.get_node_admin_module",
            return_value=SimpleNamespace(apply_snapshot_archive=apply_snapshot),
        ),
        patch("xian_cli.commands.node_context.get_xian_stack_node_status", node_status),
        redirect_stdout(io.StringIO()),
    ):
        try:
            main(["snapshot", "restore", "validator-1", "--base-dir", str(node.base_dir)])
        finally:
            node.apply_snapshot = apply_snapshot
            node.node_status = node_status


@pytest.mark.parametrize(
    "status",
    [
        {"runtime_services_running": True, "backend_running": True},
        # A crash-looping container still restarts into the replaced data.
        {"runtime_services_running": True, "backend_running": False},
        {"runtime_services_running": False, "backend_running": True},
    ],
)
def test_snapshot_restore_refuses_a_running_node(initialized_node, status):
    with pytest.raises(RuntimeError, match="xian node stop validator-1"):
        restore(initialized_node, status)

    initialized_node.apply_snapshot.assert_not_called()
    call = initialized_node.node_status.call_args.kwargs
    assert call["stack_dir"] == initialized_node.stack_dir.resolve()
    assert call["cometbft_home"] == initialized_node.home


def test_snapshot_restore_proceeds_once_the_node_is_stopped(initialized_node):
    restore(initialized_node, {"runtime_services_running": False, "backend_running": False})

    initialized_node.apply_snapshot.assert_called_once_with(
        SNAPSHOT_URL,
        initialized_node.home,
        trusted_manifest_public_keys=["b" * 64],
        expected_chain_id="local-dev-1",
    )


def test_snapshot_restore_checks_the_default_stack_when_the_profile_has_none(tmp_path):
    (tmp_path / "xian-stack").mkdir()
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "config.toml").write_text("", encoding="utf-8")
    run_cli(
        "network", "create", "local-dev", "--base-dir", str(tmp_path), "--chain-id", "local-dev-1"
    )
    run_cli(
        "network",
        "join",
        "validator-1",
        "--base-dir",
        str(tmp_path),
        "--network",
        "local-dev",
        "--home",
        str(home),
        "--snapshot-url",
        SNAPSHOT_URL,
        "--snapshot-signing-key",
        "b" * 64,
    )
    node = SimpleNamespace(base_dir=tmp_path)

    with pytest.raises(RuntimeError, match="is running"):
        restore(node, {"runtime_services_running": True})

    assert node.node_status.call_args.kwargs["stack_dir"] == (tmp_path / "xian-stack").resolve()
