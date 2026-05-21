from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from xian_cli.setup_contract import BackendRequest, drop_none

DEFAULT_RPC_TIMEOUT_SECONDS = 90.0

BACKEND_OPTION_KEYS = (
    ("node_image_mode", "node_image_mode"),
    ("node_integrated_image", "node_integrated_image"),
    ("node_split_image", "node_split_image"),
    ("bds_enabled", "bds_enabled"),
    ("dashboard_enabled", "dashboard"),
    ("monitoring_enabled", "monitoring"),
    ("dashboard_host", "dashboard_host"),
    ("dashboard_port", "dashboard_port"),
    ("intentkit_enabled", "intentkit"),
    ("intentkit_network_id", "intentkit_network_id"),
    ("intentkit_host", "intentkit_host"),
    ("intentkit_port", "intentkit_port"),
    ("intentkit_api_port", "intentkit_api_port"),
    ("dex_automation_enabled", "dex_automation"),
    ("dex_automation_host", "dex_automation_host"),
    ("dex_automation_port", "dex_automation_port"),
    ("dex_automation_config", "dex_automation_config"),
    ("shielded_relayer_enabled", "shielded_relayer"),
    ("shielded_relayer_host", "shielded_relayer_host"),
    ("shielded_relayer_port", "shielded_relayer_port"),
    ("wait_for_health", "wait_for_health"),
    ("rpc_timeout_seconds", "rpc_timeout_seconds"),
    ("rpc_url", "rpc_url"),
    ("check_disk", "check_disk"),
)

RUNTIME_COMMAND_KWARG_NAMES = (
    "cometbft_home",
    "node_image_mode",
    "node_integrated_image",
    "node_split_image",
    "bds_enabled",
    "dashboard_enabled",
    "monitoring_enabled",
    "dashboard_host",
    "dashboard_port",
    "intentkit_enabled",
    "intentkit_network_id",
    "intentkit_host",
    "intentkit_port",
    "intentkit_api_port",
    "dex_automation_enabled",
    "dex_automation_host",
    "dex_automation_port",
    "dex_automation_config",
    "shielded_relayer_enabled",
    "shielded_relayer_host",
    "shielded_relayer_port",
)


def _backend_options(values: Mapping[str, Any]) -> dict[str, Any]:
    return drop_none(
        {
            option_key: values.get(argument_name)
            for argument_name, option_key in BACKEND_OPTION_KEYS
        }
    )


def _runtime_command_kwargs(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        name: values[name]
        for name in RUNTIME_COMMAND_KWARG_NAMES
        if name in values
    }


def resolve_stack_dir(base_dir: Path, explicit: Path | None = None) -> Path:
    if explicit is not None:
        resolved = explicit.expanduser().resolve()
        if not resolved.exists():
            raise FileNotFoundError(
                f"xian-stack directory does not exist: {resolved}"
            )
        if not resolved.is_dir():
            raise NotADirectoryError(
                f"xian-stack path is not a directory: {resolved}"
            )
        return resolved

    candidate = (base_dir / "xian-stack").resolve()
    if candidate.exists():
        return candidate

    workspace_candidate = Path(__file__).resolve().parents[3] / "xian-stack"
    if workspace_candidate.exists():
        return workspace_candidate.resolve()

    raise FileNotFoundError(
        "unable to resolve xian-stack directory; "
        "pass --stack-dir or set stack_dir in the node profile"
    )


def default_home_for_backend(
    *, base_dir: Path, stack_dir: Path | None = None
) -> Path:
    resolved_stack_dir = resolve_stack_dir(base_dir, explicit=stack_dir)
    return resolved_stack_dir / ".cometbft"


def fetch_json(url: str, *, timeout: float = 10.0) -> dict:
    with urlopen(url, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset)
    return json.loads(payload)


