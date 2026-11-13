import pytest
import xian.node_setup as node_setup
from xian.node_setup import render_xian_config

from xian_cli.commands import node_options
from xian_cli.models import (
    NetworkTemplate,
    NodeProfile,
    normalize_network_template,
    normalize_node_profile,
)


def test_profile_without_simulation_cap_leaves_the_chain_cap_in_charge():
    profile = normalize_node_profile(NodeProfile("node", "local", "node").to_dict())
    assert profile["simulation_max_chi"] is None
    options = node_options.node_config_options(node_setup, profile=profile, network={})
    assert options.simulation.max_chi is None
    assert "simulation_max_chi" not in render_xian_config(options=options)


def test_template_without_simulation_cap_has_none():
    template = normalize_network_template(NetworkTemplate("t", "T", "Test").to_dict())
    assert template["simulation_max_chi"] is None


@pytest.mark.parametrize("value", [0, -1, True])
def test_simulation_cap_must_be_a_positive_integer(value):
    payload = NodeProfile("node", "local", "node").to_dict()
    payload["simulation_max_chi"] = value
    with pytest.raises(ValueError, match="simulation_max_chi"):
        normalize_node_profile(payload)
