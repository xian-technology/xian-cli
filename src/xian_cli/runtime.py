from __future__ import annotations

import json
import os
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
    cometbft_home: Path | None = None,
    node_image_mode: str | None = None,
    node_integrated_image: str | None = None,
    node_split_image: str | None = None,
    service_node: bool = False,
    dashboard_enabled: bool = False,
    monitoring_enabled: bool = False,
    dashboard_host: str | None = None,
    dashboard_port: int | None = None,
    intentkit_enabled: bool = False,
    intentkit_network_id: str | None = None,
    intentkit_host: str | None = None,
    intentkit_port: int | None = None,
    intentkit_api_port: int | None = None,
    shielded_relayer_enabled: bool = False,
    shielded_relayer_host: str | None = None,
    shielded_relayer_port: int | None = None,
    wait_for_health: bool | None = None,
    rpc_timeout_seconds: float | None = None,
    rpc_url: str | None = None,
    check_disk: bool | None = None,
) -> dict:
    cmd = [sys.executable, str(_backend_script(stack_dir)), command]
    if node_image_mode is not None:
        cmd.extend(["--node-image-mode", node_image_mode])
    if node_integrated_image is not None:
        cmd.extend(["--node-integrated-image", node_integrated_image])
    if node_split_image is not None:
        cmd.extend(["--node-split-image", node_split_image])
    cmd.append("--service-node" if service_node else "--no-service-node")
    cmd.append("--dashboard" if dashboard_enabled else "--no-dashboard")
    cmd.append("--monitoring" if monitoring_enabled else "--no-monitoring")
    cmd.append("--intentkit" if intentkit_enabled else "--no-intentkit")
    cmd.append(
        "--shielded-relayer"
        if shielded_relayer_enabled
        else "--no-shielded-relayer"
    )

    if dashboard_enabled:
        if dashboard_host is not None:
            cmd.extend(["--dashboard-host", dashboard_host])
        if dashboard_port is not None:
            cmd.extend(["--dashboard-port", str(dashboard_port)])
    if intentkit_enabled:
        if intentkit_network_id is not None:
            cmd.extend(["--intentkit-network-id", intentkit_network_id])
        if intentkit_host is not None:
            cmd.extend(["--intentkit-host", intentkit_host])
        if intentkit_port is not None:
            cmd.extend(["--intentkit-port", str(intentkit_port)])
        if intentkit_api_port is not None:
            cmd.extend(["--intentkit-api-port", str(intentkit_api_port)])
    if shielded_relayer_enabled:
        if shielded_relayer_host is not None:
            cmd.extend(["--shielded-relayer-host", shielded_relayer_host])
        if shielded_relayer_port is not None:
            cmd.extend(
                ["--shielded-relayer-port", str(shielded_relayer_port)]
            )

    if wait_for_health is not None:
        cmd.append(
            "--wait-for-health" if wait_for_health else "--no-wait-for-health"
        )
    if rpc_timeout_seconds is not None:
        cmd.extend(["--rpc-timeout-seconds", str(rpc_timeout_seconds)])
    if rpc_url is not None:
        cmd.extend(["--rpc-url", rpc_url])
    if check_disk is not None:
        cmd.append("--check-disk" if check_disk else "--no-check-disk")

    try:
        result = subprocess.run(
            cmd,
            cwd=stack_dir,
            check=True,
            capture_output=True,
            text=True,
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
    service_node: bool,
    dashboard_enabled: bool = False,
    monitoring_enabled: bool = False,
    dashboard_host: str = "127.0.0.1",
    dashboard_port: int = 8080,
    intentkit_enabled: bool = False,
    intentkit_network_id: str | None = None,
    intentkit_host: str = "127.0.0.1",
    intentkit_port: int = 38000,
    intentkit_api_port: int = 38080,
    shielded_relayer_enabled: bool = False,
    shielded_relayer_host: str = "127.0.0.1",
    shielded_relayer_port: int = 38180,
    wait_for_rpc: bool = True,
    rpc_timeout_seconds: float = DEFAULT_RPC_TIMEOUT_SECONDS,
) -> dict:
    return run_backend_command(
        stack_dir,
        "start",
        cometbft_home=cometbft_home,
        node_image_mode=node_image_mode,
        node_integrated_image=node_integrated_image,
        node_split_image=node_split_image,
        service_node=service_node,
        dashboard_enabled=dashboard_enabled,
        monitoring_enabled=monitoring_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        intentkit_enabled=intentkit_enabled,
        intentkit_network_id=intentkit_network_id,
        intentkit_host=intentkit_host,
        intentkit_port=intentkit_port,
        intentkit_api_port=intentkit_api_port,
        shielded_relayer_enabled=shielded_relayer_enabled,
        shielded_relayer_host=shielded_relayer_host,
        shielded_relayer_port=shielded_relayer_port,
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
    service_node: bool,
    dashboard_enabled: bool = False,
    monitoring_enabled: bool = False,
    dashboard_host: str = "127.0.0.1",
    dashboard_port: int = 8080,
    intentkit_enabled: bool = False,
    intentkit_network_id: str | None = None,
    intentkit_host: str = "127.0.0.1",
    intentkit_port: int = 38000,
    intentkit_api_port: int = 38080,
    shielded_relayer_enabled: bool = False,
    shielded_relayer_host: str = "127.0.0.1",
    shielded_relayer_port: int = 38180,
) -> dict:
    return run_backend_command(
        stack_dir,
        "stop",
        cometbft_home=cometbft_home,
        node_image_mode=node_image_mode,
        node_integrated_image=node_integrated_image,
        node_split_image=node_split_image,
        service_node=service_node,
        dashboard_enabled=dashboard_enabled,
        monitoring_enabled=monitoring_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        intentkit_enabled=intentkit_enabled,
        intentkit_network_id=intentkit_network_id,
        intentkit_host=intentkit_host,
        intentkit_port=intentkit_port,
        intentkit_api_port=intentkit_api_port,
        shielded_relayer_enabled=shielded_relayer_enabled,
        shielded_relayer_host=shielded_relayer_host,
        shielded_relayer_port=shielded_relayer_port,
    )


