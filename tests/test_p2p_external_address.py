import pytest
import xian.node_setup as node_setup
from xian.node_setup import render_cometbft_config

from xian_cli.commands import node_options
from xian_cli.models import NodeProfile, normalize_node_profile


def _profile(**cometbft):
    payload = NodeProfile("node", "local", "node").to_dict()
    payload["advanced"]["cometbft"].update(cometbft)
    return normalize_node_profile(payload)


def _rendered_external_address(profile) -> str:
    options = node_options.node_config_options(node_setup, profile=profile, network={})
    return render_cometbft_config(options=options)["p2p"]["external_address"]


def test_profile_external_address_is_advertised_to_peers():
    profile = _profile(external_address="203.0.113.10:26656")
    assert profile["advanced"]["cometbft"]["external_address"] == "203.0.113.10:26656"
    assert _rendered_external_address(profile) == "203.0.113.10:26656"


def test_profile_without_external_address_leaves_it_unset():
    profile = _profile()
    assert profile["advanced"]["cometbft"]["external_address"] == ""
    assert _rendered_external_address(profile) == ""


def test_external_address_must_be_text():
    with pytest.raises(ValueError, match="external_address"):
        _profile(external_address=26656)
