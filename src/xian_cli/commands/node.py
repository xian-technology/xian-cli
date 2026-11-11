from __future__ import annotations

import argparse
import json
import tomllib
from dataclasses import dataclass, field
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
from xian_cli.commands.node_options import node_config_options
from xian_cli.commands.node_options import service_config as _service_config
from xian_cli.commands.node_status import profile_service_summary as _profile_service_summary
from xian_cli.commands.node_status import summarize_node_status
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


@dataclass(frozen=True, slots=True)
class _NodeInitialization:
    base_dir: Path
    profile_path: Path
    profile: dict = field(repr=False)
    network: dict = field(repr=False)
    stack_dir: Path | None
    validator_key_payload: dict = field(repr=False)
    genesis: dict = field(repr=False)
    effective_genesis_source: str
    home: Path


def _load_validator_key(
    args: argparse.Namespace, *, base_dir: Path, profile_path: Path, profile: dict
) -> dict:
    explicit_key = args.validator_key
    if explicit_key is not None and not explicit_key.is_absolute():
        explicit_key = (base_dir / explicit_key).resolve()
    reference = explicit_key or _resolve_path(
        profile.get("validator_key_ref"), base_dir=base_dir, fallback_dir=profile_path.parent
    )
    if reference is None or not reference.exists():
        raise FileNotFoundError(
            "validator key reference is required; set validator_key_ref in the "
            "node profile or pass --validator-key"
        )
    return _extract_priv_validator_key(read_json(reference))


def _resolve_initialization(args: argparse.Namespace) -> _NodeInitialization:
    base_dir = args.base_dir.resolve()
    profile_path, profile, network_path, network = _load_profile_and_network(
        base_dir=base_dir,
        name=args.name,
        profile_arg=args.profile,
        network_arg=args.network,
        configs_dir=args.configs_dir,
    )
    stack_dir = _resolve_stack_dir_from_profile(
        base_dir=base_dir, profile=profile, explicit_stack_dir=args.stack_dir
    )
    key = _load_validator_key(args, base_dir=base_dir, profile_path=profile_path, profile=profile)
    genesis, source = _resolve_effective_genesis_payload(
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
    return _NodeInitialization(
        base_dir=base_dir,
        profile_path=profile_path,
        profile=profile,
        network=network,
        stack_dir=stack_dir,
        validator_key_payload=key,
        genesis=genesis,
        effective_genesis_source=source,
        home=home,
    )


def _restore_initialization_snapshot(
    result: dict, plan: _NodeInitialization, args: argparse.Namespace
) -> None:
    result["effective_snapshot_url"] = _resolve_effective_snapshot_url(
        profile=plan.profile,
        network=plan.network,
        explicit_snapshot_url=getattr(args, "snapshot_url", None),
    )
    result["effective_genesis_source"] = plan.effective_genesis_source
    result["snapshot_restored"] = False
    if getattr(args, "restore_snapshot", False):
        snapshot = _restore_snapshot(
            base_dir=plan.base_dir,
            profile=plan.profile,
            profile_path=plan.profile_path,
            network=plan.network,
            stack_dir=plan.stack_dir,
            explicit_home=plan.home,
            explicit_snapshot_url=getattr(args, "snapshot_url", None),
        )
        result["snapshot_restored"] = True
        result["snapshot"] = snapshot


def _initialize_node_from_args(args: argparse.Namespace) -> dict:
    node_setup = get_node_setup_module()
    plan = _resolve_initialization(args)
    configs = node_setup.render_node_configs(
        options=node_config_options(node_setup, profile=plan.profile, network=plan.network)
    )
    result = node_setup.materialize_cometbft_home(
        home=plan.home,
        config=configs["cometbft"],
        xian_config=configs["xian"],
        genesis=plan.genesis,
        priv_validator_key=plan.validator_key_payload,
        overwrite=args.force,
    )
    _restore_initialization_snapshot(result, plan, args)
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
    return summarize_node_status(result, block_age_seconds=_block_age_seconds)


def _initial_node_status(context: dict, *, name: str, check_rpc: bool) -> dict:
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
            "name": name,
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

    return result


def _collect_backend_status(result: dict, context: dict) -> None:
    try:
        backend_status = get_xian_stack_node_status(
            stack_dir=context["stack_dir"],
            cometbft_home=context["home"],
            **_stack_runtime_profile_kwargs(context["profile"], context["network"]),
        )
        result["backend_status"] = backend_status
        result["backend_checked"] = True
        result["backend_running"] = backend_status.get("backend_running")
        if result.get("node_id") is None:
            result["node_id"] = backend_status.get("node_id")
    except Exception as exc:
        result["backend_checked"] = True
        result["backend_error"] = str(exc)


def _collect_rpc_status(result: dict, rpc_url: str) -> None:
    try:
        result["rpc_status"] = fetch_json(rpc_url)
        result["rpc_reachable"] = True
    except Exception as exc:
        result["rpc_reachable"] = False
        result["rpc_error"] = str(exc)


def _status_endpoints(result: dict, *, rpc_url: str, profile: dict) -> dict:
    if isinstance(result.get("backend_status"), dict) and isinstance(
        result["backend_status"].get("endpoints"), dict
    ):
        return result["backend_status"]["endpoints"]
    return _fallback_node_endpoints(
        rpc_status_url=rpc_url,
        profile=profile,
    )


def _collect_node_status(
    args: argparse.Namespace, *, check_rpc: bool, check_backend: bool = True
) -> dict:
    context = _resolve_node_context(args)
    result = _initial_node_status(context, name=args.name, check_rpc=check_rpc)
    if context["stack_dir"] is not None and check_backend:
        _collect_backend_status(result, context)
    if check_rpc:
        _collect_rpc_status(result, args.rpc_url)
    result["endpoints"] = _status_endpoints(
        result, rpc_url=args.rpc_url, profile=context["profile"]
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
