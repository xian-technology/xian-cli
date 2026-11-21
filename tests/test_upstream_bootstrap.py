"""Stock CometBFT supports replay and full-store restore, not P2P app state sync."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from xian_cli.commands import node
from xian_cli.commands.doctor import _doctor_cometbft_config_check
from xian_cli.commands.node_status import _release_summary
from xian_cli.models import read_network_manifest, read_node_profile, write_json


def release() -> dict:
    return {
        "schema_version": 1,
        "components": {"xian-abci": {"repository": "xian-technology/xian-abci", "ref": "a" * 40}},
        "build": {
            "python_image": "python@sha256:" + "b" * 64,
            "go_image": "golang@sha256:" + "c" * 64,
            "cometbft_version": "0.40.0",
            "cometbft_source_url": "https://codeload.github.com/cometbft/cometbft/tar.gz/"
            + "d" * 40,
            "cometbft_source_sha256": "e" * 64,
            "cometbft_source_commit": "d" * 40,
            "s6_overlay_version": "3.2.3.0",
            "s6_overlay_noarch_sha256": "a" * 64,
            "s6_overlay_x86_64_sha256": "b" * 64,
            "s6_overlay_aarch64_sha256": "c" * 64,
        },
        "images": {"integrated": "xian-node"},
    }


def profile(manifest=None) -> dict:
    return {
        "schema_version": 1,
        "name": "node",
        "network": "local",
        "moniker": "node",
        "node_image_mode": "registry",
        "node_integrated_image": "registry.example/xian-node@sha256:" + "a" * 64,
        "node_release_manifest": manifest,
    }


def write_config(home: Path, *, enabled=True):
    directory = home / "config"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.toml").write_text(
        "[statesync]\n"
        + f"enable = {str(enabled).lower()}\n"
        + 'rpc_servers = "http://one:26657,http://two:26657"\n'
        + 'trust_height = 42\ntrust_hash = "'
        + "ab" * 32
        + '"\ntrust_period = "168h0m0s"\n'
    )
    (directory / "xian.toml").write_text("")


def test_upstream_release_roundtrip_and_status_preserve_source_identity(tmp_path):
    manifest = release()
    # Removed custom-fork fields must not survive normalization or status.
    for field in ("cometbft_patch_path", "cometbft_patch_sha256", "cometbft_history_revision"):
        manifest["build"][field] = "obsolete"
    payloads = [
        (profile(manifest), read_node_profile),
        (
            {
                "schema_version": 1,
                "name": "local",
                "chain_id": "local-1",
                "genesis": {"kind": "source", "source": "./genesis.json"},
                "node_release_manifest": manifest,
            },
            read_network_manifest,
        ),
    ]
    for index, (payload, reader) in enumerate(payloads):
        path = tmp_path / f"{index}.json"
        write_json(path, payload)
        normalized = reader(path)["node_release_manifest"]
        build = normalized["build"]
        assert build["cometbft_source_commit"] == "d" * 40
        assert build["cometbft_source_sha256"] == "e" * 64
        assert not any("patch" in field or "history" in field for field in build)
        assert _release_summary(normalized)["release_manifest_build"] == build


@pytest.mark.parametrize("commit", ["D" * 40, "d" * 39, 1])
def test_malformed_upstream_source_commit_is_rejected(tmp_path, commit):
    manifest = release()
    manifest["build"]["cometbft_source_commit"] = commit
    write_json(tmp_path / "p.json", profile(manifest))
    with pytest.raises(ValueError, match="source_commit"):
        read_node_profile(tmp_path / "p.json")


def test_ordinary_legacy_provenance_and_disabled_config_remain_usable(tmp_path):
    manifest = release()
    del manifest["build"]["cometbft_source_commit"]
    manifest["build"]["cometbft_version"] = "0.39.3"
    write_json(tmp_path / "p.json", profile(manifest))
    assert (
        read_node_profile(tmp_path / "p.json")["node_release_manifest"]["build"]["cometbft_version"]
        == "0.39.3"
    )
    write_config(tmp_path, enabled=False)
    assert _doctor_cometbft_config_check({"home": str(tmp_path)}) == {
        "path": str(tmp_path / "config/config.toml")
    }


def test_disabled_legacy_profile_settings_are_dropped(tmp_path):
    p = profile()
    p["advanced"] = {"statesync": {"enabled": False, "trust_height": 42}}
    path = tmp_path / "p.json"
    write_json(path, p)
    assert "statesync" not in read_node_profile(path)["advanced"]


def test_enabled_rendered_state_sync_refuses_even_complete_trust_settings(tmp_path):
    write_config(tmp_path)
    with pytest.raises(ValueError, match="replay.*full-node-store"):
        _doctor_cometbft_config_check({"home": str(tmp_path)})


@pytest.mark.parametrize("enabled", [False, True, 0])
def test_start_refuses_state_sync_before_backend_even_when_skipping_health(
    tmp_path, monkeypatch, enabled
):
    home = tmp_path / "home"
    write_config(home, enabled=enabled)
    stack = tmp_path / "stack"
    stack.mkdir()
    p = profile()
    p["home"] = str(home)
    monkeypatch.setattr(
        node, "_load_profile_and_network", lambda **kw: (tmp_path / "p", p, tmp_path / "n", {})
    )
    backend = Mock(return_value={"ok": True})
    monkeypatch.setattr(node, "start_xian_stack_node", backend)
    args = SimpleNamespace(
        base_dir=tmp_path,
        name="node",
        profile=None,
        network=None,
        configs_dir=None,
        stack_dir=stack,
        skip_health_check=True,
        rpc_timeout_seconds=2,
    )
    if enabled is not False:
        message = "full-node-store" if enabled is True else "boolean"
        with pytest.raises(ValueError, match=message):
            node._handle_node_start(args)
        backend.assert_not_called()
    else:
        assert node._handle_node_start(args) == 0
        backend.assert_called_once()


def test_node_health_returns_failure_for_enabled_state_sync(tmp_path, monkeypatch, capsys):
    write_config(tmp_path)
    write_json(tmp_path / "p", profile())
    context = {
        "profile_path": tmp_path / "p",
        "profile": profile(),
        "network_path": tmp_path / "n",
        "network": {},
        "stack_dir": None,
        "home": tmp_path,
    }
    monkeypatch.setattr(node, "_resolve_node_context", lambda args: context)
    args = SimpleNamespace(rpc_url="http://127.0.0.1:26657", skip_disk_check=True)
    assert node._handle_node_health(args) == 1
    result = json.loads(capsys.readouterr().out)
    assert "statesync" not in result
    assert result["profile_config"]["ok"] is True
    assert result["cometbft_config"]["ok"] is False
    assert "replay" in result["cometbft_config"]["error"]


@pytest.mark.parametrize(
    "text", ["[statesync]\nenable = 0\n", 'statesync = "invalid"\n', "broken = [\n"]
)
def test_invalid_cometbft_config_fails_health_and_doctor(tmp_path, monkeypatch, capsys, text):
    write_config(tmp_path)
    write_json(tmp_path / "p", profile())
    (tmp_path / "config/config.toml").write_text(text)
    context = {
        "profile_path": tmp_path / "p",
        "profile": profile(),
        "network_path": tmp_path / "n",
        "network": {},
        "stack_dir": None,
        "home": tmp_path,
    }
    monkeypatch.setattr(node, "_resolve_node_context", lambda args: context)
    assert (
        node._handle_node_health(
            SimpleNamespace(rpc_url="http://127.0.0.1:26657", skip_disk_check=True)
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["cometbft_config"]["error"]
    with pytest.raises(ValueError):
        _doctor_cometbft_config_check({"home": str(tmp_path)})


@pytest.mark.parametrize("legacy", [{"enabled": True}, {"enabled": 0}, [], None])
def test_unsupported_profile_can_still_be_recovered(tmp_path, monkeypatch, capsys, legacy):
    p = profile()
    p["advanced"] = {"statesync": legacy}
    p["home"] = str(tmp_path / "home")
    write_config(Path(p["home"]), enabled=False)
    write_json(tmp_path / "nodes/node.json", p)
    write_json(
        tmp_path / "networks/local/manifest.json",
        {
            "schema_version": 1,
            "name": "local",
            "chain_id": "local-1",
            "genesis": {"kind": "source", "source": "./genesis.json"},
        },
    )
    common_args = ["node", "--base-dir", str(tmp_path), "--stack-dir", str(tmp_path)]
    backend = Mock(return_value={"ok": True})
    monkeypatch.setattr(node, "stop_xian_stack_node", backend)
    monkeypatch.setattr(node, "get_xian_stack_node_status", Mock(return_value={}))
    monkeypatch.setattr(node, "get_xian_stack_node_endpoints", Mock(return_value={}))
    monkeypatch.setattr(node, "_require_stopped_node", Mock())
    export = Mock(return_value={})
    monkeypatch.setattr(node, "run_backend_command", export)
    restore = Mock(return_value={})
    monkeypatch.setattr(node, "_restore_snapshot", restore)
    from xian_cli.cli import main

    assert main(["node", "stop", *common_args]) == 0
    backend.assert_called_once()
    assert main(["node", "status", *common_args, "--skip-rpc"]) == 0
    assert main(["node", "endpoints", *common_args]) == 0
    assert main(["snapshot", "restore", *common_args]) == 0
    restore.assert_called_once()
    assert (
        main(
            [
                "snapshot",
                "export",
                *common_args,
                "--archive-output",
                str(tmp_path / "snapshot.tar.gz"),
                "--manifest-output",
                str(tmp_path / "snapshot.json"),
                "--snapshot-url",
                "https://example.invalid/snapshot.tar.gz",
                "--signing-key-file",
                str(tmp_path / "publisher-key"),
            ]
        )
        == 0
    )
    export.assert_called_once()
    with pytest.raises(ValueError, match="P2P application state sync|advanced.statesync"):
        main(["node", "start", *common_args])

    monkeypatch.setattr(node, "get_xian_stack_node_health", Mock(return_value={}))
    capsys.readouterr()
    assert main(["node", "health", *common_args]) == 1
    health = json.loads(capsys.readouterr().out)
    assert health["profile_config"]["ok"] is False
    assert health["cometbft_config"]["ok"] is True

    assert main(["doctor", *common_args, "--skip-live-checks"]) == 1
    checks = {check["name"]: check for check in json.loads(capsys.readouterr().out)["checks"]}
    assert checks["profile_config"]["ok"] is False
    assert checks["cometbft_config"]["ok"] is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("components", {}),
        ("components", []),
        ("components", {"": {"repository": "repo", "ref": "abcdef"}}),
        ("components", {"xian-abci": "bad"}),
        ("build", []),
        ("images", []),
    ],
)
def test_release_boundary_keeps_existing_validation(tmp_path, field, value):
    manifest = release()
    manifest[field] = value
    write_json(tmp_path / "p.json", profile(manifest))
    with pytest.raises(ValueError):
        read_node_profile(tmp_path / "p.json")
