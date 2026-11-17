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
    defaults = node_setup.StateSyncOptions()
    settings = profile_config(profile, "advanced", "statesync")
    return node_setup.StateSyncOptions(
        enable=bool(settings.get("enabled", defaults.enable)),
        rpc_servers=tuple(settings.get("rpc_servers") or ()),
        trust_height=int(settings.get("trust_height", defaults.trust_height)),
        trust_hash=str(settings.get("trust_hash", defaults.trust_hash)),
        trust_period=str(settings.get("trust_period", defaults.trust_period)),
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
    defaults = node_setup.MetricsOptions()
    settings = profile_config(profile, "advanced", "metrics")
    return node_setup.MetricsOptions(
        enabled=bool(settings.get("enabled", defaults.enabled)),
        host=APP_METRICS_CONTAINER_HOST,
        port=APP_METRICS_CONTAINER_PORT,
        bds_refresh_seconds=float(
            settings.get("bds_refresh_seconds", defaults.bds_refresh_seconds)
        ),
    )


def app_logging_options(node_setup: ModuleType, profile: dict) -> AppLoggingOptions:
    defaults = node_setup.AppLoggingOptions()
    return node_setup.AppLoggingOptions(
        level=str(profile.get("app_log_level", defaults.level)),
        json_logging=bool(profile.get("app_log_json", defaults.json_logging)),
        rotation_hours=int(profile.get("app_log_rotation_hours", defaults.rotation_hours)),
        retention_days=int(profile.get("app_log_retention_days", defaults.retention_days)),
        max_file_mb=int(profile.get("app_log_max_file_mb", defaults.max_file_mb)),
        max_total_mb=int(profile.get("app_log_max_total_mb", defaults.max_total_mb)),
    )


def simulation_options(node_setup: ModuleType, profile: dict) -> SimulationOptions:
    defaults = node_setup.SimulationOptions()
    return node_setup.SimulationOptions(
        enabled=bool(profile.get("simulation_enabled", defaults.enabled)),
        max_concurrency=int(profile.get("simulation_max_concurrency", defaults.max_concurrency)),
        timeout_ms=int(profile.get("simulation_timeout_ms", defaults.timeout_ms)),
        max_chi=profile.get("simulation_max_chi", defaults.max_chi),
    )


def bds_options(node_setup: ModuleType, profile: dict) -> BdsOptions:
    defaults = node_setup.BdsOptions()
    settings = profile_config(profile, "services", "bds")
    # An unset host, user or catch-up RPC URL leaves the backend's default in
    # place: the xian-stack Postgres service and the node's own RPC. The
    # password is never a profile value; the node reads the secret file the
    # stack mounts (BdsOptions.password_file).
    connection = {
        key: str(settings[key]) for key in ("host", "user", "rpc_url") if settings.get(key)
    }
    return node_setup.BdsOptions(
        dsn=str(settings.get("dsn", defaults.dsn)),
        port=int(settings.get("port", defaults.port)),
        database=str(settings.get("database", defaults.database)),
        **connection,
        pool_min_size=int(settings.get("pool_min_size", defaults.pool_min_size)),
        pool_max_size=int(settings.get("pool_max_size", defaults.pool_max_size)),
        statement_timeout_ms=int(
            settings.get("statement_timeout_ms", defaults.statement_timeout_ms)
        ),
        acquire_timeout_ms=int(settings.get("acquire_timeout_ms", defaults.acquire_timeout_ms)),
        application_name=str(settings.get("application_name", defaults.application_name)),
        queue_max_size=int(settings.get("queue_max_size", defaults.queue_max_size)),
        catchup_enabled=bool(settings.get("catchup_enabled", defaults.catchup_enabled)),
        catchup_poll_seconds=float(
            settings.get("catchup_poll_seconds", defaults.catchup_poll_seconds)
        ),
        spool_dir=str(settings.get("spool_dir", defaults.spool_dir)),
        spool_warn_entries=int(settings.get("spool_warn_entries", defaults.spool_warn_entries)),
        spool_warn_bytes=int(settings.get("spool_warn_bytes", defaults.spool_warn_bytes)),
        disk_free_warn_bytes=int(
            settings.get("disk_free_warn_bytes", defaults.disk_free_warn_bytes)
        ),
    )


def node_config_options(
    node_setup: ModuleType, *, profile: dict, network: dict
) -> NodeConfigOptions:
    defaults = node_setup.NodeConfigOptions(moniker=profile["moniker"])
    advanced_cometbft = profile_config(profile, "advanced", "cometbft")
    advanced_pending_nonce = profile_config(profile, "advanced", "pending_nonce")
    return node_setup.NodeConfigOptions(
        moniker=profile["moniker"],
        p2p_seeds=_peer_values(profile, network, "seeds"),
        p2p_persistent_peers=_peer_values(profile, network, "persistent_peers"),
        p2p_external_address=str(
            advanced_cometbft.get("external_address", defaults.p2p_external_address)
        ),
        allow_cors=bool(advanced_cometbft.get("allow_cors", defaults.allow_cors)),
        bds_enabled=bool(service_config(profile, "bds").get("enabled", defaults.bds_enabled)),
        enable_pruning=bool(profile.get("pruning_enabled", defaults.enable_pruning)),
        blocks_to_keep=int(profile.get("blocks_to_keep", defaults.blocks_to_keep)),
        block_policy_mode=str(
            profile.get(
                "block_policy_mode",
                network.get("block_policy_mode", defaults.block_policy_mode),
            )
        ),
        block_policy_interval=str(
            profile.get(
                "block_policy_interval",
                network.get("block_policy_interval", defaults.block_policy_interval),
            )
        ),
        transaction_trace_logging=bool(
            profile.get("transaction_trace_logging", defaults.transaction_trace_logging)
        ),
        statesync=state_sync_options(node_setup, profile),
        metrics=metrics_options(node_setup, profile),
        app_logging=app_logging_options(node_setup, profile),
        simulation=simulation_options(node_setup, profile),
        pending_nonce_reservation_ttl_seconds=float(
            advanced_pending_nonce.get(
                "reservation_ttl_seconds", defaults.pending_nonce_reservation_ttl_seconds
            )
        ),
        max_pending_nonces_per_sender=int(
            advanced_pending_nonce.get("max_per_sender", defaults.max_pending_nonces_per_sender)
        ),
        bds=bds_options(node_setup, profile),
        proxy_app=str(advanced_cometbft.get("proxy_app", defaults.proxy_app)),
        prometheus=bool(advanced_cometbft.get("prometheus", defaults.prometheus)),
    )