def get_xian_stack_node_status(
    *,
    stack_dir: Path,
    cometbft_home: Path | None = None,
    node_image_mode: str = "local_build",
    node_integrated_image: str | None = None,
    node_split_image: str | None = None,
    service_node: bool,
    dashboard_enabled: bool = False,
    monitoring_enabled: bool = False,
    dashboard_host: str = "127.0.0.1",
    dashboard_port: int = 8080,
    intentkit_enabled: bool = False,
    intentkit_network_id: str | None = None,
    intentkit_host: str = "127.0.0.1",
    intentkit_port: int = 38000,
    intentkit_api_port: int = 38080,
    shielded_relayer_enabled: bool = False,
    shielded_relayer_host: str = "127.0.0.1",
    shielded_relayer_port: int = 38180,
) -> dict:
    return run_backend_command(
        stack_dir,
        "status",
        cometbft_home=cometbft_home,
        node_image_mode=node_image_mode,
        node_integrated_image=node_integrated_image,
        node_split_image=node_split_image,
        service_node=service_node,
        dashboard_enabled=dashboard_enabled,
        monitoring_enabled=monitoring_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        intentkit_enabled=intentkit_enabled,
        intentkit_network_id=intentkit_network_id,
        intentkit_host=intentkit_host,
        intentkit_port=intentkit_port,
        intentkit_api_port=intentkit_api_port,
        shielded_relayer_enabled=shielded_relayer_enabled,
        shielded_relayer_host=shielded_relayer_host,
        shielded_relayer_port=shielded_relayer_port,
    )


def get_xian_stack_node_endpoints(
    *,
    stack_dir: Path,
    cometbft_home: Path | None = None,
    node_image_mode: str = "local_build",
    node_integrated_image: str | None = None,
    node_split_image: str | None = None,
    service_node: bool,
    dashboard_enabled: bool = False,
    monitoring_enabled: bool = False,
    dashboard_host: str = "127.0.0.1",
    dashboard_port: int = 8080,
    intentkit_enabled: bool = False,
    intentkit_network_id: str | None = None,
    intentkit_host: str = "127.0.0.1",
    intentkit_port: int = 38000,
    intentkit_api_port: int = 38080,
    shielded_relayer_enabled: bool = False,
    shielded_relayer_host: str = "127.0.0.1",
    shielded_relayer_port: int = 38180,
) -> dict:
    return run_backend_command(
        stack_dir,
        "endpoints",
        cometbft_home=cometbft_home,
        node_image_mode=node_image_mode,
        node_integrated_image=node_integrated_image,
        node_split_image=node_split_image,
        service_node=service_node,
        dashboard_enabled=dashboard_enabled,
        monitoring_enabled=monitoring_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        intentkit_enabled=intentkit_enabled,
        intentkit_network_id=intentkit_network_id,
        intentkit_host=intentkit_host,
        intentkit_port=intentkit_port,
        intentkit_api_port=intentkit_api_port,
        shielded_relayer_enabled=shielded_relayer_enabled,
        shielded_relayer_host=shielded_relayer_host,
        shielded_relayer_port=shielded_relayer_port,
    )


def get_xian_stack_node_health(
    *,
    stack_dir: Path,
    cometbft_home: Path | None = None,
    node_image_mode: str = "local_build",
    node_integrated_image: str | None = None,
    node_split_image: str | None = None,
    service_node: bool,
    dashboard_enabled: bool = False,
    monitoring_enabled: bool = False,
    dashboard_host: str = "127.0.0.1",
    dashboard_port: int = 8080,
    intentkit_enabled: bool = False,
    intentkit_network_id: str | None = None,
    intentkit_host: str = "127.0.0.1",
    intentkit_port: int = 38000,
    intentkit_api_port: int = 38080,
    shielded_relayer_enabled: bool = False,
    shielded_relayer_host: str = "127.0.0.1",
    shielded_relayer_port: int = 38180,
    rpc_url: str = "http://127.0.0.1:26657/status",
    check_disk: bool = True,
) -> dict:
    return run_backend_command(
        stack_dir,
        "health",
        cometbft_home=cometbft_home,
        node_image_mode=node_image_mode,
        node_integrated_image=node_integrated_image,
        node_split_image=node_split_image,
        service_node=service_node,
        dashboard_enabled=dashboard_enabled,
        monitoring_enabled=monitoring_enabled,
        dashboard_host=dashboard_host,
        dashboard_port=dashboard_port,
        intentkit_enabled=intentkit_enabled,
        intentkit_network_id=intentkit_network_id,
        intentkit_host=intentkit_host,
        intentkit_port=intentkit_port,
        intentkit_api_port=intentkit_api_port,
        shielded_relayer_enabled=shielded_relayer_enabled,
        shielded_relayer_host=shielded_relayer_host,
        shielded_relayer_port=shielded_relayer_port,
        rpc_url=rpc_url,
        check_disk=check_disk,
    )
