from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


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
        "unable to resolve xian-stack directory; pass --stack-dir or set stack_dir in the node profile"
    )


def default_home_for_backend(*, base_dir: Path, runtime_backend: str, stack_dir: Path | None = None) -> Path:
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
    timeout_seconds: float = 30.0,
    poll_interval: float = 1.0,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None

    while time.monotonic() < deadline:
        try:
            payload = fetch_json(rpc_url, timeout=poll_interval)
            if payload.get("result"):
                return payload
        except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc

        time.sleep(poll_interval)

    raise TimeoutError(f"RPC did not become ready at {rpc_url}") from last_error


def run_make_target(stack_dir: Path, target: str) -> None:
    subprocess.run(
        ["make", target],
        cwd=stack_dir,
        check=True,
    )


def start_xian_stack_node(
    *,
    stack_dir: Path,
    service_node: bool,
    wait_for_rpc: bool = True,
    rpc_timeout_seconds: float = 30.0,
) -> dict:
    container_target = "abci-bds-up" if service_node else "abci-up"
    node_target = "up-bds" if service_node else "up"

    run_make_target(stack_dir, container_target)
    run_make_target(stack_dir, node_target)

    result = {
        "stack_dir": str(stack_dir),
        "container_target": container_target,
        "node_target": node_target,
        "rpc_checked": wait_for_rpc,
    }

    if wait_for_rpc:
        payload = wait_for_rpc_ready(timeout_seconds=rpc_timeout_seconds)
        result["rpc_status"] = payload

    return result


def stop_xian_stack_node(*, stack_dir: Path, service_node: bool) -> dict:
    container_target = "abci-bds-down" if service_node else "abci-down"

    run_make_target(stack_dir, "down")
    run_make_target(stack_dir, container_target)

    return {
        "stack_dir": str(stack_dir),
        "container_target": container_target,
    }
