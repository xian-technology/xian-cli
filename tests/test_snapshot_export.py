"""Public full-store snapshot commands dispatch without handling private key bytes."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from snapshot_test_support import stopped_home_proof

from xian_cli import runtime
from xian_cli.cli import build_parser, main
from xian_cli.commands import node, node_context


@pytest.fixture(autouse=True)
def no_live_global_docker_queries(monkeypatch):
    monkeypatch.setattr(node_context, "run_backend_command", stopped_home_proof)


def test_snapshot_backend_options_use_structured_request(tmp_path, monkeypatch):
    backend = Mock(return_value=SimpleNamespace(stdout='{"ok": true}'))
    monkeypatch.setattr(runtime.subprocess, "run", backend)
    monkeypatch.setattr(runtime, "_backend_script", lambda path: path / "backend.py")
    options = {
        "home": str(tmp_path / "home"),
        "signing_key_file": str(tmp_path / "key"),
        "offline": True,
    }
    assert runtime.run_backend_command(
        tmp_path, "node-store-snapshot-export", snapshot_options=options
    ) == {"ok": True}
    request = json.loads(backend.call_args.kwargs["input"])
    assert request["command"] == "node-store-snapshot-export"
    assert all(request["options"][key] == value for key, value in options.items())
    assert set(request["options"]) == set(options)
    assert backend.call_args.args[0][-2:] == ["--request-json", "-"]


@pytest.mark.parametrize("running", [False, True])
def test_public_snapshot_export_checks_stop_then_dispatches_files_only(
    tmp_path, monkeypatch, capsys, running
):
    context = {
        "profile": {"name": "node"},
        "network": {},
        "stack_dir": tmp_path,
        "home": tmp_path / "home",
    }
    monkeypatch.setattr(node, "_resolve_node_context", lambda args: context)
    monkeypatch.setattr(
        node_context,
        "get_xian_stack_node_status",
        lambda **kw: {
            "runtime_status_known": True,
            "backend_running": running,
            "runtime_services_running": False,
            "compose_services": [],
        },
    )
    backend = Mock(return_value={"ok": True, "manifest_output": "public.json"})
    monkeypatch.setattr(node, "run_backend_command", backend, raising=False)
    key = tmp_path / "key"
    key.write_text("secret-key-bytes-never-in-request")
    args = [
        "snapshot",
        "export",
        "node",
        "--archive-output",
        str(tmp_path / "node.tar.gz"),
        "--manifest-output",
        str(tmp_path / "manifest.json"),
        "--snapshot-url",
        "https://snapshots.example/node.tar.gz",
        "--signing-key-file",
        str(key),
    ]
    if running:
        with pytest.raises(RuntimeError, match="stop"):
            main(args)
        backend.assert_not_called()
    else:
        assert main(args) == 0
        assert backend.call_args.kwargs["snapshot_options"] == {
            "home": str(tmp_path / "home"),
            "archive_output": str(tmp_path / "node.tar.gz"),
            "manifest_output": str(tmp_path / "manifest.json"),
            "snapshot_url": "https://snapshots.example/node.tar.gz",
            "signing_key_file": str(key),
            "offline": True,
        }
        assert backend.call_args.args[1] == "node-store-snapshot-export"
        assert "secret-key-bytes" not in capsys.readouterr().out


def test_restore_native_inspector_uses_the_selected_image_and_recipient_genesis(
    tmp_path, monkeypatch
):
    backend = Mock(return_value={"schema_version": 1, "checkpoint": "public"})
    monkeypatch.setattr(node_context, "run_backend_command", backend)
    profile = {
        "node_image_mode": "registry",
        "node_integrated_image": "image@sha256:" + "a" * 64,
        "node_split_image": "split@sha256:" + "b" * 64,
    }
    inspector = node_context._snapshot_store_inspector(
        stack_dir=tmp_path, home=tmp_path / "recipient", profile=profile, network={}
    )
    result = inspector(
        tmp_path / "staged/data", "goleveldb", tmp_path / "recipient/config/genesis.json"
    )
    assert result == {"schema_version": 1, "checkpoint": "public"}
    assert backend.call_args.args == (tmp_path, "node-store-snapshot-inspect")
    assert backend.call_args.kwargs["snapshot_options"] == {
        "db_dir": str(tmp_path / "staged/data"),
        "db_backend": "goleveldb",
        "genesis_file": str(tmp_path / "recipient/config/genesis.json"),
    }
    assert backend.call_args.kwargs["node_integrated_image"] == profile["node_integrated_image"]


@pytest.mark.parametrize("action", ["export", "inspect", "check-stopped"])
def test_current_stack_parser_accepts_actual_cli_snapshot_requests(tmp_path, monkeypatch, action):
    stack = Path(__file__).resolve().parents[2] / "xian-stack"
    monkeypatch.syspath_prepend(str(stack / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "snapshot_stack_contract", stack / "scripts/backend.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    capture = Mock(return_value=SimpleNamespace(stdout='{"ok": true}'))
    monkeypatch.setattr(runtime.subprocess, "run", capture)
    options = (
        {
            "home": str(tmp_path / "home"),
            "archive_output": str(tmp_path / "node.tar.gz"),
            "manifest_output": str(tmp_path / "manifest.json"),
            "snapshot_url": "https://snapshots.example/node.tar.gz",
            "signing_key_file": str(tmp_path / "key"),
            "offline": True,
        }
        if action == "export"
        else {
            "db_dir": str(tmp_path / "staged/data"),
            "db_backend": "goleveldb",
            "genesis_file": str(tmp_path / "home/config/genesis.json"),
        }
        if action == "inspect"
        else {"home": str(tmp_path / "fresh")}
    )
    runtime.run_backend_command(
        stack,
        "node-store-snapshot-" + action,
        node_image_mode="registry",
        node_integrated_image="reviewed-image",
        node_split_image="reviewed-split-image",
        snapshot_options=options,
    )
    request = json.loads(capture.call_args.kwargs["input"])
    parsed = module.args_from_backend_request(module.build_parser(), request)
    assert parsed.node_image_mode == "registry"
    assert parsed.node_integrated_image == "reviewed-image"
    assert parsed.command == request["command"]
    for key, value in options.items():
        assert str(getattr(parsed, key)) == str(value)


@pytest.mark.parametrize(
    "missing", ["archive-output", "manifest-output", "snapshot-url", "signing-key-file"]
)
def test_snapshot_export_requires_each_publication_input_before_context_loading(tmp_path, missing):
    values = {
        "archive-output": str(tmp_path / "node.tar.gz"),
        "manifest-output": str(tmp_path / "manifest.json"),
        "snapshot-url": "https://snapshots.example/node.tar.gz",
        "signing-key-file": str(tmp_path / "key"),
    }
    args = ["snapshot", "export", "node", "--base-dir", str(tmp_path)]
    for name, value in values.items():
        if name != missing:
            args.extend(["--" + name, value])
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2


def test_snapshot_parser_rejects_a_missing_subcommand():
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["snapshot"])
    assert exc.value.code == 2
