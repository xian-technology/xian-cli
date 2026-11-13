"""Translate node-profile intent to the backend's explicit configuration options.

The backend module is supplied lazily by the node command. Importing CLI help
or inspecting a profile therefore does not import node runtime dependencies.
"""

from __future__ import annotations

from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xian.node_setup import (
        AppLoggingOptions,
        BdsOptions,
        MetricsOptions,
        NodeConfigOptions,
        SimulationOptions,
        StateSyncOptions,
    )


# xian-stack publishes the profile's advanced.metrics host and port on the host
# (XIAN_APP_METRICS_HOST:XIAN_APP_METRICS_PORT) to this container address, so the
# node always listens here.
APP_METRICS_CONTAINER_HOST = "0.0.0.0"
APP_METRICS_CONTAINER_PORT = 9108
# Publishing stays local-only unless a profile names another host address.
DEFAULT_APP_METRICS_HOST = "127.0.0.1"


def profile_config(profile: dict, section: str, name: str) -> dict:
    values = profile.get(section)
    if not isinstance(values, dict):
        return {}
    value = values.get(name)
    return value if isinstance(value, dict) else {}


def service_config(profile: dict, name: str) -> dict:
    return profile_config(profile, "services", name)


def _peer_values(profile: dict, network: dict, name: str) -> tuple[str, ...]:
    network_p2p = network.get("p2p") if isinstance(network.get("p2p"), dict) else {}
    profile_p2p = profile.get("p2p") if isinstance(profile.get("p2p"), dict) else {}
    return tuple(network_p2p.get(name) or ()) + tuple(profile_p2p.get(name) or ())


def state_sync_options(node_setup: ModuleType, profile: dict) -> StateSyncOptions:
    settings = profile_config(profile, "advanced", "statesync")
    return node_setup.StateSyncOptions(
        enable=bool(settings.get("enabled", False)),
        rpc_servers=tuple(settings.get("rpc_servers") or ()),
        trust_height=int(settings.get("trust_height", 0)),
        trust_hash=str(settings.get("trust_hash", "")),
        trust_period=str(settings.get("trust_period", "168h0m0s")),
    )


def app_metrics_host_port(profile: dict) -> int:
    """The host port the stack publishes the node's app metrics on."""
    settings = profile_config(profile, "advanced", "metrics")
    return int(settings.get("port", APP_METRICS_CONTAINER_PORT))


def app_metrics_host_address(profile: dict) -> str:
    """The host address the stack binds the node's published app metrics to."""
    settings = profile_config(profile, "advanced", "metrics")
    return str(settings.get("host", DEFAULT_APP_METRICS_HOST))


def metrics_options(node_setup: ModuleType, profile: dict) -> MetricsOptions:
    settings = profile_config(profile, "advanced", "metrics")
    return node_setup.MetricsOptions(
        enabled=bool(settings.get("enabled", True)),
        host=APP_METRICS_CONTAINER_HOST,
        port=APP_METRICS_CONTAINER_PORT,
        bds_refresh_seconds=float(settings.get("bds_refresh_seconds", 5.0)),
    )


def app_logging_options(node_setup: ModuleType, profile: dict) -> AppLoggingOptions:
    return node_setup.AppLoggingOptions(
        level=str(profile.get("app_log_level", "INFO")),
        json_logging=bool(profile.get("app_log_json", False)),
        rotation_hours=int(profile.get("app_log_rotation_hours", 1)),
        retention_days=int(profile.get("app_log_retention_days", 7)),
        max_file_mb=int(profile.get("app_log_max_file_mb", 50)),
        max_total_mb=int(profile.get("app_log_max_total_mb", 250)),
    )


def simulation_options(node_setup: ModuleType, profile: dict) -> SimulationOptions:
    return node_setup.SimulationOptions(
        enabled=bool(profile.get("simulation_enabled", True)),
        max_concurrency=int(profile.get("simulation_max_concurrency", 2)),
        timeout_ms=int(profile.get("simulation_timeout_ms", 3000)),
        max_chi=profile.get("simulation_max_chi"),
    )


def bds_options(node_setup: ModuleType, profile: dict) -> BdsOptions:
    settings = profile_config(profile, "services", "bds")
    return node_setup.BdsOptions(
        dsn=str(settings.get("dsn", "")),
        host=str(settings.get("host", "")),
        port=int(settings.get("port", 5432)),
        database=str(settings.get("database", "xian")),
        user=str(settings.get("user", "")),
        password=str(settings.get("password", "")),
        pool_min_size=int(settings.get("pool_min_size", 1)),
        pool_max_size=int(settings.get("pool_max_size", 10)),
        statement_timeout_ms=int(settings.get("statement_timeout_ms", 0)),
        acquire_timeout_ms=int(settings.get("acquire_timeout_ms", 10000)),
        application_name=str(settings.get("application_name", "xian-bds")),
        queue_max_size=int(settings.get("queue_max_size", 128)),
        catchup_enabled=bool(settings.get("catchup_enabled", True)),
        catchup_poll_seconds=float(settings.get("catchup_poll_seconds", 1.0)),
        rpc_url=str(settings.get("rpc_url") or ""),
        spool_dir=str(settings.get("spool_dir", "")),
        spool_warn_entries=int(settings.get("spool_warn_entries", 256)),
        spool_warn_bytes=int(settings.get("spool_warn_bytes", 536_870_912)),
        disk_free_warn_bytes=int(settings.get("disk_free_warn_bytes", 2_147_483_648)),
    )


def node_config_options(
    node_setup: ModuleType, *, profile: dict, network: dict
) -> NodeConfigOptions:
    advanced_cometbft = profile_config(profile, "advanced", "cometbft")
    advanced_pending_nonce = profile_config(profile, "advanced", "pending_nonce")
    return node_setup.NodeConfigOptions(
        moniker=profile["moniker"],
        p2p_seeds=_peer_values(profile, network, "seeds"),
        p2p_persistent_peers=_peer_values(profile, network, "persistent_peers"),
        allow_cors=bool(advanced_cometbft.get("allow_cors", True)),
        bds_enabled=bool(service_config(profile, "bds").get("enabled")),
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
        statesync=state_sync_options(node_setup, profile),
        metrics=metrics_options(node_setup, profile),
        app_logging=app_logging_options(node_setup, profile),
        simulation=simulation_options(node_setup, profile),
        pending_nonce_reservation_ttl_seconds=float(
            advanced_pending_nonce.get("reservation_ttl_seconds", 60.0)
        ),
        max_pending_nonces_per_sender=int(advanced_pending_nonce.get("max_per_sender", 128)),
        bds=bds_options(node_setup, profile),
        proxy_app=str(advanced_cometbft.get("proxy_app", "unix:///tmp/abci.sock")),
        prometheus=bool(advanced_cometbft.get("prometheus", True)),
    )
