"""Pure projections of collected node observations into the status JSON contract."""

from __future__ import annotations

from collections.abc import Callable

from xian_cli.commands.node_options import service_config

_RELEASE_BUILD_FIELDS = (
    "python_image",
    "go_image",
    "cometbft_version",
    "cometbft_source_url",
    "cometbft_source_sha256",
    "s6_overlay_version",
    "s6_overlay_noarch_sha256",
    "s6_overlay_x86_64_sha256",
    "s6_overlay_aarch64_sha256",
)

_SERVICE_STATUS_FIELDS = {
    "dashboard_enabled": ("dashboard_reachable",),
    "monitoring_enabled": ("prometheus_reachable", "grafana_reachable"),
    "bds_enabled": ("graphiql_reachable",),
    "intentkit_enabled": (
        "intentkit_running",
        "intentkit_reachable",
        "intentkit_api_reachable",
    ),
    "dex_automation_enabled": ("dex_automation_running", "dex_automation_reachable"),
    "shielded_relayer_enabled": ("shielded_relayer_running", "shielded_relayer_reachable"),
}


def profile_service_summary(profile: dict) -> dict[str, object]:
    bds = service_config(profile, "bds")
    dashboard = service_config(profile, "dashboard")
    monitoring = service_config(profile, "monitoring")
    intentkit = service_config(profile, "intentkit")
    dex_automation = service_config(profile, "dex_automation")
    shielded_relayer = service_config(profile, "shielded_relayer")
    return {
        "bds_enabled": bool(bds.get("enabled")),
        "dashboard_enabled": bool(dashboard.get("enabled")),
        "monitoring_enabled": bool(monitoring.get("enabled")),
        "intentkit_enabled": bool(intentkit.get("enabled")),
        "intentkit_network_id": intentkit.get("network_id"),
        "dex_automation_enabled": bool(dex_automation.get("enabled")),
        "shielded_relayer_enabled": bool(shielded_relayer.get("enabled")),
    }


def _node_state(result: dict) -> str:
    if not result.get("initialized"):
        return "not_initialized"
    if result.get("backend_checked") and not result.get("backend_running"):
        return "stopped"
    if result.get("rpc_checked") and not result.get("rpc_reachable"):
        return "rpc_unreachable"
    return "ready"


def _rpc_summary(result: dict, block_age_seconds: Callable) -> dict:
    rpc_payload = result.get("rpc_status")
    rpc_result = rpc_payload.get("result", {}) if isinstance(rpc_payload, dict) else {}
    sync_info = rpc_result.get("sync_info", {}) if isinstance(rpc_result, dict) else {}
    node_info = rpc_result.get("node_info", {}) if isinstance(rpc_result, dict) else {}
    other = node_info.get("other", {}) if isinstance(node_info, dict) else {}
    return {
        "rpc_height": sync_info.get("latest_block_height"),
        "rpc_latest_block_time": sync_info.get("latest_block_time"),
        "rpc_block_age_seconds": block_age_seconds(sync_info.get("latest_block_time")),
        "rpc_catching_up": sync_info.get("catching_up"),
        "rpc_network": node_info.get("network"),
        "peer_count": other.get("n_peers"),
    }


def _release_summary(release_manifest: object) -> dict:
    if not isinstance(release_manifest, dict):
        return {}
    summary = {}
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
            field: build.get(field) for field in _RELEASE_BUILD_FIELDS
        }
    return summary


def _runtime_images(backend_status: dict) -> dict:
    services = backend_status.get("compose_services")
    if not isinstance(services, list):
        return {}
    return {
        str(service.get("service")): service.get("image")
        for service in services
        if isinstance(service, dict)
        and isinstance(service.get("service"), str)
        and service.get("image")
    }


def _backend_summary(backend_status: object, service_flags: dict) -> dict:
    if not isinstance(backend_status, dict):
        return {}
    summary = {}
    images = _runtime_images(backend_status)
    if images:
        summary["runtime_service_images"] = images
    for enabled, fields in _SERVICE_STATUS_FIELDS.items():
        if service_flags[enabled]:
            summary.update({field: backend_status.get(field) for field in fields})
    return summary


def summarize_node_status(result: dict, *, block_age_seconds: Callable) -> dict:
    profile = result.get("profile", {})
    services = profile_service_summary(profile)
    return {
        "state": _node_state(result),
        "initialized": bool(result.get("initialized")),
        "operator_profile": profile.get("operator_profile"),
        "monitoring_profile": profile.get("monitoring_profile"),
        **services,
        "backend_running": result.get("backend_running"),
        "rpc_reachable": result.get("rpc_reachable"),
        **_rpc_summary(result, block_age_seconds),
        "node_image_mode": profile.get("node_image_mode"),
        "node_integrated_image": profile.get("node_integrated_image"),
        "node_split_image": profile.get("node_split_image"),
        **_release_summary(result.get("node_release_manifest")),
        **_backend_summary(result.get("backend_status"), services),
    }
