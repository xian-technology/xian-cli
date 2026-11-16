"""Operator readiness describes intended history-aware artifacts, never attestation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from xian_cli.commands import node
from xian_cli.commands.common import _statesync_artifact_kwargs
from xian_cli.commands.doctor import _doctor_statesync_check
from xian_cli.commands.node_status import _release_summary
from xian_cli.models import read_network_manifest, read_node_profile, write_json
from xian_cli.state_sync_artifact import history_build_metadata

HISTORY_FIELDS = (
    "cometbft_source_commit",
    "cometbft_patch_path",
    "cometbft_patch_sha256",
    "cometbft_history_revision",
)


def history_release() -> dict:
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
            "cometbft_patch_path": "docker/cometbft-patches/0001-verified-evidence-history.patch",
            "cometbft_patch_sha256": "f" * 64,
            "cometbft_history_revision": "1",
            "s6_overlay_version": "3.2.3.0",
            "s6_overlay_noarch_sha256": "a" * 64,
            "s6_overlay_x86_64_sha256": "b" * 64,
            "s6_overlay_aarch64_sha256": "c" * 64,
        },
        "images": {"integrated": "xian-node", "split": "xian-node-split"},
    }


def profile(release=None) -> dict:
    return {
        "schema_version": 1,
        "name": "node",
        "network": "local",
        "moniker": "node",
        "node_image_mode": "registry",
        "node_integrated_image": "registry.example/xian-node@sha256:" + "a" * 64,
        "node_split_image": "registry.example/xian-node-split@sha256:" + "b" * 64,
        "node_release_manifest": release,
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


def test_network_and_profile_roundtrip_keep_history_identity(tmp_path):
    release = history_release()
    payloads = [
        (profile(release), read_node_profile),
        (
            {
                "schema_version": 1,
                "name": "local",
                "chain_id": "local-1",
                "genesis": {"kind": "source", "source": "./genesis.json"},
                "node_release_manifest": release,
            },
            read_network_manifest,
        ),
    ]
    for i, (payload, reader) in enumerate(payloads):
        path = tmp_path / f"{i}.json"
        write_json(path, payload)
        normalized = reader(path)
        for field in HISTORY_FIELDS:
            assert normalized["node_release_manifest"]["build"][field] == release["build"][field]
        assert (
            _release_summary(normalized["node_release_manifest"])["release_manifest_build"][
                "cometbft_patch_sha256"
            ]
            == release["build"]["cometbft_patch_sha256"]
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("cometbft_source_commit", "D" * 40),
        ("cometbft_source_commit", "d" * 39),
        ("cometbft_patch_sha256", "F" * 64),
        ("cometbft_patch_sha256", 1),
        ("cometbft_patch_path", "/tmp/evil.patch"),
        ("cometbft_patch_path", "docker/cometbft-patches/../evil.patch"),
        ("cometbft_history_revision", "2"),
        ("cometbft_history_revision", 1),
        ("cometbft_version", "0.39.3"),
        ("cometbft_source_url", "https://example.invalid/source.tar.gz"),
    ],
)
def test_profile_rejects_malformed_history_identity(tmp_path, field, value):
    release = history_release()
    release["build"][field] = value
    path = tmp_path / "profile.json"
    write_json(path, profile(release))
    with pytest.raises(ValueError):
        read_node_profile(path)


@pytest.mark.parametrize("missing", HISTORY_FIELDS)
def test_partial_history_metadata_is_rejected(tmp_path, missing):
    release = history_release()
    del release["build"][missing]
    path = tmp_path / "profile.json"
    write_json(path, profile(release))
    with pytest.raises(ValueError):
        read_node_profile(path)


def test_boolean_release_schema_is_not_integer_one(tmp_path):
    release = history_release()
    release["schema_version"] = True
    path = tmp_path / "profile.json"
    write_json(path, profile(release))
    with pytest.raises(ValueError):
        read_node_profile(path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("components", None),
        ("components", {}),
        ("components", []),
        ("components", {"": {"repository": "repo", "ref": "abcdef"}}),
        ("components", {"xian-abci": "bad"}),
        ("components", {"xian-abci": {"repository": "repo"}}),
        ("build", []),
        ("images", []),
    ],
)
def test_release_boundary_rejects_malformed_existing_sections(tmp_path, field, value):
    release = history_release()
    release[field] = value
    path = tmp_path / "profile.json"
    write_json(path, profile(release))
    with pytest.raises(ValueError):
        read_node_profile(path)


def test_legacy_manifest_still_loads_for_ordinary_replay(tmp_path):
    release = history_release()
    for key in HISTORY_FIELDS:
        del release["build"][key]
    release["build"]["cometbft_version"] = "0.39.3"
    path = tmp_path / "profile.json"
    write_json(path, profile(release))
    assert read_node_profile(path)["node_release_manifest"]["build"]["cometbft_version"] == "0.39.3"
    write_config(tmp_path / "home", enabled=False)
    assert node._collect_statesync_readiness(tmp_path / "home")["state"] == "disabled"


def test_enabled_trust_settings_alone_are_not_history_ready(tmp_path):
    write_config(tmp_path)
    readiness = node._collect_statesync_readiness(tmp_path)
    assert readiness["ready"] is False
    assert readiness["state"] == "unsupported_artifact"
    with pytest.raises(RuntimeError, match="history"):
        _doctor_statesync_check({"home": str(tmp_path)})


@pytest.mark.parametrize("enabled,has_metadata", [(False, False), (True, False), (True, True)])
def test_start_gates_only_state_sync_intended_artifact(
    tmp_path, monkeypatch, enabled, has_metadata
):
    home = tmp_path / "home"
    write_config(home, enabled=enabled)
    stack = tmp_path / "stack"
    stack.mkdir()
    p = profile(history_release() if has_metadata else None)
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
    if enabled and not has_metadata:
        with pytest.raises(ValueError, match="history"):
            node._handle_node_start(args)
        backend.assert_not_called()
    else:
        assert node._handle_node_start(args) == 0
        backend.assert_called_once()


@pytest.mark.parametrize(
    "manifest",
    [
        [],
        None,
        {},
        {"schema_version": True},
        {"schema_version": 1.0},
        {"schema_version": 2},
        {"schema_version": 1, "build": []},
        {"schema_version": 1, "build": {}},
    ],
)
def test_readiness_rejects_malformed_external_release_object(tmp_path, manifest):
    write_config(tmp_path)
    readiness = node._collect_statesync_readiness(tmp_path, release_manifest=manifest)
    assert readiness["ready"] is False
    assert readiness["history_bootstrap"]["configured"] is False
    assert readiness["history_bootstrap"]["metadata_only"] is True


@pytest.mark.parametrize(
    "field,value",
    [
        ("cometbft_source_sha256", "E" * 64),
        ("cometbft_source_sha256", None),
        ("cometbft_patch_sha256", "f" * 64 + "\n"),
    ],
)
def test_raw_intended_build_identity_is_strict(field, value):
    release = history_release()
    release["build"][field] = value
    with pytest.raises(ValueError, match="history"):
        history_build_metadata(release)


def test_registry_network_metadata_reaches_doctor_via_status(tmp_path):
    write_config(tmp_path)
    release = history_release()
    p = profile()
    status = node._initial_node_status(
        {
            "profile_path": tmp_path / "p",
            "profile": p,
            "network_path": tmp_path / "n",
            "network": {"node_release_manifest": release},
            "stack_dir": tmp_path / "stack",
            "home": tmp_path,
        },
        name="node",
        check_rpc=False,
    )
    readiness = _doctor_statesync_check(status)
    assert readiness["ready"] is True
    assert readiness["history_bootstrap"] == {
        "configured": True,
        "metadata_only": True,
        "build": history_build_metadata(release),
    }


def test_local_build_uses_selected_stack_intended_metadata(tmp_path):
    write_config(tmp_path)
    stack = tmp_path / "stack"
    stack.mkdir()
    write_json(stack / "release-manifest.json", history_release())
    p = profile()
    p["node_image_mode"] = "local_build"
    readiness = node._collect_statesync_readiness(
        tmp_path, **_statesync_artifact_kwargs(p, stack_dir=stack)
    )
    assert readiness["ready"] is True
    assert readiness["history_bootstrap"]["metadata_only"] is True
    assert (
        _doctor_statesync_check({"home": str(tmp_path), "profile": p, "stack_dir": str(stack)})[
            "ready"
        ]
        is True
    )
    # A registry profile cannot borrow an unrelated local build's metadata.
    assert (
        node._collect_statesync_readiness(
            tmp_path, **_statesync_artifact_kwargs(profile(), stack_dir=stack)
        )["ready"]
        is False
    )


@pytest.mark.parametrize("content", [None, "{bad-json", "[]"])
def test_local_build_missing_or_invalid_manifest_is_not_ready(tmp_path, content):
    write_config(tmp_path)
    path = tmp_path / "release-manifest.json"
    if content is not None:
        path.write_text(content)
    readiness = node._collect_statesync_readiness(tmp_path, local_manifest_path=path)
    assert readiness["state"] == "unsupported_artifact"
    assert readiness["ready"] is False
    assert readiness["history_bootstrap"]["error"]


def test_disabled_state_sync_does_not_require_local_manifest(tmp_path):
    write_config(tmp_path, enabled=False)
    readiness = node._collect_statesync_readiness(
        tmp_path, local_manifest_path=tmp_path / "missing.json"
    )
    assert readiness["state"] == "disabled"
    assert "history_bootstrap" not in readiness


def test_incomplete_trust_is_not_hidden_by_supported_artifact(tmp_path):
    write_config(tmp_path)
    path = tmp_path / "config" / "config.toml"
    path.write_text(path.read_text().replace("trust_height = 42", "trust_height = 0"))
    readiness = node._collect_statesync_readiness(tmp_path, release_manifest=history_release())
    assert readiness["state"] == "incomplete"
    assert readiness["ready"] is False
    with pytest.raises(RuntimeError, match="incomplete trust"):
        _doctor_statesync_check({"home": str(tmp_path), "profile": profile(history_release())})
