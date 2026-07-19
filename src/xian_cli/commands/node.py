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


def _service_config(profile: dict, name: str) -> dict:
    services = profile.get("services")
    if not isinstance(services, dict):
        return {}
    value = services.get(name)
    return value if isinstance(value, dict) else {}


def _advanced_config(profile: dict, name: str) -> dict:
    advanced = profile.get("advanced")
    if not isinstance(advanced, dict):
        return {}
    value = advanced.get(name)
    return value if isinstance(value, dict) else {}


def _profile_service_summary(profile: dict) -> dict[str, object]:
    bds = _service_config(profile, "bds")
    dashboard = _service_config(profile, "dashboard")
    monitoring = _service_config(profile, "monitoring")
    intentkit = _service_config(profile, "intentkit")
    dex_automation = _service_config(profile, "dex_automation")
    shielded_relayer = _service_config(profile, "shielded_relayer")
    return {
        "bds_enabled": bool(bds.get("enabled")),
        "dashboard_enabled": bool(dashboard.get("enabled")),
        "monitoring_enabled": bool(monitoring.get("enabled")),
        "intentkit_enabled": bool(intentkit.get("enabled")),
        "intentkit_network_id": intentkit.get("network_id"),
        "dex_automation_enabled": bool(dex_automation.get("enabled")),
        "shielded_relayer_enabled": bool(shielded_relayer.get("enabled")),
    }


