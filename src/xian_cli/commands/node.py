from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path
from urllib.parse import urlparse

from xian_cli.abci_bridge import get_node_setup_module
from xian_cli.commands.common import (
    _block_age_seconds,
    _effective_node_image_config,
    _effective_node_release_manifest,
    _network_shielded_relayer_endpoints,
    _stack_runtime_profile_kwargs,
)
from xian_cli.commands.node_context import (
    _extract_priv_validator_key,
    _load_profile_and_network,
    _resolve_effective_genesis_payload,
    _resolve_effective_snapshot_url,
    _resolve_home,
    _resolve_path,
    _resolve_stack_dir_from_profile,
    _restore_snapshot,
)
from xian_cli.models import NodeProfile, read_json
from xian_cli.runtime import (
    fetch_json,
    get_xian_stack_node_endpoints,
    get_xian_stack_node_health,
    get_xian_stack_node_status,
    resolve_stack_dir,
    start_xian_stack_node,
    stop_xian_stack_node,
)


def _initialize_node_from_args(args: argparse.Namespace) -> dict:
    node_setup = get_node_setup_module()

    base_dir = args.base_dir.resolve()
    profile_path, profile, network_path, network = _load_profile_and_network(
        base_dir=base_dir,
        name=args.name,
        profile_arg=args.profile,
        network_arg=args.network,
        configs_dir=args.configs_dir,
    )

    stack_dir = _resolve_stack_dir_from_profile(
        base_dir=base_dir,
        profile=profile,
        explicit_stack_dir=args.stack_dir,
    )

    explicit_validator_key = args.validator_key
    if (
        explicit_validator_key is not None
        and not explicit_validator_key.is_absolute()
    ):
        explicit_validator_key = (base_dir / explicit_validator_key).resolve()

    validator_key_ref = explicit_validator_key or _resolve_path(
        profile.get("validator_key_ref"),
        base_dir=base_dir,
        fallback_dir=profile_path.parent,
    )
    if validator_key_ref is None or not validator_key_ref.exists():
        raise FileNotFoundError(
            "validator key reference is required; set validator_key_ref in the "
            "node profile or pass --validator-key"
        )

    validator_key_payload = _extract_priv_validator_key(
        read_json(validator_key_ref)
    )
    genesis, effective_genesis_source = _resolve_effective_genesis_payload(
        profile=profile,
        network=network,
        base_dir=base_dir,
        manifest_path=network_path,
        configs_dir=args.configs_dir,
    )

    if genesis.get("chain_id") and genesis["chain_id"] != network["chain_id"]:
        raise ValueError(
            f"genesis chain_id {genesis['chain_id']} does not match manifest "
            f"chain_id {network['chain_id']}"
        )

    home = _resolve_home(
        base_dir=base_dir,
        profile=profile,
        profile_path=profile_path,
        stack_dir=stack_dir,
        explicit_home=args.home,
    )

    seed_nodes = list(network.get("seed_nodes") or [])
    seed_nodes.extend(profile.get("seeds") or [])

    configs = node_setup.render_node_configs(
        options=node_setup.NodeConfigOptions(
            moniker=profile["moniker"],
            seed_nodes=tuple(seed_nodes),
            service_node=bool(profile.get("service_node")),
            enable_pruning=bool(profile.get("pruning_enabled")),
            blocks_to_keep=int(profile.get("blocks_to_keep", 100000)),
            block_policy_mode=str(
                profile.get(
                    "block_policy_mode",
                    network.get("block_policy_mode", "on_demand"),
                )
            ),
            block_policy_interval=str(
                profile.get(
                    "block_policy_interval",
                    network.get("block_policy_interval", "0s"),
                )
            ),
            transaction_trace_logging=bool(
                profile.get("transaction_trace_logging", False)
            ),
            app_logging=node_setup.AppLoggingOptions(
                level=str(profile.get("app_log_level", "INFO")),
                json_logging=bool(profile.get("app_log_json", False)),
                rotation_hours=int(profile.get("app_log_rotation_hours", 1)),
                retention_days=int(profile.get("app_log_retention_days", 7)),
            ),
            simulation=node_setup.SimulationOptions(
                enabled=bool(profile.get("simulation_enabled", True)),
                max_concurrency=int(
                    profile.get("simulation_max_concurrency", 2)
                ),
                timeout_ms=int(profile.get("simulation_timeout_ms", 3000)),
                max_chi=int(profile.get("simulation_max_chi", 1_000_000)),
            ),
            parallel_execution=node_setup.ParallelExecutionOptions(
                enabled=bool(profile.get("parallel_execution_enabled", False)),
                workers=int(profile.get("parallel_execution_workers", 0)),
                min_transactions=int(
                    profile.get("parallel_execution_min_transactions", 8)
                ),
            ),
            # The xian-stack runtime publishes the app metrics port from Docker,
            # so the in-container exporter must listen on all interfaces.
            metrics=node_setup.MetricsOptions(host="0.0.0.0"),
        )
    )
    config = configs["cometbft"]
    xian_config = configs["xian"]

    result = node_setup.materialize_cometbft_home(
        home=home,
        config=config,
        xian_config=xian_config,
        genesis=genesis,
        priv_validator_key=validator_key_payload,
        overwrite=args.force,
    )
    effective_snapshot_url = _resolve_effective_snapshot_url(
        profile=profile,
        network=network,
        explicit_snapshot_url=getattr(args, "snapshot_url", None),
    )
    result["effective_snapshot_url"] = effective_snapshot_url
    result["effective_genesis_source"] = effective_genesis_source
    result["snapshot_restored"] = False
    if getattr(args, "restore_snapshot", False):
        snapshot_result = _restore_snapshot(
            base_dir=base_dir,
            profile=profile,
            profile_path=profile_path,
            network=network,
            stack_dir=stack_dir,
            explicit_home=home,
            explicit_snapshot_url=getattr(args, "snapshot_url", None),
        )
        result["snapshot_restored"] = True
        result["snapshot"] = snapshot_result
    return result


