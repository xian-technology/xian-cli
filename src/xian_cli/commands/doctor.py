from __future__ import annotations

import argparse
import json
from pathlib import Path

from xian_cli.abci_bridge import (
    get_genesis_builder_module,
    get_node_admin_module,
    get_node_setup_module,
)
from xian_cli.commands.node import (
    _collect_node_status,
    _collect_statesync_readiness,
)
from xian_cli.config_repo import resolve_configs_dir
from xian_cli.runtime import resolve_stack_dir


def _run_check(name: str, fn) -> dict[str, object]:
    try:
        detail = fn()
    except Exception as exc:
        return {
            "name": name,
            "ok": False,
            "detail": str(exc),
        }

    return {
        "name": name,
        "ok": True,
        "detail": detail,
    }


def _doctor_node_artifacts(status: dict[str, object]) -> dict[str, object]:
    missing = []
    if not status.get("config_present"):
        missing.append("config.toml")
    if not status.get("xian_config_present"):
        missing.append("xian.toml")
    if not status.get("genesis_present"):
        missing.append("genesis.json")
    if not status.get("node_key_present"):
        missing.append("node_key.json")
    if not status.get("priv_validator_state_present"):
        missing.append("priv_validator_state.json")
    if not status.get("initialized") or missing:
        detail = ", ".join(missing) if missing else "node home not initialized"
        raise RuntimeError(detail)
    return {
        "home": status.get("home"),
        "node_id": status.get("node_id"),
    }


def _doctor_backend_check(status: dict[str, object]) -> dict[str, object]:
    if status.get("backend_error"):
        raise RuntimeError(str(status["backend_error"]))
    if not status.get("backend_running"):
        raise RuntimeError("xian-stack backend is not running")
    return {
        "stack_dir": status.get("stack_dir"),
        "backend_running": True,
    }


def _doctor_rpc_check(status: dict[str, object]) -> dict[str, object]:
    if not status.get("rpc_reachable"):
        raise RuntimeError(str(status.get("rpc_error", "RPC is unreachable")))
    return status.get("summary", {})


def _doctor_statesync_check(status: dict[str, object]) -> dict[str, object]:
    readiness = _collect_statesync_readiness(Path(str(status["home"])))
    if readiness["state"] == "incomplete":
        raise RuntimeError(
            "statesync is enabled but trust settings are incomplete"
        )
    return readiness


def _doctor_snapshot_check(status: dict[str, object]) -> dict[str, object]:
    return {
        "effective_snapshot_url": status.get("effective_snapshot_url"),
        "available": bool(status.get("effective_snapshot_url")),
    }


def _doctor_service_check(
    status: dict[str, object],
    *,
    service_name: str,
    reachable_key: str,
    error_key: str,
) -> dict[str, object]:
    backend_status = status.get("backend_status")
    if not isinstance(backend_status, dict):
        raise RuntimeError("backend status is unavailable")
    if not backend_status.get(reachable_key):
        raise RuntimeError(
            str(backend_status.get(error_key, f"{service_name} is unreachable"))
        )
    return {
        "service": service_name,
        "reachable": True,
        "url": status.get("endpoints", {}).get(service_name),
    }


def _profile_service_enabled(profile: dict[str, object], name: str) -> bool:
    services = profile.get("services")
    if not isinstance(services, dict):
        return False
    service = services.get(name)
    if not isinstance(service, dict):
        return False
    return bool(service.get("enabled"))


def _handle_doctor(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    checks = [
        _run_check(
            "configs_dir",
            lambda: str(
                resolve_configs_dir(base_dir, explicit=args.configs_dir)
            ),
        ),
        _run_check(
            "stack_dir",
            lambda: str(resolve_stack_dir(base_dir, explicit=args.stack_dir)),
        ),
        _run_check("node_setup", lambda: get_node_setup_module().__name__),
        _run_check("node_admin", lambda: get_node_admin_module().__name__),
        _run_check(
            "genesis_builder",
            lambda: get_genesis_builder_module().__name__,
        ),
    ]

    if args.name is not None:
        status_args = argparse.Namespace(
            name=args.name,
            base_dir=base_dir,
            profile=args.profile,
            network=args.network,
            stack_dir=args.stack_dir,
            configs_dir=args.configs_dir,
            home=args.home,
            rpc_url=args.rpc_url,
            skip_rpc=args.skip_live_checks,
        )
        node_status = _collect_node_status(
            status_args,
            check_rpc=not args.skip_live_checks,
            check_backend=not args.skip_live_checks,
        )
        checks.append(
            _run_check(
                "node_status",
                lambda: node_status,
            )
        )
        checks.append(
            _run_check(
                "node_artifacts",
                lambda: _doctor_node_artifacts(node_status),
            )
        )
        checks.append(
            _run_check(
                "endpoints",
                lambda: node_status.get("endpoints", {}),
            )
        )
        checks.append(
            _run_check(
                "statesync",
                lambda: _doctor_statesync_check(node_status),
            )
        )
        checks.append(
            _run_check(
                "snapshot_bootstrap",
                lambda: _doctor_snapshot_check(node_status),
            )
        )
        if not args.skip_live_checks:
            checks.append(
                _run_check(
                    "backend",
                    lambda: _doctor_backend_check(node_status),
                )
            )
            checks.append(
                _run_check(
                    "rpc",
                    lambda: _doctor_rpc_check(node_status),
                )
            )
            profile = node_status.get("profile", {})
            if not isinstance(profile, dict):
                profile = {}
            if _profile_service_enabled(profile, "dashboard"):
                checks.append(
                    _run_check(
                        "dashboard",
                        lambda: _doctor_service_check(
                            node_status,
                            service_name="dashboard",
                            reachable_key="dashboard_reachable",
                            error_key="dashboard_error",
                        ),
                    )
                )
            if _profile_service_enabled(profile, "monitoring"):
                checks.append(
                    _run_check(
                        "prometheus",
                        lambda: _doctor_service_check(
                            node_status,
                            service_name="prometheus",
                            reachable_key="prometheus_reachable",
                            error_key="prometheus_error",
                        ),
                    )
                )
            if _profile_service_enabled(profile, "intentkit"):
                checks.append(
                    _run_check(
                        "intentkit",
                        lambda: _doctor_service_check(
                            node_status,
                            service_name="intentkit",
                            reachable_key="intentkit_reachable",
                            error_key="intentkit_probe_error",
                        ),
                    )
                )
                checks.append(
                    _run_check(
                        "intentkit_api",
                        lambda: _doctor_service_check(
                            node_status,
                            service_name="intentkit_api",
                            reachable_key="intentkit_api_reachable",
                            error_key="intentkit_api_error",
                        ),
                    )
                )
            if _profile_service_enabled(profile, "dex_automation"):
                checks.append(
                    _run_check(
                        "dex_automation",
                        lambda: _doctor_service_check(
                            node_status,
                            service_name="dex_automation",
                            reachable_key="dex_automation_reachable",
                            error_key="dex_automation_error",
                        ),
                    )
                )
            if _profile_service_enabled(profile, "monitoring"):
                checks.append(
                    _run_check(
                        "grafana",
                        lambda: _doctor_service_check(
                            node_status,
                            service_name="grafana",
                            reachable_key="grafana_reachable",
                            error_key="grafana_error",
                        ),
                    )
                )

    result = {
        "ok": all(check["ok"] for check in checks),
        "checks": checks,
    }
    if args.name is not None:
        result["node"] = node_status
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1
