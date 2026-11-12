"""advanced.metrics.port is the published host port; the node always listens on 9108."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from xian_cli.commands.common import _stack_runtime_profile_kwargs
from xian_cli.commands.node import _fallback_node_endpoints
from xian_cli.commands.node_options import APP_METRICS_CONTAINER_PORT, metrics_options
from xian_cli.runtime import run_backend_command, start_xian_stack_node

PROFILE = {"advanced": {"metrics": {"host": "0.0.0.0", "port": 39108}}}


class _MetricsOptions:
    def __init__(self, **values: object) -> None:
        self.values = values


def test_node_listens_on_the_container_port_whatever_the_profile_says() -> None:
    setup = type("Setup", (), {"MetricsOptions": _MetricsOptions})

    options = metrics_options(setup, PROFILE)

    assert APP_METRICS_CONTAINER_PORT == 9108
    assert options.values["port"] == APP_METRICS_CONTAINER_PORT
    assert options.values["host"] == "0.0.0.0"


def test_stack_runtime_publishes_the_profile_port() -> None:
    assert _stack_runtime_profile_kwargs(PROFILE)["app_metrics_port"] == 39108
    assert _stack_runtime_profile_kwargs({})["app_metrics_port"] == 9108


def test_backend_receives_the_published_port_as_the_stack_variable() -> None:
    def run(cmd, **kwargs):
        assert kwargs["env"]["XIAN_APP_METRICS_PORT"] == "39108"
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"ok": True}), stderr="")

    with patch("xian_cli.runtime.subprocess.run", side_effect=run) as runner:
        assert run_backend_command(Path("/tmp/stack"), "endpoints", app_metrics_port=39108) == {
            "ok": True
        }
    runner.assert_called_once()


def test_backend_keeps_the_stack_default_without_a_port() -> None:
    def run(cmd, **kwargs):
        assert "XIAN_APP_METRICS_PORT" not in kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    with (
        patch.dict("os.environ", {}, clear=True),
        patch("xian_cli.runtime.subprocess.run", side_effect=run),
    ):
        run_backend_command(Path("/tmp/stack"), "status")


def test_start_forwards_the_published_port() -> None:
    with patch("xian_cli.runtime.run_backend_command") as backend:
        start_xian_stack_node(
            stack_dir=Path("/tmp/stack"), bds_enabled=False, app_metrics_port=39108
        )

    assert backend.call_args.kwargs["app_metrics_port"] == 39108


def test_fallback_endpoints_report_the_published_port() -> None:
    endpoints = _fallback_node_endpoints(
        rpc_status_url="http://127.0.0.1:26657/status", profile=PROFILE
    )

    assert endpoints["xian_metrics"] == "http://127.0.0.1:39108/metrics"
