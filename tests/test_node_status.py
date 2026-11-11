"""Pin the status JSON contract before separating its projections."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from xian_cli.cli import main
from xian_cli.commands import node

BASELINE = json.loads((Path(__file__).parent / "fixtures/node_status_baseline.json").read_text())


@pytest.mark.parametrize("name", BASELINE["inputs"])
def test_status_summary_preserves_complete_output(name):
    with patch("xian_cli.commands.node._block_age_seconds", return_value=7.25):
        summary = node._summarize_node_status(BASELINE["inputs"][name])
    assert summary == BASELINE["outputs"][name]


def test_cli_import_keeps_node_helpers_lazy():
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import xian_cli.cli; assert 'xian.node_setup' not in sys.modules",
        ],
        check=True,
    )


@pytest.mark.parametrize("backend_fails", [False, True])
def test_status_reports_collection_failures_and_recovers_invalid_node_key(
    tmp_path, capsys, backend_fails
):
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    for name in ("config.toml", "xian.toml"):
        (home / "config" / name).write_text("")
    (home / "config/node_key.json").write_text("invalid json")
    context = {
        "profile_path": tmp_path / "nodes/node.json",
        "profile": {},
        "network_path": tmp_path / "networks/local.json",
        "network": {},
        "stack_dir": tmp_path / "stack",
        "home": home,
    }
    backend = {"backend_running": True, "node_id": "backend-node"}
    with (
        patch("xian_cli.commands.node._resolve_node_context", return_value=context),
        patch(
            "xian_cli.commands.node.get_xian_stack_node_status",
            return_value=backend,
            side_effect=RuntimeError("backend unavailable") if backend_fails else None,
        ),
        patch("xian_cli.commands.node.fetch_json", side_effect=OSError("RPC unavailable")),
    ):
        assert main(["node", "status", "node"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["backend_checked"] is True
    assert result["rpc_checked"] is True
    assert result["rpc_reachable"] is False
    assert result["rpc_error"] == "RPC unavailable"
    if backend_fails:
        assert result["node_id"] is None
        assert result["backend_error"] == "backend unavailable"
        assert result["summary"]["state"] == "stopped"
    else:
        assert result["node_id"] == "backend-node"
        assert result["backend_running"] is True
        assert result["summary"]["state"] == "rpc_unreachable"


@pytest.mark.parametrize(
    "config_files", [(), ("config.toml",), ("xian.toml",), ("config.toml", "xian.toml")]
)
def test_status_requires_both_configs_and_skips_unconfigured_backend(
    tmp_path, capsys, config_files
):
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    for name in config_files:
        (home / "config" / name).write_text("")
    context = {
        "profile_path": tmp_path / "nodes/node.json",
        "profile": {},
        "network_path": tmp_path / "networks/local.json",
        "network": {},
        "stack_dir": None,
        "home": home,
    }
    with (
        patch("xian_cli.commands.node._resolve_node_context", return_value=context),
        patch("xian_cli.commands.node.get_xian_stack_node_status") as backend,
        patch("xian_cli.commands.node.fetch_json") as fetch,
    ):
        assert main(["node", "status", "node", "--skip-rpc"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["initialized"] is (len(config_files) == 2)
    assert "backend_checked" not in result
    assert result["rpc_checked"] is False
    backend.assert_not_called()
    fetch.assert_not_called()