def _handle_node_init(args: argparse.Namespace) -> int:
    result = _initialize_node_from_args(args)
    print(json.dumps(result, indent=2))
    return 0


def _handle_node_start(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    profile_path, profile, _, network = _load_profile_and_network(
        base_dir=base_dir,
        name=args.name,
        profile_arg=args.profile,
        network_arg=args.network,
        configs_dir=args.configs_dir,
    )

    stack_dir = resolve_stack_dir(
        base_dir,
        explicit=_resolve_stack_dir_from_profile(
            base_dir=base_dir,
            profile=profile,
            explicit_stack_dir=args.stack_dir,
        ),
    )
    home = _resolve_home(
        base_dir=base_dir,
        profile=profile,
        profile_path=profile_path,
        stack_dir=stack_dir,
    )
    config_path = home / "config" / "config.toml"
    xian_config_path = home / "config" / "xian.toml"
    if not config_path.exists():
        raise FileNotFoundError(
            f"{config_path} does not exist; "
            f"run `xian node init {args.name}` first"
        )
    if not xian_config_path.exists():
        raise FileNotFoundError(
            f"{xian_config_path} does not exist; "
            f"run `xian node init {args.name}` first"
        )

    result = start_xian_stack_node(
        stack_dir=stack_dir,
        cometbft_home=home,
        **_stack_runtime_profile_kwargs(profile, network),
        wait_for_rpc=not args.skip_health_check,
        rpc_timeout_seconds=args.rpc_timeout_seconds,
    )
    print(json.dumps(result, indent=2))
    return 0


def _handle_node_stop(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    profile_path, profile, _, network = _load_profile_and_network(
        base_dir=base_dir,
        name=args.name,
        profile_arg=args.profile,
        network_arg=args.network,
        configs_dir=args.configs_dir,
    )

    stack_dir = resolve_stack_dir(
        base_dir,
        explicit=_resolve_stack_dir_from_profile(
            base_dir=base_dir,
            profile=profile,
            explicit_stack_dir=args.stack_dir,
        ),
    )
    result = stop_xian_stack_node(
        stack_dir=stack_dir,
        cometbft_home=_resolve_home(
            base_dir=base_dir,
            profile=profile,
            profile_path=profile_path,
            stack_dir=stack_dir,
        ),
        **_stack_runtime_profile_kwargs(profile, network),
    )
    print(json.dumps(result, indent=2))
    return 0


def _resolve_node_context(args: argparse.Namespace) -> dict[str, object]:
    base_dir = args.base_dir.resolve()
    profile_path, profile, network_path, network = _load_profile_and_network(
        base_dir=base_dir,
        name=args.name,
        profile_arg=args.profile,
        network_arg=args.network,
        configs_dir=args.configs_dir,
    )

    stack_dir = _resolve_stack_dir_from_profile(
        base_dir=base_dir,
        profile=profile,
        explicit_stack_dir=args.stack_dir,
    )
    stack_dir = resolve_stack_dir(base_dir, explicit=stack_dir)

    home = _resolve_home(
        base_dir=base_dir,
        profile=profile,
        profile_path=profile_path,
        stack_dir=stack_dir,
        explicit_home=getattr(args, "home", None),
    )
    return {
        "base_dir": base_dir,
        "profile_path": profile_path,
        "profile": profile,
        "network_path": network_path,
        "network": network,
        "stack_dir": stack_dir,
        "home": home,
    }


def _format_url_host(hostname: str) -> str:
    if ":" in hostname and not hostname.startswith("["):
        return f"[{hostname}]"
    return hostname


def _display_endpoint_host(hostname: str) -> str:
    if hostname == "0.0.0.0":
        return "127.0.0.1"
    if hostname == "::":
        return "::1"
    return hostname


def _replace_url_port(url: str, *, port: int, suffix: str = "") -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "http"
    hostname = _format_url_host(parsed.hostname or "127.0.0.1")
    return f"{scheme}://{hostname}:{port}{suffix}"


def _rpc_base_url(rpc_status_url: str) -> str:
    if rpc_status_url.endswith("/status"):
        return rpc_status_url[: -len("/status")]
    return rpc_status_url.rstrip("/")


def _fallback_node_endpoints(
    *,
    rpc_status_url: str,
    profile: NodeProfile,
    network: dict[str, object] | None = None,
) -> dict[str, str]:
    base_url = _rpc_base_url(rpc_status_url)
    endpoints = {
        "rpc": base_url,
        "rpc_status": rpc_status_url,
        "abci_query": f"{base_url}/abci_query",
        "cometbft_metrics": _replace_url_port(
            base_url,
            port=26660,
            suffix="/metrics",
        ),
        "xian_metrics": _replace_url_port(
            base_url,
            port=9108,
            suffix="/metrics",
        ),
    }
    if bool(profile.get("dashboard_enabled")):
        dashboard_host = _display_endpoint_host(
            str(profile.get("dashboard_host", "127.0.0.1"))
        )
        dashboard_port = int(profile.get("dashboard_port", 8080))
        dashboard_url = f"http://{dashboard_host}:{dashboard_port}"
        endpoints["dashboard"] = dashboard_url
        endpoints["dashboard_status"] = f"{dashboard_url}/api/status"
    if bool(profile.get("monitoring_enabled")):
        endpoints["prometheus"] = _replace_url_port(base_url, port=9090)
        endpoints["grafana"] = _replace_url_port(base_url, port=3000)
    if bool(profile.get("intentkit_enabled")):
        intentkit_host = _display_endpoint_host(
            str(profile.get("intentkit_host", "127.0.0.1"))
        )
        intentkit_port = int(profile.get("intentkit_port", 38000))
        intentkit_api_port = int(profile.get("intentkit_api_port", 38080))
        frontend_url = f"http://{intentkit_host}:{intentkit_port}"
        api_url = f"http://{intentkit_host}:{intentkit_api_port}"
        endpoints["intentkit"] = frontend_url
        endpoints["intentkit_api"] = api_url
        endpoints["intentkit_api_health"] = f"{api_url}/health"
    if bool(profile.get("shielded_relayer_enabled")):
        relayer_host = _display_endpoint_host(
            str(profile.get("shielded_relayer_host", "127.0.0.1"))
        )
        relayer_port = int(profile.get("shielded_relayer_port", 38180))
        relayer_url = f"http://{relayer_host}:{relayer_port}"
        endpoints["shielded_relayer"] = relayer_url
        endpoints["shielded_relayer_health"] = f"{relayer_url}/health"
        endpoints["shielded_relayer_info"] = f"{relayer_url}/v1/info"
        endpoints["shielded_relayer_metrics"] = f"{relayer_url}/metrics"
        endpoints["shielded_relayer_quote"] = f"{relayer_url}/v1/quote"
        endpoints["shielded_relayer_jobs"] = f"{relayer_url}/v1/jobs"
    else:
        endpoints.update(_network_shielded_relayer_endpoints(network))
    return endpoints


def _collect_node_endpoints(args: argparse.Namespace) -> dict[str, object]:
    context = _resolve_node_context(args)
    profile = context["profile"]
    network = context["network"]
    stack_dir = context["stack_dir"]
    home = context["home"]
    rpc_status_url = getattr(args, "rpc_url", "http://127.0.0.1:26657/status")
    node_image_mode, node_integrated_image, node_split_image = (
        _effective_node_image_config(profile, network)
    )

    payload: dict[str, object] = {
        "profile_path": str(context["profile_path"]),
        "network_path": str(context["network_path"]),
        "node_image_mode": node_image_mode,
        "node_integrated_image": node_integrated_image,
        "node_split_image": node_split_image,
        "service_node": bool(profile.get("service_node")),
        "operator_profile": profile.get("operator_profile"),
        "monitoring_profile": profile.get("monitoring_profile"),
        "dashboard_enabled": bool(profile.get("dashboard_enabled")),
        "monitoring_enabled": bool(profile.get("monitoring_enabled")),
        "intentkit_enabled": bool(profile.get("intentkit_enabled")),
        "dex_automation_enabled": bool(profile.get("dex_automation_enabled")),
        "shielded_relayer_enabled": bool(
            profile.get("shielded_relayer_enabled")
        ),
        "intentkit_network_id": profile.get("intentkit_network_id"),
    }
    if stack_dir is not None:
        payload["stack_dir"] = str(stack_dir)

    if stack_dir is not None:
        try:
            backend_payload = get_xian_stack_node_endpoints(
                stack_dir=stack_dir,
                cometbft_home=home,
                **_stack_runtime_profile_kwargs(profile, context["network"]),
            )
            payload["endpoints"] = backend_payload["endpoints"]
            payload["backend_checked"] = True
            return payload
        except Exception as exc:
            payload["backend_checked"] = True
            payload["backend_error"] = str(exc)

    payload["endpoints"] = _fallback_node_endpoints(
        rpc_status_url=rpc_status_url,
        profile=profile,
        network=network,
    )
    return payload


def _summarize_node_status(result: dict[str, object]) -> dict[str, object]:
    rpc_payload = result.get("rpc_status")
    rpc_result = (
        rpc_payload.get("result", {}) if isinstance(rpc_payload, dict) else {}
    )
    sync_info = (
        rpc_result.get("sync_info", {}) if isinstance(rpc_result, dict) else {}
    )
    node_info = (
        rpc_result.get("node_info", {}) if isinstance(rpc_result, dict) else {}
    )
    other = node_info.get("other", {}) if isinstance(node_info, dict) else {}

    state = "ready"
    if not result.get("initialized"):
        state = "not_initialized"
    elif result.get("backend_checked") and not result.get("backend_running"):
        state = "stopped"
    elif result.get("rpc_checked") and not result.get("rpc_reachable"):
        state = "rpc_unreachable"

    summary: dict[str, object] = {
        "state": state,
        "initialized": bool(result.get("initialized")),
        "service_node": bool(result.get("profile", {}).get("service_node")),
        "operator_profile": result.get("profile", {}).get("operator_profile"),
        "monitoring_profile": result.get("profile", {}).get(
            "monitoring_profile"
        ),
        "dashboard_enabled": bool(
            result.get("profile", {}).get("dashboard_enabled")
        ),
        "monitoring_enabled": bool(
            result.get("profile", {}).get("monitoring_enabled")
        ),
        "intentkit_enabled": bool(
            result.get("profile", {}).get("intentkit_enabled")
        ),
        "dex_automation_enabled": bool(
            result.get("profile", {}).get("dex_automation_enabled")
        ),
        "shielded_relayer_enabled": bool(
            result.get("profile", {}).get("shielded_relayer_enabled")
        ),
        "backend_running": result.get("backend_running"),
        "rpc_reachable": result.get("rpc_reachable"),
        "rpc_height": sync_info.get("latest_block_height"),
        "rpc_latest_block_time": sync_info.get("latest_block_time"),
        "rpc_block_age_seconds": _block_age_seconds(
            sync_info.get("latest_block_time")
        ),
        "rpc_catching_up": sync_info.get("catching_up"),
        "rpc_network": node_info.get("network"),
        "peer_count": other.get("n_peers"),
        "node_image_mode": result.get("profile", {}).get("node_image_mode"),
        "node_integrated_image": result.get("profile", {}).get(
            "node_integrated_image"
        ),
        "node_split_image": result.get("profile", {}).get("node_split_image"),
    }

    release_manifest = result.get("node_release_manifest")
    if isinstance(release_manifest, dict):
        components = release_manifest.get("components")
        build = release_manifest.get("build")
        if isinstance(components, dict):
            summary["release_manifest_refs"] = {
                str(name): component.get("ref")
                for name, component in components.items()
                if isinstance(name, str) and isinstance(component, dict)
            }
        if isinstance(build, dict):
            summary["release_manifest_build"] = {
                "python_image": build.get("python_image"),
                "go_image": build.get("go_image"),
                "cometbft_version": build.get("cometbft_version"),
                "cometbft_source_url": build.get("cometbft_source_url"),
                "cometbft_source_sha256": build.get("cometbft_source_sha256"),
                "s6_overlay_version": build.get("s6_overlay_version"),
                "s6_overlay_noarch_sha256": build.get(
                    "s6_overlay_noarch_sha256"
                ),
                "s6_overlay_x86_64_sha256": build.get(
                    "s6_overlay_x86_64_sha256"
                ),
                "s6_overlay_aarch64_sha256": build.get(
                    "s6_overlay_aarch64_sha256"
                ),
            }

    backend_status = result.get("backend_status")
    if isinstance(backend_status, dict):
        compose_services = backend_status.get("compose_services")
        if isinstance(compose_services, list):
            runtime_images = {
                str(service.get("service")): service.get("image")
                for service in compose_services
                if isinstance(service, dict)
                and isinstance(service.get("service"), str)
                and service.get("image")
            }
            if runtime_images:
                summary["runtime_service_images"] = runtime_images
        if summary["dashboard_enabled"]:
            summary["dashboard_reachable"] = backend_status.get(
                "dashboard_reachable"
            )
        if summary["monitoring_enabled"]:
            summary["prometheus_reachable"] = backend_status.get(
                "prometheus_reachable"
            )
            summary["grafana_reachable"] = backend_status.get(
                "grafana_reachable"
            )
        if summary["intentkit_enabled"]:
            summary["intentkit_running"] = backend_status.get(
                "intentkit_running"
            )
            summary["intentkit_reachable"] = backend_status.get(
                "intentkit_reachable"
            )
            summary["intentkit_api_reachable"] = backend_status.get(
                "intentkit_api_reachable"
            )
        if summary["dex_automation_enabled"]:
            summary["dex_automation_running"] = backend_status.get(
                "dex_automation_running"
            )
            summary["dex_automation_reachable"] = backend_status.get(
                "dex_automation_reachable"
            )
        if summary["shielded_relayer_enabled"]:
            summary["shielded_relayer_running"] = backend_status.get(
                "shielded_relayer_running"
            )
            summary["shielded_relayer_reachable"] = backend_status.get(
                "shielded_relayer_reachable"
            )
    return summary


def _collect_node_status(
    args: argparse.Namespace,
    *,
    check_rpc: bool,
    check_backend: bool = True,
) -> dict:
    context = _resolve_node_context(args)
    profile_path = context["profile_path"]
    profile = context["profile"]
    network_path = context["network_path"]
    network = context["network"]
    stack_dir = context["stack_dir"]
    home = context["home"]
    node_image_mode, node_integrated_image, node_split_image = (
        _effective_node_image_config(profile, network)
    )
    node_release_manifest = _effective_node_release_manifest(profile, network)
    config_path = home / "config" / "config.toml"
    xian_config_path = home / "config" / "xian.toml"
    genesis_path = home / "config" / "genesis.json"
    node_key_path = home / "config" / "node_key.json"
    validator_state_path = home / "data" / "priv_validator_state.json"

    result: dict[str, object] = {
        "profile_path": str(profile_path),
        "network_path": str(network_path),
        "home": str(home),
        "initialized": config_path.exists() and xian_config_path.exists(),
        "config_present": config_path.exists(),
        "xian_config_present": xian_config_path.exists(),
        "genesis_present": genesis_path.exists(),
        "node_key_present": node_key_path.exists(),
        "priv_validator_state_present": validator_state_path.exists(),
        "effective_genesis_source": (
            profile.get("genesis_url")
            or network.get("genesis_source")
            or (
                None
                if network.get("genesis_preset") is None
                else f"preset:{network['genesis_preset']}"
            )
        ),
        "effective_snapshot_url": _resolve_effective_snapshot_url(
            profile=profile,
            network=network,
        ),
        "rpc_checked": check_rpc,
        "profile": {
            "name": args.name,
            "network": profile.get("network"),
            "node_image_mode": node_image_mode,
            "node_integrated_image": node_integrated_image,
            "node_split_image": node_split_image,
            "node_release_manifest": node_release_manifest,
            "service_node": bool(profile.get("service_node")),
            "operator_profile": profile.get("operator_profile"),
            "monitoring_profile": profile.get("monitoring_profile"),
            "dashboard_enabled": bool(profile.get("dashboard_enabled")),
            "monitoring_enabled": bool(profile.get("monitoring_enabled")),
            "intentkit_enabled": bool(profile.get("intentkit_enabled")),
            "intentkit_network_id": profile.get("intentkit_network_id"),
            "dex_automation_enabled": bool(
                profile.get("dex_automation_enabled")
            ),
        },
    }
    result["node_release_manifest"] = node_release_manifest
    if stack_dir is not None:
        result["stack_dir"] = str(stack_dir)

    if node_key_path.exists():
        try:
            result["node_id"] = read_json(node_key_path).get("node_id")
        except json.JSONDecodeError:
            result["node_id"] = None

    if stack_dir is not None and check_backend:
        try:
            backend_status = get_xian_stack_node_status(
                stack_dir=stack_dir,
                cometbft_home=home,
                **_stack_runtime_profile_kwargs(profile, context["network"]),
            )
            result["backend_status"] = backend_status
            result["backend_checked"] = True
            result["backend_running"] = backend_status.get("backend_running")
            if result.get("node_id") is None:
                result["node_id"] = backend_status.get("node_id")
        except Exception as exc:
            result["backend_checked"] = True
            result["backend_error"] = str(exc)

    if check_rpc:
        try:
            result["rpc_status"] = fetch_json(args.rpc_url)
            result["rpc_reachable"] = True
        except Exception as exc:
            result["rpc_reachable"] = False
            result["rpc_error"] = str(exc)

    if isinstance(result.get("backend_status"), dict) and isinstance(
        result["backend_status"].get("endpoints"), dict
    ):
        result["endpoints"] = result["backend_status"]["endpoints"]
    else:
        result["endpoints"] = _fallback_node_endpoints(
            rpc_status_url=args.rpc_url,
            profile=profile,
        )

    result["summary"] = _summarize_node_status(result)
    return result


def _handle_node_status(args: argparse.Namespace) -> int:
    result = _collect_node_status(args, check_rpc=not args.skip_rpc)
    print(json.dumps(result, indent=2))
    return 0


def _handle_node_endpoints(args: argparse.Namespace) -> int:
    result = _collect_node_endpoints(args)
    print(json.dumps(result, indent=2))
    return 0


def _read_rendered_config_toml(home: Path) -> dict[str, object]:
    config_path = home / "config" / "config.toml"
    if not config_path.exists():
        raise FileNotFoundError(f"{config_path} does not exist")
    try:
        return tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in {config_path}") from exc


def _collect_statesync_readiness(home: Path) -> dict[str, object]:
    config = _read_rendered_config_toml(home)
    statesync = config.get("statesync", {})
    enabled = bool(statesync.get("enable"))
    rpc_servers = [
        server.strip()
        for server in str(statesync.get("rpc_servers", "")).split(",")
        if server.strip()
    ]
    trust_height = int(statesync.get("trust_height", 0) or 0)
    trust_hash = str(statesync.get("trust_hash", "") or "")
    trust_period = str(statesync.get("trust_period", "") or "")

    if not enabled:
        state = "disabled"
        ready = False
    else:
        ready = (
            len(rpc_servers) >= 2
            and trust_height > 0
            and bool(trust_hash)
            and bool(trust_period)
        )
        state = "configured" if ready else "incomplete"

    return {
        "enabled": enabled,
        "state": state,
        "ready": ready,
        "rpc_servers": rpc_servers,
        "trust_height": trust_height,
        "trust_hash_present": bool(trust_hash),
        "trust_period": trust_period,
    }


def _collect_node_health(args: argparse.Namespace) -> dict[str, object]:
    context = _resolve_node_context(args)
    profile = context["profile"]
    network = context["network"]
    stack_dir = context["stack_dir"]
    home = context["home"]
    node_image_mode, node_integrated_image, node_split_image = (
        _effective_node_image_config(profile, network)
    )

    payload: dict[str, object] = {
        "profile_path": str(context["profile_path"]),
        "network_path": str(context["network_path"]),
        "node_image_mode": node_image_mode,
        "node_integrated_image": node_integrated_image,
        "node_split_image": node_split_image,
        "home": str(home),
        "service_node": bool(profile.get("service_node")),
        "operator_profile": profile.get("operator_profile"),
        "monitoring_profile": profile.get("monitoring_profile"),
        "dashboard_enabled": bool(profile.get("dashboard_enabled")),
        "monitoring_enabled": bool(profile.get("monitoring_enabled")),
        "intentkit_enabled": bool(profile.get("intentkit_enabled")),
        "intentkit_network_id": profile.get("intentkit_network_id"),
        "dex_automation_enabled": bool(profile.get("dex_automation_enabled")),
        "effective_snapshot_url": _resolve_effective_snapshot_url(
            profile=profile,
            network=context["network"],
        ),
    }
    if stack_dir is not None:
        payload["stack_dir"] = str(stack_dir)

    try:
        payload["statesync"] = _collect_statesync_readiness(home)
    except Exception as exc:
        payload["statesync"] = {
            "state": "unavailable",
            "error": str(exc),
        }

    if stack_dir is not None:
        payload["health"] = get_xian_stack_node_health(
            stack_dir=stack_dir,
            cometbft_home=home,
            **_stack_runtime_profile_kwargs(profile, network),
            rpc_url=args.rpc_url,
            check_disk=not args.skip_disk_check,
        )
        payload["endpoints"] = payload["health"].get("endpoints", {})
    else:
        payload["endpoints"] = _fallback_node_endpoints(
            rpc_status_url=args.rpc_url,
            profile=profile,
        )
    return payload


def _handle_node_health(args: argparse.Namespace) -> int:
    result = _collect_node_health(args)
    print(json.dumps(result, indent=2))
    return 0


def _handle_snapshot_restore(args: argparse.Namespace) -> int:
    base_dir = args.base_dir.resolve()
    profile_path, profile, _, network = _load_profile_and_network(
        base_dir=base_dir,
        name=args.name,
        profile_arg=args.profile,
        network_arg=args.network,
        configs_dir=args.configs_dir,
    )
    stack_dir = _resolve_stack_dir_from_profile(
        base_dir=base_dir,
        profile=profile,
        explicit_stack_dir=args.stack_dir,
    )
    result = _restore_snapshot(
        base_dir=base_dir,
        profile=profile,
        profile_path=profile_path,
        network=network,
        stack_dir=stack_dir,
        explicit_home=args.home,
        explicit_snapshot_url=args.snapshot_url,
    )
    print(json.dumps(result, indent=2))
    return 0
