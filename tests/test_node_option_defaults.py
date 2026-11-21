"""The operator bridge inherits backend defaults without eager runtime imports."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from xian_cli.abci_bridge import get_node_setup_module
from xian_cli.commands import node_options


@pytest.mark.parametrize(
    "function,kind,changes",
    [
        ("metrics_options", "MetricsOptions", {"bds_refresh_seconds": 8.0}),
        ("app_logging_options", "AppLoggingOptions", {"rotation_hours": 6}),
        ("simulation_options", "SimulationOptions", {"timeout_ms": 4567}),
        ("bds_options", "BdsOptions", {"pool_max_size": 23}),
    ],
)
def test_missing_profile_settings_inherit_backend_defaults(function, kind, changes):
    cls = getattr(get_node_setup_module(), kind)
    defaults = replace(cls(), **changes)
    setup = SimpleNamespace(**{kind: lambda **values: replace(defaults, **values)})

    options = getattr(node_options, function)(setup, {})

    if kind == "MetricsOptions":
        defaults = replace(
            defaults,
            host=node_options.APP_METRICS_CONTAINER_HOST,
            port=node_options.APP_METRICS_CONTAINER_PORT,
        )
    assert options == defaults
