"""Invalid numeric profile inputs fail before node configuration begins."""

import json

import pytest

from xian_cli.models import read_node_profile

NUMERIC_OPTIONS = [
    ("services", "bds", "catchup_poll_seconds"),
    ("advanced", "metrics", "bds_refresh_seconds"),
    ("advanced", "pending_nonce", "reservation_ttl_seconds"),
    ("advanced", "parallel_execution", "min_wave_acceptance_ratio"),
]


def write_profile(tmp_path, option, value):
    section, component, name = option
    payload = {
        "schema_version": 1,
        "name": "validator",
        "network": "local",
        "moniker": "validator",
        section: {component: {name: value}},
    }
    path = tmp_path / "node.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize("option", NUMERIC_OPTIONS, ids=lambda option: ".".join(option))
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 10**400])
def test_profile_rejects_non_finite_numeric_options(tmp_path, option, value):
    with pytest.raises(ValueError, match="finite number"):
        read_node_profile(write_profile(tmp_path, option, value))


@pytest.mark.parametrize("option", NUMERIC_OPTIONS, ids=lambda option: ".".join(option))
@pytest.mark.parametrize("value", [True, False, "0.5", None, [], {}])
def test_profile_rejects_non_numeric_options(tmp_path, option, value):
    with pytest.raises(ValueError, match="must be a number"):
        read_node_profile(write_profile(tmp_path, option, value))


@pytest.mark.parametrize("option", NUMERIC_OPTIONS, ids=lambda option: ".".join(option))
@pytest.mark.parametrize("value", [1, 0.5])
def test_profile_preserves_valid_numeric_options(tmp_path, option, value):
    profile = read_node_profile(write_profile(tmp_path, option, value))
    section, component, name = option
    assert profile[section][component][name] == value
    assert isinstance(profile[section][component][name], float)
