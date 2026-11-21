"""Snapshot consumers require positive proof that the managed runtime is stopped."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from snapshot_test_support import stopped_home_proof

from xian_cli.cli import main
from xian_cli.commands import node_context, recovery
from xian_cli.models import write_json


@pytest.fixture(autouse=True)
def no_live_global_docker_queries(monkeypatch):
    monkeypatch.setattr(node_context, "run_backend_command", stopped_home_proof)


def stopped_status(**overrides):
    return {
        "runtime_status_known": True,
        "runtime_services_running": False,
        "backend_running": False,
        "compose_services": [],
        **overrides,
    }


@pytest.mark.parametrize(
    "status",
    [
        {},
        None,
        stopped_status(runtime_status_known=False),
        stopped_status(compose_services=None),
        stopped_status(backend_running=True),
        stopped_status(runtime_services_running=True),
        stopped_status(compose_services=[{"service": "abci", "state": "running"}]),
        stopped_status(compose_services=[{"service": "postgres", "state": "restarting"}]),
        stopped_status(compose_services=[{"service": "abci", "state": "paused"}]),
        stopped_status(compose_services=[{"service": "abci", "state": "unknown"}]),
    ],
)
def test_restore_and_export_stop_check_fails_closed(tmp_path, monkeypatch, status):
    monkeypatch.setattr(node_context, "get_xian_stack_node_status", lambda **kw: status)
    with pytest.raises(RuntimeError, match="stop|status"):
        node_context._require_stopped_node(
            stack_dir=tmp_path, home=tmp_path / "home", profile={"name": "node"}, network={}
        )


@pytest.mark.parametrize(
    "services",
    [[], [{"service": "abci", "state": "exited"}], [{"service": "abci", "state": "created"}]],
)
def test_known_stopped_or_fresh_unstarted_home_can_restore_or_export(
    tmp_path, monkeypatch, services
):
    monkeypatch.setattr(
        node_context,
        "get_xian_stack_node_status",
        lambda **kw: stopped_status(compose_services=services),
    )
    node_context._require_stopped_node(
        stack_dir=tmp_path, home=tmp_path / "home", profile={"name": "node"}, network={}
    )


def test_recovery_skip_stop_still_requires_status_proof_before_restore(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / "config/config.toml").write_text("")
    write_json(
        tmp_path / "nodes/node.json",
        {
            "schema_version": 1,
            "name": "node",
            "network": "local",
            "moniker": "node",
            "home": str(home),
            "stack_dir": str(tmp_path),
        },
    )
    write_json(
        tmp_path / "networks/local/manifest.json",
        {
            "schema_version": 1,
            "name": "local",
            "chain_id": "local-1",
            "genesis": {"kind": "source", "source": "./genesis.json"},
        },
    )
    plan = tmp_path / "recovery.json"
    write_json(
        plan,
        {
            "schema_version": 1,
            "name": "recover",
            "chain_id": "local-1",
            "target_height": 1,
            "trusted_block_hash": "a" * 64,
            "trusted_app_hash": "b" * 64,
            "reason": "test",
            "artifact": {
                "kind": "snapshot_url",
                "uri": "https://snapshots.example/node.tar.gz",
                "sha256": "c" * 64,
            },
        },
    )
    monkeypatch.setattr(node_context, "get_xian_stack_node_status", lambda **kw: {})
    importer = Mock()
    monkeypatch.setattr(recovery, "get_node_admin_module", importer)
    with pytest.raises(RuntimeError, match="status"):
        main(
            [
                "recovery",
                "apply",
                str(plan),
                "node",
                "--base-dir",
                str(tmp_path),
                "--skip-stop",
                "--skip-backup",
                "--yes",
                "--rpc-url",
                "",
            ]
        )
    importer.assert_not_called()


def test_known_empty_compose_still_refuses_a_global_writable_home_mount(tmp_path, monkeypatch):
    monkeypatch.setattr(node_context, "get_xian_stack_node_status", lambda **kw: stopped_status())
    global_check = Mock(side_effect=RuntimeError("snapshot home overlaps an active writable mount"))
    monkeypatch.setattr(node_context, "run_backend_command", global_check)
    with pytest.raises(RuntimeError, match="writable"):
        node_context._require_stopped_node(
            stack_dir=tmp_path, home=tmp_path / "fresh", profile={"name": "node"}, network={}
        )
    assert global_check.call_args.args[1] == "node-store-snapshot-check-stopped"


@pytest.mark.parametrize(
    "proof",
    [{}, None, {"runtime_stopped": False}, {"runtime_stopped": True, "home": "/different-home"}],
)
def test_global_stop_proof_must_be_positive_and_bound_to_the_requested_home(
    tmp_path, monkeypatch, proof
):
    monkeypatch.setattr(node_context, "get_xian_stack_node_status", lambda **kw: stopped_status())
    monkeypatch.setattr(node_context, "run_backend_command", lambda *a, **kw: proof)
    with pytest.raises(RuntimeError, match="status|proof"):
        node_context._require_stopped_node(
            stack_dir=tmp_path, home=tmp_path / "fresh", profile={"name": "node"}, network={}
        )