def wait_for_rpc_ready(
    *,
    rpc_url: str = "http://127.0.0.1:26657/status",
    timeout_seconds: float = DEFAULT_RPC_TIMEOUT_SECONDS,
    poll_interval: float = 1.0,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            payload = fetch_json(rpc_url, timeout=poll_interval)
            if payload.get("result"):
                return payload
        except (
            OSError,
            URLError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            last_error = exc

        time.sleep(poll_interval)

    raise TimeoutError(f"RPC did not become ready at {rpc_url}") from last_error


def _backend_script(stack_dir: Path) -> Path:
    return stack_dir / "scripts" / "backend.py"


def run_backend_command(
    stack_dir: Path,
    command: str,
    *,
    cometbft_home: Path | None = None,
    node_image_mode: str | None = None,
    node_integrated_image: str | None = None,
    node_split_image: str | None = None,
    bds_enabled: bool = False,
    dashboard_enabled: bool = False,
    monitoring_enabled: bool = False,
    dashboard_host: str | None = None,
    dashboard_port: int | None = None,
    intentkit_enabled: bool = False,
    intentkit_network_id: str | None = None,
    intentkit_host: str | None = None,
    intentkit_port: int | None = None,
    intentkit_api_port: int | None = None,
    dex_automation_enabled: bool = False,
    dex_automation_host: str | None = None,
    dex_automation_port: int | None = None,
    dex_automation_config: str | None = None,
    shielded_relayer_enabled: bool = False,
    shielded_relayer_host: str | None = None,
    shielded_relayer_port: int | None = None,
    wait_for_health: bool | None = None,
    rpc_timeout_seconds: float | None = None,
    rpc_url: str | None = None,
    check_disk: bool | None = None,
) -> dict:
    request = BackendRequest(
        command=command,
        options=_backend_options(locals()),
    )
    cmd = [
        sys.executable,
        str(_backend_script(stack_dir)),
        "--request-json",
        "-",
    ]

    try:
        result = subprocess.run(
            cmd,
            cwd=stack_dir,
            check=True,
            capture_output=True,
            text=True,
            input=json.dumps(request.to_dict()),
            env=(
                {
                    **os.environ,
                    **(
                        {"XIAN_COMETBFT_HOME": str(cometbft_home)}
                        if cometbft_home is not None
                        else {}
                    ),
                }
            ),
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise RuntimeError(
            f"xian-stack backend command failed ({command}): {detail}"
        ) from exc
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"xian-stack backend command returned invalid JSON: {command}"
        ) from exc


def start_xian_stack_node(
    *,
    stack_dir: Path,
    cometbft_home: Path | None = None,
    node_image_mode: str = "local_build",
    node_integrated_image: str | None = None,
    node_split_image: str | None = None,
    bds_enabled: bool,
    dashboard_enabled: bool = False,
    monitoring_enabled: bool = False,
    dashboard_host: str = "127.0.0.1",
    dashboard_port: int = 8080,
    intentkit_enabled: bool = False,
    intentkit_network_id: str | None = None,
    intentkit_host: str = "127.0.0.1",
    intentkit_port: int = 38000,
    intentkit_api_port: int = 38080,
    dex_automation_enabled: bool = False,
    dex_automation_host: str = "127.0.0.1",
    dex_automation_port: int = 38280,
    dex_automation_config: str | None = None,
    shielded_relayer_enabled: bool = False,
    shielded_relayer_host: str = "127.0.0.1",
    shielded_relayer_port: int = 38180,
    wait_for_rpc: bool = True,
    rpc_timeout_seconds: float = DEFAULT_RPC_TIMEOUT_SECONDS,
) -> dict:
    runtime_options = _runtime_command_kwargs(locals())
    return run_backend_command(
        stack_dir,
        "start",
        **runtime_options,
        wait_for_health=wait_for_rpc,
        rpc_timeout_seconds=rpc_timeout_seconds,
        rpc_url="http://127.0.0.1:26657/status",
    )


def stop_xian_stack_node(
    *,
    stack_dir: Path,
    cometbft_home: Path | None = None,
    node_image_mode: str = "local_build",
    node_integrated_image: str | None = None,
    node_split_image: str | None = None,
    bds_enabled: bool,
    dashboard_enabled: bool = False,
    monitoring_enabled: bool = False,
    dashboard_host: str = "127.0.0.1",
    dashboard_port: int = 8080,
    intentkit_enabled: bool = False,
    intentkit_network_id: str | None = None,
    intentkit_host: str = "127.0.0.1",
    intentkit_port: int = 38000,
    intentkit_api_port: int = 38080,
    dex_automation_enabled: bool = False,
    dex_automation_host: str = "127.0.0.1",
    dex_automation_port: int = 38280,
    dex_automation_config: str | None = None,
    shielded_relayer_enabled: bool = False,
    shielded_relayer_host: str = "127.0.0.1",
    shielded_relayer_port: int = 38180,
) -> dict:
    runtime_options = _runtime_command_kwargs(locals())
    return run_backend_command(
        stack_dir,
        "stop",
        **runtime_options,
    )


def get_xian_stack_node_status(
    *,
    stack_dir: Path,
    cometbft_home: Path | None = None,
    node_image_mode: str = "local_build",
    node_integrated_image: str | None = None,
    node_split_image: str | None = None,
    bds_enabled: bool,
    dashboard_enabled: bool = False,
    monitoring_enabled: bool = False,
    dashboard_host: str = "127.0.0.1",
    dashboard_port: int = 8080,
    intentkit_enabled: bool = False,
    intentkit_network_id: str | None = None,
    intentkit_host: str = "127.0.0.1",
    intentkit_port: int = 38000,
    intentkit_api_port: int = 38080,
    dex_automation_enabled: bool = False,
    dex_automation_host: str = "127.0.0.1",
    dex_automation_port: int = 38280,
    dex_automation_config: str | None = None,
    shielded_relayer_enabled: bool = False,
    shielded_relayer_host: str = "127.0.0.1",
    shielded_relayer_port: int = 38180,
) -> dict:
    runtime_options = _runtime_command_kwargs(locals())
    return run_backend_command(
        stack_dir,
        "status",
        **runtime_options,
    )


def get_xian_stack_node_endpoints(
    *,
    stack_dir: Path,
    cometbft_home: Path | None = None,
    node_image_mode: str = "local_build",
    node_integrated_image: str | None = None,
    node_split_image: str | None = None,
    bds_enabled: bool,
    dashboard_enabled: bool = False,
    monitoring_enabled: bool = False,
    dashboard_host: str = "127.0.0.1",
    dashboard_port: int = 8080,
    intentkit_enabled: bool = False,
    intentkit_network_id: str | None = None,
    intentkit_host: str = "127.0.0.1",
    intentkit_port: int = 38000,
    intentkit_api_port: int = 38080,
    dex_automation_enabled: bool = False,
    dex_automation_host: str = "127.0.0.1",
    dex_automation_port: int = 38280,
    dex_automation_config: str | None = None,
    shielded_relayer_enabled: bool = False,
    shielded_relayer_host: str = "127.0.0.1",
    shielded_relayer_port: int = 38180,
) -> dict:
    runtime_options = _runtime_command_kwargs(locals())
    return run_backend_command(
        stack_dir,
        "endpoints",
        **runtime_options,
    )


def get_xian_stack_node_health(
    *,
    stack_dir: Path,
    cometbft_home: Path | None = None,
    node_image_mode: str = "local_build",
    node_integrated_image: str | None = None,
    node_split_image: str | None = None,
    bds_enabled: bool,
    dashboard_enabled: bool = False,
    monitoring_enabled: bool = False,
    dashboard_host: str = "127.0.0.1",
    dashboard_port: int = 8080,
    intentkit_enabled: bool = False,
    intentkit_network_id: str | None = None,
    intentkit_host: str = "127.0.0.1",
    intentkit_port: int = 38000,
    intentkit_api_port: int = 38080,
    dex_automation_enabled: bool = False,
    dex_automation_host: str = "127.0.0.1",
    dex_automation_port: int = 38280,
    dex_automation_config: str | None = None,
    shielded_relayer_enabled: bool = False,
    shielded_relayer_host: str = "127.0.0.1",
    shielded_relayer_port: int = 38180,
    rpc_url: str = "http://127.0.0.1:26657/status",
    check_disk: bool = True,
) -> dict:
    runtime_options = _runtime_command_kwargs(locals())
    return run_backend_command(
        stack_dir,
        "health",
        **runtime_options,
        rpc_url=rpc_url,
        check_disk=check_disk,
    )