def _describe_effective_genesis(*, profile: dict, network: dict) -> str | None:
    genesis = profile.get("genesis") or network.get("genesis")
    if not isinstance(genesis, dict):
        return None
    if genesis.get("kind") == "source":
        source = genesis.get("source")
        return str(source) if source is not None else None
    if genesis.get("kind") == "bundle":
        bundle = genesis.get("bundle")
        return f"bundle:{bundle}" if bundle is not None else None
    return None


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
    if explicit_validator_key is not None and not explicit_validator_key.is_absolute():
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

    validator_key_payload = _extract_priv_validator_key(read_json(validator_key_ref))
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

    network_p2p = network.get("p2p") if isinstance(network.get("p2p"), dict) else {}
    profile_p2p = profile.get("p2p") if isinstance(profile.get("p2p"), dict) else {}
    p2p_seeds = list(network_p2p.get("seeds") or [])
    p2p_seeds.extend(profile_p2p.get("seeds") or [])
    p2p_persistent_peers = list(profile_p2p.get("persistent_peers") or [])
    services_bds = _service_config(profile, "bds")
    advanced_cometbft = _advanced_config(profile, "cometbft")
    advanced_statesync = _advanced_config(profile, "statesync")
    advanced_metrics = _advanced_config(profile, "metrics")
    advanced_pending_nonce = _advanced_config(profile, "pending_nonce")
    advanced_parallel = _advanced_config(profile, "parallel_execution")

    configs = node_setup.render_node_configs(
        options=node_setup.NodeConfigOptions(
            moniker=profile["moniker"],
            p2p_seeds=tuple(p2p_seeds),
            p2p_persistent_peers=tuple(p2p_persistent_peers),
            allow_cors=bool(advanced_cometbft.get("allow_cors", True)),
            bds_enabled=bool(services_bds.get("enabled")),
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
            transaction_trace_logging=bool(profile.get("transaction_trace_logging", False)),
            statesync=node_setup.StateSyncOptions(
                enable=bool(advanced_statesync.get("enabled", False)),
                rpc_servers=tuple(advanced_statesync.get("rpc_servers") or ()),
                trust_height=int(advanced_statesync.get("trust_height", 0)),
                trust_hash=str(advanced_statesync.get("trust_hash", "")),
                trust_period=str(advanced_statesync.get("trust_period", "168h0m0s")),
            ),
            metrics=node_setup.MetricsOptions(
                enabled=bool(advanced_metrics.get("enabled", True)),
                host=str(advanced_metrics.get("host", "0.0.0.0")),
                port=int(advanced_metrics.get("port", 9108)),
                bds_refresh_seconds=float(advanced_metrics.get("bds_refresh_seconds", 5.0)),
            ),
            app_logging=node_setup.AppLoggingOptions(
                level=str(profile.get("app_log_level", "INFO")),
                json_logging=bool(profile.get("app_log_json", False)),
                rotation_hours=int(profile.get("app_log_rotation_hours", 1)),
                retention_days=int(profile.get("app_log_retention_days", 7)),
            ),
            simulation=node_setup.SimulationOptions(
                enabled=bool(profile.get("simulation_enabled", True)),
                max_concurrency=int(profile.get("simulation_max_concurrency", 2)),
                timeout_ms=int(profile.get("simulation_timeout_ms", 3000)),
                max_chi=int(profile.get("simulation_max_chi", 1_000_000)),
            ),
            tx_fee_mode=str(profile.get("tx_fee_mode", "paid_metered")),
            free_tx_max_chi=int(profile.get("free_tx_max_chi", 1_000_000)),
            free_block_max_chi=int(profile.get("free_block_max_chi", 20_000_000)),
            parallel_execution=node_setup.ParallelExecutionOptions(
                enabled=bool(profile.get("parallel_execution_enabled", False)),
                workers=int(profile.get("parallel_execution_workers", 4)),
                min_transactions=int(profile.get("parallel_execution_min_transactions", 8)),
                max_speculative_waves=int(advanced_parallel.get("max_speculative_waves", 4)),
                min_wave_acceptance_ratio=float(
                    advanced_parallel.get("min_wave_acceptance_ratio", 0.25)
                ),
                low_acceptance_min_wave_size=int(
                    advanced_parallel.get("low_acceptance_min_wave_size", 8)
                ),
                warm_workers=bool(advanced_parallel.get("warm_workers", True)),
                access_estimates_enabled=bool(
                    advanced_parallel.get("access_estimates_enabled", True)
                ),
            ),
            pending_nonce_reservation_ttl_seconds=float(
                advanced_pending_nonce.get("reservation_ttl_seconds", 60.0)
            ),
            max_pending_nonces_per_sender=int(advanced_pending_nonce.get("max_per_sender", 128)),
            bds=node_setup.BdsOptions(
                dsn=str(services_bds.get("dsn", "")),
                host=str(services_bds.get("host", "")),
                port=int(services_bds.get("port", 5432)),
                database=str(services_bds.get("database", "xian")),
                user=str(services_bds.get("user", "")),
                password=str(services_bds.get("password", "")),
                pool_min_size=int(services_bds.get("pool_min_size", 1)),
                pool_max_size=int(services_bds.get("pool_max_size", 10)),
                statement_timeout_ms=int(services_bds.get("statement_timeout_ms", 0)),
                acquire_timeout_ms=int(services_bds.get("acquire_timeout_ms", 10000)),
                application_name=str(services_bds.get("application_name", "xian-bds")),
                queue_max_size=int(services_bds.get("queue_max_size", 128)),
                catchup_enabled=bool(services_bds.get("catchup_enabled", True)),
                catchup_poll_seconds=float(services_bds.get("catchup_poll_seconds", 1.0)),
                rpc_url=str(services_bds.get("rpc_url") or ""),
                spool_dir=str(services_bds.get("spool_dir", "")),
                spool_warn_entries=int(services_bds.get("spool_warn_entries", 256)),
                spool_warn_bytes=int(services_bds.get("spool_warn_bytes", 536_870_912)),
                disk_free_warn_bytes=int(services_bds.get("disk_free_warn_bytes", 2_147_483_648)),
            ),
            proxy_app=str(advanced_cometbft.get("proxy_app", "unix:///tmp/abci.sock")),
            prometheus=bool(advanced_cometbft.get("prometheus", True)),
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
            f"{config_path} does not exist; run `xian node init {args.name}` first"
        )
    if not xian_config_path.exists():
        raise FileNotFoundError(
            f"{xian_config_path} does not exist; run `xian node init {args.name}` first"
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
    hostname = hostname.strip()
    if ":" in hostname and not hostname.startswith("["):
        return f"[{hostname}]"
    return hostname


def _unbracket_url_host(hostname: str) -> str:
    hostname = hostname.strip()
    if hostname.startswith("[") and hostname.endswith("]"):
        return hostname[1:-1]
    return hostname


def _display_endpoint_host(hostname: str) -> str:
    hostname = _unbracket_url_host(hostname)
    if hostname == "0.0.0.0":
        return _format_url_host("127.0.0.1")
    if hostname == "::":
        return _format_url_host("::1")
    return _format_url_host(hostname)


def _endpoint_url(*, host: str, port: int, suffix: str = "", scheme: str = "http") -> str:
    return f"{scheme}://{_display_endpoint_host(host)}:{port}{suffix}"


def _display_endpoint_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.netloc:
        return url
    try:
        port = parsed.port
    except ValueError:
        return url
    hostname = parsed.hostname
    if hostname is None:
        return url
    userinfo = ""
    if parsed.username is not None:
        userinfo = parsed.username
        if parsed.password is not None:
            userinfo = f"{userinfo}:{parsed.password}"
        userinfo = f"{userinfo}@"
    netloc = f"{userinfo}{_display_endpoint_host(hostname)}"
    if port is not None:
        netloc = f"{netloc}:{port}"
    scheme = parsed.scheme or "http"
    return parsed._replace(scheme=scheme, netloc=netloc).geturl()


def _replace_url_port(url: str, *, port: int, suffix: str = "") -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme or "http"
    return _endpoint_url(
        scheme=scheme,
        host=parsed.hostname or "127.0.0.1",
        port=port,
        suffix=suffix,
    )


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
    rpc_status_url = _display_endpoint_url(rpc_status_url)
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
    bds = _service_config(profile, "bds")
    dashboard = _service_config(profile, "dashboard")
    monitoring = _service_config(profile, "monitoring")
    intentkit = _service_config(profile, "intentkit")
    shielded_relayer = _service_config(profile, "shielded_relayer")
    if bool(dashboard.get("enabled")):
        dashboard_port = int(dashboard.get("port", 8080))
        dashboard_url = _endpoint_url(
            host=str(dashboard.get("host", "127.0.0.1")),
            port=dashboard_port,
        )
        endpoints["dashboard"] = dashboard_url
        endpoints["dashboard_status"] = f"{dashboard_url}/api/status"
    if bool(bds.get("enabled")):
        graphql_base = _replace_url_port(base_url, port=5000)
        endpoints["graphql"] = f"{graphql_base}/graphql"
        endpoints["graphiql"] = f"{graphql_base}/graphiql"
    if bool(monitoring.get("enabled")):
        endpoints["prometheus"] = _replace_url_port(base_url, port=9090)
        endpoints["grafana"] = _replace_url_port(base_url, port=3000)
    if bool(intentkit.get("enabled")):
        intentkit_port = int(intentkit.get("port", 38000))
        intentkit_api_port = int(intentkit.get("api_port", 38080))
        intentkit_host = str(intentkit.get("host", "127.0.0.1"))
        frontend_url = _endpoint_url(host=intentkit_host, port=intentkit_port)
        api_url = _endpoint_url(host=intentkit_host, port=intentkit_api_port)
        endpoints["intentkit"] = frontend_url
        endpoints["intentkit_api"] = api_url
        endpoints["intentkit_api_health"] = f"{api_url}/health"
    if bool(shielded_relayer.get("enabled")):
        relayer_port = int(shielded_relayer.get("port", 38180))
        relayer_url = _endpoint_url(
            host=str(shielded_relayer.get("host", "127.0.0.1")),
            port=relayer_port,
        )
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
    node_image_mode, node_integrated_image, node_split_image = _effective_node_image_config(
        profile, network
    )

    payload: dict[str, object] = {
        "profile_path": str(context["profile_path"]),
        "network_path": str(context["network_path"]),
        "node_image_mode": node_image_mode,
        "node_integrated_image": node_integrated_image,
        "node_split_image": node_split_image,
        "operator_profile": profile.get("operator_profile"),
        "monitoring_profile": profile.get("monitoring_profile"),
        **_profile_service_summary(profile),
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
    rpc_result = rpc_payload.get("result", {}) if isinstance(rpc_payload, dict) else {}
    sync_info = rpc_result.get("sync_info", {}) if isinstance(rpc_result, dict) else {}
    node_info = rpc_result.get("node_info", {}) if isinstance(rpc_result, dict) else {}
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
        "operator_profile": result.get("profile", {}).get("operator_profile"),
        "monitoring_profile": result.get("profile", {}).get("monitoring_profile"),
        **_profile_service_summary(result.get("profile", {})),
        "backend_running": result.get("backend_running"),
        "rpc_reachable": result.get("rpc_reachable"),
        "rpc_height": sync_info.get("latest_block_height"),
        "rpc_latest_block_time": sync_info.get("latest_block_time"),
        "rpc_block_age_seconds": _block_age_seconds(sync_info.get("latest_block_time")),
        "rpc_catching_up": sync_info.get("catching_up"),
        "rpc_network": node_info.get("network"),
        "peer_count": other.get("n_peers"),
        "node_image_mode": result.get("profile", {}).get("node_image_mode"),
        "node_integrated_image": result.get("profile", {}).get("node_integrated_image"),
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
                "s6_overlay_noarch_sha256": build.get("s6_overlay_noarch_sha256"),
                "s6_overlay_x86_64_sha256": build.get("s6_overlay_x86_64_sha256"),
                "s6_overlay_aarch64_sha256": build.get("s6_overlay_aarch64_sha256"),
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
            summary["dashboard_reachable"] = backend_status.get("dashboard_reachable")
        if summary["monitoring_enabled"]:
            summary["prometheus_reachable"] = backend_status.get("prometheus_reachable")
            summary["grafana_reachable"] = backend_status.get("grafana_reachable")
        if summary["bds_enabled"]:
            summary["graphiql_reachable"] = backend_status.get("graphiql_reachable")
        if summary["intentkit_enabled"]:
            summary["intentkit_running"] = backend_status.get("intentkit_running")
            summary["intentkit_reachable"] = backend_status.get("intentkit_reachable")
            summary["intentkit_api_reachable"] = backend_status.get("intentkit_api_reachable")
        if summary["dex_automation_enabled"]:
            summary["dex_automation_running"] = backend_status.get("dex_automation_running")
            summary["dex_automation_reachable"] = backend_status.get("dex_automation_reachable")
        if summary["shielded_relayer_enabled"]:
            summary["shielded_relayer_running"] = backend_status.get("shielded_relayer_running")
            summary["shielded_relayer_reachable"] = backend_status.get("shielded_relayer_reachable")
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
    node_image_mode, node_integrated_image, node_split_image = _effective_node_image_config(
        profile, network
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
        "effective_genesis_source": _describe_effective_genesis(
            profile=profile,
            network=network,
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
            "operator_profile": profile.get("operator_profile"),
            "monitoring_profile": profile.get("monitoring_profile"),
            "services": profile.get("services"),
            **_profile_service_summary(profile),
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
            len(rpc_servers) >= 2 and trust_height > 0 and bool(trust_hash) and bool(trust_period)
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
    node_image_mode, node_integrated_image, node_split_image = _effective_node_image_config(
        profile, network
    )

    payload: dict[str, object] = {
        "profile_path": str(context["profile_path"]),
        "network_path": str(context["network_path"]),
        "node_image_mode": node_image_mode,
        "node_integrated_image": node_integrated_image,
        "node_split_image": node_split_image,
        "home": str(home),
        "operator_profile": profile.get("operator_profile"),
        "monitoring_profile": profile.get("monitoring_profile"),
        **_profile_service_summary(profile),
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
