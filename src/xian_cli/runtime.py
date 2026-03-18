from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

DEFAULT_RPC_TIMEOUT_SECONDS = 90.0


def resolve_stack_dir(base_dir: Path, explicit: Path | None = None) -> Path:
    if explicit is not None:
        return explicit.resolve()

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
    *, base_dir: Path, runtime_backend: str, stack_dir: Path | None = None
) -> Path:
    if runtime_backend == "xian-stack":
        resolved_stack_dir = resolve_stack_dir(base_dir, explicit=stack_dir)
        return resolved_stack_dir / ".cometbft"

    return Path.home() / ".cometbft"


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
    service_node: bool = False,
    dashboard_enabled: bool = False,
    dashboard_host: str | None = None,
    dashboard_port: int | None = None,
    wait_for_health: bool | None = None,
    rpc_timeout_seconds: float | None = None,
    rpc_url: str | None = None,
) -> dict:
    cmd = [sys.executable, str(_backend_script(stack_dir)), command]
    cmd.append("--service-node" if service_node else "--no-service-node")
    cmd.append("--dashboard" if dashboard_enabled else "--no-dashboard")

    if dashboard_enabled:
        if dashboard_host is not None:
            cmd.extend(["--dashboard-host", dashboard_host])
        if dashboard_port is not None:
            cmd.extend(["--dashboard-port", str(dashboard_port)])

    if wait_for_health is not None:
        cmd.append(
            "--wait-for-health" if wait_for_health else "--no-wait-for-health"
        )
    if rpc_timeout_seconds is not None:
        cmd.extend(["--rpc-timeout-seconds", str(rpc_timeout_seconds)])
    if rpc_url is not None:
        cmd.extend(["--rpc-url", rpc_url])

    result = subprocess.run(
        cmd,
        cwd=stack_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"xian-stack backend command returned invalid JSON: {command}"
        ) from exc


def start_xian_stack_node(
    *,
    stack_dir: Path,
    service_node: bool,
    dashboard_enabled: bool = False,
    dashboard_host: str = "127.0.0.1",
    dashboard_port: int = 8080,
    wait_for_rpc: bool = True,
    rpc_timeout_seconds: float = DEFAULT_RPC_TIMEOUT_SECONDS,
) -> dict:
    return run_backend_command(
        stack_dir,
        "start",
        service_node=service_node,
        dashboard_enabled=dashboard_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        wait_for_health=wait_for_rpc,
        rpc_timeout_seconds=rpc_timeout_seconds,
        rpc_url="http://127.0.0.1:26657/status",
    )


def stop_xian_stack_node(
    *,
    stack_dir: Path,
    service_node: bool,
    dashboard_enabled: bool = False,
    dashboard_host: str = "127.0.0.1",
    dashboard_port: int = 8080,
) -> dict:
    return run_backend_command(
        stack_dir,
        "stop",
        service_node=service_node,
        dashboard_enabled=dashboard_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
    )


def get_xian_stack_node_status(
    *,
    stack_dir: Path,
    service_node: bool,
    dashboard_enabled: bool = False,
    dashboard_host: str = "127.0.0.1",
    dashboard_port: int = 8080,
) -> dict:
    return run_backend_command(
        stack_dir,
        "status",
        service_node=service_node,
        dashboard_enabled=dashboard_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
    )
