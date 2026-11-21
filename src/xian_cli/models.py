from __future__ import annotations

import decimal
import json
import math
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path

from xian.node_defaults import (
    COMETBFT_DEFAULTS,
    DEFAULT_ABCI_PROXY_APP,
    DEFAULT_BLOCK_POLICY_INTERVAL,
    DEFAULT_BLOCK_POLICY_MODE,
    RUNTIME_DEFAULTS,
)
from xian.services.bds.config import BdsConfig
from xian.state_sync_policy import P2P_STATE_SYNC_UNSUPPORTED
from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import normalize_wire_numbers
from xian_runtime_types.time import Datetime

from xian_cli.secret_files import secure_write_text

SCHEMA_VERSION = 1
SUPPORTED_BLOCK_POLICY_MODES = {"on_demand", "idle_interval", "periodic"}
SUPPORTED_NODE_IMAGE_MODES = {"local_build", "registry"}
SUPPORTED_GENESIS_KINDS = {"source", "bundle"}
SUPPORTED_OPERATOR_PROFILES = {
    "local_development",
    "shared_network",
}
SUPPORTED_INTENTKIT_NETWORK_IDS = {
    "xian-mainnet",
    "xian-localnet",
}
SUPPORTED_SHIELDED_RELAYER_AUTH_SCHEMES = {"none", "bearer"}
SUPPORTED_SHIELDED_RELAYER_SUBMISSION_KINDS = {
    "shielded_note_relay_transfer",
    "shielded_command",
}
SUPPORTED_SHIELDED_HISTORY_COMPATIBILITY = {
    "best_effort",
    "versioned",
}
SUPPORTED_SHIELDED_HISTORY_RETENTION = {
    "operator_defined",
    "archive",
}
SUPPORTED_PRIVACY_DISCLOSURE_POLICIES = {
    "user_controlled",
    "network_governed",
}
SUPPORTED_APP_LOG_LEVELS = {
    "TRACE",
    "DEBUG",
    "INFO",
    "SUCCESS",
    "WARNING",
    "ERROR",
    "CRITICAL",
}
SUPPORTED_TX_FEE_MODES = {
    "paid_metered",
    "free_metered",
}
DEFAULT_TX_FEE_MODE = "paid_metered"
SUPPORTED_RECOVERY_ARTIFACT_KINDS = {"snapshot_url"}
SUPPORTED_RUNTIME_FEATURES = {"zk"}

DEFAULT_P2P = {
    "seeds": [],
    "persistent_peers": [],
}

_BDS_DEFAULTS = BdsConfig()

DEFAULT_SERVICES = {
    "bds": {
        # Empty connection fields defer to the backend deployment context.
        # Secret paths remain backend-owned and are never saved in profiles.
        "enabled": RUNTIME_DEFAULTS.bds_enabled,
        "dsn": _BDS_DEFAULTS.dsn or "",
        "host": "",
        "port": _BDS_DEFAULTS.port,
        "database": _BDS_DEFAULTS.database,
        "user": "",
        "pool_min_size": _BDS_DEFAULTS.pool_min_size,
        "pool_max_size": _BDS_DEFAULTS.pool_max_size,
        "statement_timeout_ms": _BDS_DEFAULTS.statement_timeout_ms,
        "acquire_timeout_ms": _BDS_DEFAULTS.acquire_timeout_ms,
        "application_name": _BDS_DEFAULTS.application_name,
        "queue_max_size": _BDS_DEFAULTS.queue_max_size,
        "catchup_enabled": _BDS_DEFAULTS.catchup_enabled,
        "catchup_poll_seconds": _BDS_DEFAULTS.catchup_poll_seconds,
        "rpc_url": None,
        "spool_dir": _BDS_DEFAULTS.spool_dir or "",
        "spool_warn_entries": _BDS_DEFAULTS.spool_warn_entries,
        "spool_warn_bytes": _BDS_DEFAULTS.spool_warn_bytes,
        "disk_free_warn_bytes": _BDS_DEFAULTS.disk_free_warn_bytes,
    },
    "dashboard": {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 8080,
    },
    "monitoring": {
        "enabled": False,
    },
    "intentkit": {
        "enabled": False,
        "network_id": None,
        "host": "127.0.0.1",
        "port": 38000,
        "api_port": 38080,
    },
    "dex_automation": {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 38280,
        "config": None,
    },
    "shielded_relayer": {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 38180,
    },
}

# Templates choose which services run; BDS connection and tuning settings are
# node-specific and live only in node profiles.
DEFAULT_TEMPLATE_SERVICES = {
    **DEFAULT_SERVICES,
    "bds": {"enabled": RUNTIME_DEFAULTS.bds_enabled},
}

DEFAULT_ADVANCED_RUNTIME = {
    "cometbft": {
        "allow_cors": bool(COMETBFT_DEFAULTS["rpc"]["cors_allowed_origins"]),
        "prometheus": COMETBFT_DEFAULTS["instrumentation"]["prometheus"],
        "proxy_app": DEFAULT_ABCI_PROXY_APP,
        "external_address": COMETBFT_DEFAULTS["p2p"]["external_address"],
    },
    "metrics": {
        "enabled": RUNTIME_DEFAULTS.metrics_enabled,
        # Profile metrics addresses describe host publication, not the container listener.
        "host": RUNTIME_DEFAULTS.metrics_host,
        "port": RUNTIME_DEFAULTS.metrics_port,
        "bds_refresh_seconds": RUNTIME_DEFAULTS.metrics_bds_refresh_seconds,
    },
    "pending_nonce": {
        "reservation_ttl_seconds": RUNTIME_DEFAULTS.pending_nonce_reservation_ttl_seconds,
        "max_per_sender": RUNTIME_DEFAULTS.max_pending_nonces_per_sender,
    },
}


def _require_str(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _require_text(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _require_optional_str(payload: dict, key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string when provided")
    return value


def _require_sha256(payload: dict, key: str) -> str:
    value = _require_str(payload, key).lower()
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{key} must be a 64-character lowercase hex sha256")
    return value


def _require_optional_sha256(payload: dict, key: str) -> str | None:
    if payload.get(key) is None:
        return None
    return _require_sha256(payload, key)


def _require_optional_choice(
    payload: dict,
    key: str,
    *,
    supported: set[str],
    default: str | None = None,
) -> str | None:
    value = payload.get(key, default)
    if value is None:
        return None
    if not isinstance(value, str) or value not in supported:
        raise ValueError(f"{key} must be one of {sorted(supported)}")
    return value


def _require_tx_fee_mode(payload: dict, key: str) -> str:
    return _require_optional_choice(
        payload,
        key,
        supported=SUPPORTED_TX_FEE_MODES,
        default=DEFAULT_TX_FEE_MODE,
    )


FEE_POLICY_FIELDS = ("tx_fee_mode", "max_tx_chi", "max_block_chi")
CHI_CAP_FIELDS = ("max_tx_chi", "max_block_chi")


def canonical_fee_policy(settings: dict) -> dict:
    """Return the genesis ``__fee_policy`` form, with the node's defaults and bounds."""
    # The node runtime owns the fee policy rules (default caps, bounds, the VM
    # ceiling); importing it lazily keeps CLI help free of runtime imports.
    from xian.fee_policy import fee_policy_state

    return fee_policy_state(settings)


def _normalize_manifest_fee_policy(payload: dict, genesis: dict) -> dict | None:
    value = payload.get("fee_policy")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("fee_policy must be an object")
    if genesis["kind"] != "bundle":
        raise ValueError(
            "fee_policy only applies to bundle genesis; a source genesis already "
            "carries its fee policy"
        )
    return canonical_fee_policy(value)


def _reject_node_local_fee_settings(payload: dict) -> None:
    configured = sorted(set(payload) & set(FEE_POLICY_FIELDS))
    if configured:
        raise ValueError(
            f"node profile must not set {', '.join(configured)}: the fee policy is "
            "chain state chosen at genesis; set fee_policy in the network manifest"
        )


def _require_bool(payload: dict, key: str, *, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _normalize_runtime_features(
    payload: dict,
    key: str = "runtime_features",
) -> dict[str, bool] | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object when provided")
    _reject_unknown_fields(
        value,
        allowed=SUPPORTED_RUNTIME_FEATURES,
        label=key,
    )
    return {
        feature: _require_bool(value, feature, default=False)
        for feature in sorted(SUPPORTED_RUNTIME_FEATURES)
        if feature in value
    }


def _require_int(payload: dict, key: str, *, default: int) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _require_float(payload: dict, key: str, *, default: float) -> float:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{key} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{key} must be a finite number")
    return number


def _require_non_negative_int(payload: dict, key: str, *, default: int) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def _require_positive_int(payload: dict, key: str, *, default: int) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _require_positive_int_no_default(payload: dict, key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _require_optional_positive_int(
    payload: dict, key: str, *, default: int | None = None
) -> int | None:
    value = payload.get(key, default)
    if value is None:
        return None
    return _require_positive_int_no_default({key: value}, key)


def _require_str_list(payload: dict, key: str) -> list[str]:
    value = payload.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"{key} must be a list of non-empty strings")
    return value


def _require_object(
    payload: dict,
    key: str,
    *,
    default: dict | None = None,
) -> dict:
    value = payload.get(key, deepcopy(default or {}))
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


def _require_optional_object(payload: dict, key: str) -> dict | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object when provided")
    return value


def _require_port(payload: dict, key: str, *, default: int) -> int:
    value = _require_int(payload, key, default=default)
    if value < 1 or value > 65535:
        raise ValueError(f"{key} must be between 1 and 65535")
    return value


def _merge_defaults(defaults: dict, value: dict | None) -> dict:
    merged = deepcopy(defaults)
    if value is None:
        return merged
    for key, nested in value.items():
        if isinstance(nested, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_defaults(merged[key], nested)
        else:
            merged[key] = nested
    return merged


def _normalize_release_components(components_raw: object, key: str) -> dict[str, dict[str, str]]:
    if not isinstance(components_raw, dict) or not components_raw:
        raise ValueError(f"{key}.components must be a non-empty object")
    components: dict[str, dict[str, str]] = {}
    for component_name, component in components_raw.items():
        if not isinstance(component_name, str) or not component_name:
            raise ValueError(f"{key}.components keys must be non-empty strings")
        if not isinstance(component, dict):
            raise ValueError(f"{key}.components.{component_name} must be an object")
        components[component_name] = {
            "repository": _require_str(component, "repository"),
            "ref": _require_str(component, "ref"),
        }
    return components


def _normalize_upstream_source_commit(build: dict) -> dict[str, str]:
    if "cometbft_source_commit" not in build:
        return {}
    commit = build["cometbft_source_commit"]
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("cometbft_source_commit must be a lowercase 40-character hex string")
    return {"cometbft_source_commit": commit}


def _normalize_node_release_manifest(
    payload: dict,
    key: str,
) -> dict | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object when provided")
    if type(value.get("schema_version")) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise ValueError(f"{key}.schema_version must be {SCHEMA_VERSION}")

    components = _normalize_release_components(value.get("components"), key)

    build_raw = value.get("build")
    if not isinstance(build_raw, dict):
        raise ValueError(f"{key}.build must be an object")
    build = {
        "python_image": _require_str(build_raw, "python_image"),
        "go_image": _require_str(build_raw, "go_image"),
        "cometbft_version": _require_str(build_raw, "cometbft_version"),
        "cometbft_source_url": _require_str(build_raw, "cometbft_source_url"),
        "cometbft_source_sha256": _require_sha256(build_raw, "cometbft_source_sha256"),
        "s6_overlay_version": _require_str(build_raw, "s6_overlay_version"),
        "s6_overlay_noarch_sha256": _require_sha256(build_raw, "s6_overlay_noarch_sha256"),
        "s6_overlay_x86_64_sha256": _require_sha256(build_raw, "s6_overlay_x86_64_sha256"),
        "s6_overlay_aarch64_sha256": _require_sha256(build_raw, "s6_overlay_aarch64_sha256"),
    }

    build.update(_normalize_upstream_source_commit(build_raw))

    images_raw = value.get("images")
    if not isinstance(images_raw, dict):
        raise ValueError(f"{key}.images must be an object")
    images = {"integrated": _require_str(images_raw, "integrated")}

    return {
        "schema_version": SCHEMA_VERSION,
        "components": components,
        "build": build,
        "images": images,
    }


def _normalize_shielded_relayer_entry(
    payload: dict,
    key: str,
    *,
    default_id: str,
) -> dict:
    if not isinstance(payload, dict):
        raise ValueError(f"{key} must be an object")
    submission_kinds = _require_str_list(payload, "submission_kinds")
    if submission_kinds:
        invalid_kinds = sorted(set(submission_kinds) - SUPPORTED_SHIELDED_RELAYER_SUBMISSION_KINDS)
        if invalid_kinds:
            raise ValueError(f"{key}.submission_kinds contains unsupported values: {invalid_kinds}")
    return {
        "id": _require_optional_str(payload, "id") or default_id,
        "base_url": _require_str(payload, "base_url"),
        "auth_scheme": _require_optional_choice(
            payload,
            "auth_scheme",
            supported=SUPPORTED_SHIELDED_RELAYER_AUTH_SCHEMES,
            default="none",
        )
        or "none",
        "public_info": _require_bool(payload, "public_info", default=True),
        "public_quote": _require_bool(payload, "public_quote", default=False),
        "public_job_lookup": _require_bool(payload, "public_job_lookup", default=False),
        "priority": _require_non_negative_int(payload, "priority", default=100),
        "submission_kinds": (
            submission_kinds
            if submission_kinds
            else sorted(SUPPORTED_SHIELDED_RELAYER_SUBMISSION_KINDS)
        ),
    }


def _normalize_shielded_relayers_manifest(
    payload: dict,
) -> list[dict]:
    relayers_raw = payload.get("shielded_relayers")
    if relayers_raw is None:
        return []
    if not isinstance(relayers_raw, list):
        raise ValueError("shielded_relayers must be a list when provided")
    relayers = [
        _normalize_shielded_relayer_entry(
            item,
            f"shielded_relayers[{index}]",
            default_id=f"relayer-{index + 1}",
        )
        for index, item in enumerate(relayers_raw)
    ]
    seen_ids: set[str] = set()
    for relayer in relayers:
        relayer_id = relayer["id"]
        if relayer_id in seen_ids:
            raise ValueError(f"shielded_relayers contains duplicate id: {relayer_id}")
        seen_ids.add(relayer_id)
    sorted_relayers = sorted(
        relayers,
        key=lambda item: (
            int(item["priority"]),
            str(item["id"]),
            str(item["base_url"]),
        ),
    )
    return sorted_relayers


def _normalize_privacy_artifact_catalog(
    payload: dict,
    key: str,
) -> dict | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object when provided")
    return {
        "path": _require_str(value, "path"),
        "sha256": _require_sha256(value, "sha256"),
    }


def _normalize_shielded_history_policy(
    payload: dict,
    key: str,
) -> dict | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object when provided")
    compatibility_commitment = _require_optional_choice(
        value,
        "compatibility_commitment",
        supported=SUPPORTED_SHIELDED_HISTORY_COMPATIBILITY,
    )
    if compatibility_commitment is None:
        raise ValueError(f"{key}.compatibility_commitment is required")
    retention_class = _require_optional_choice(
        value,
        "retention_class",
        supported=SUPPORTED_SHIELDED_HISTORY_RETENTION,
    )
    if retention_class is None:
        raise ValueError(f"{key}.retention_class is required")
    return {
        "feed_version": _require_positive_int_no_default(value, "feed_version"),
        "compatibility_commitment": compatibility_commitment,
        "retention_class": retention_class,
        "bds_snapshot_support": _require_bool(value, "bds_snapshot_support", default=False),
        "operator_notice": _require_str(value, "operator_notice"),
    }


def _normalize_privacy_submission_policy(
    payload: dict,
    key: str,
) -> dict | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object when provided")
    disclosure_policy = _require_optional_choice(
        value,
        "disclosure_policy",
        supported=SUPPORTED_PRIVACY_DISCLOSURE_POLICIES,
    )
    if disclosure_policy is None:
        raise ValueError(f"{key}.disclosure_policy is required")
    return {
        "disclosure_policy": disclosure_policy,
        "shared_relayer_auth_required": _require_bool(
            value, "shared_relayer_auth_required", default=False
        ),
        "hidden_sender_submission_mode": _require_str(value, "hidden_sender_submission_mode"),
        "operator_notice": _require_str(value, "operator_notice"),
    }


def _require_schema_version(payload: dict) -> int:
    schema_version = payload.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema_version: {schema_version}; expected {SCHEMA_VERSION}")
    return schema_version


def _require_schema(payload: dict, *, expected: str) -> str:
    schema = _require_str(payload, "schema")
    if schema != expected:
        raise ValueError(f"unsupported schema: {schema}; expected {expected}")
    return schema


def _reject_unknown_fields(
    payload: dict,
    *,
    allowed: set[str],
    label: str,
) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        fields = ", ".join(unknown)
        raise ValueError(f"{label} has unknown field(s): {fields}")


def _reject_unknown_object_fields(
    value: object,
    *,
    allowed: set[str],
    label: str,
) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    _reject_unknown_fields(value, allowed=allowed, label=label)


def _require_node_image_mode(payload: dict, key: str) -> str:
    value = payload.get(key, "local_build")
    if not isinstance(value, str) or value not in SUPPORTED_NODE_IMAGE_MODES:
        raise ValueError(f"{key} must be one of {sorted(SUPPORTED_NODE_IMAGE_MODES)}")
    return value


def _require_optional_node_image_mode(payload: dict, key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or value not in SUPPORTED_NODE_IMAGE_MODES:
        raise ValueError(f"{key} must be one of {sorted(SUPPORTED_NODE_IMAGE_MODES)}")
    return value


def _validate_node_image_config(
    *,
    mode: str | None,
    integrated_image: str | None,
) -> tuple[str | None, str | None]:
    if mode != "registry" and integrated_image is not None:
        raise ValueError("node_integrated_image requires node_image_mode=registry")
    if mode == "registry" and integrated_image is None:
        raise ValueError("registry node image mode requires node_integrated_image")
    return mode, integrated_image


def _require_block_policy_mode(payload: dict, key: str) -> str:
    value = payload.get(key, DEFAULT_BLOCK_POLICY_MODE)
    if not isinstance(value, str) or value not in SUPPORTED_BLOCK_POLICY_MODES:
        raise ValueError(f"{key} must be one of {sorted(SUPPORTED_BLOCK_POLICY_MODES)}")
    return value


def _require_block_policy_interval(payload: dict, key: str) -> str:
    value = payload.get(key, DEFAULT_BLOCK_POLICY_INTERVAL)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _require_app_log_level(payload: dict, key: str) -> str:
    value = payload.get(key, RUNTIME_DEFAULTS.app_log_level)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    normalized = value.upper()
    if normalized not in SUPPORTED_APP_LOG_LEVELS:
        raise ValueError(f"{key} must be one of {sorted(SUPPORTED_APP_LOG_LEVELS)}")
    return normalized


def _normalize_genesis(payload: dict, key: str = "genesis") -> dict:
    value = _require_object(payload, key)
    kind = _require_str(value, "kind")
    if kind not in SUPPORTED_GENESIS_KINDS:
        raise ValueError(f"{key}.kind must be one of {sorted(SUPPORTED_GENESIS_KINDS)}")
    if kind == "source":
        _reject_unknown_fields(
            value,
            allowed={"kind", "source"},
            label=key,
        )
        source = _require_str(value, "source")
        if "bundle" in value or "genesis_time" in value:
            raise ValueError(f"{key}.source genesis must not include bundle or genesis_time")
        return {"kind": "source", "source": source}

    _reject_unknown_fields(
        value,
        allowed={"kind", "bundle", "genesis_time"},
        label=key,
    )
    bundle = _require_str(value, "bundle")
    if "source" in value:
        raise ValueError(f"{key}.bundle genesis must not include source")
    return {
        "kind": "bundle",
        "bundle": bundle,
        "genesis_time": _require_optional_str(value, "genesis_time"),
    }


def _normalize_optional_genesis(
    payload: dict,
    key: str = "genesis",
) -> dict | None:
    if payload.get(key) is None:
        return None
    return _normalize_genesis(payload, key)


def _normalize_p2p(payload: dict, key: str = "p2p") -> dict:
    _reject_unknown_object_fields(
        payload.get(key),
        allowed=set(DEFAULT_P2P),
        label=key,
    )
    value = _merge_defaults(DEFAULT_P2P, payload.get(key))
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return {
        "seeds": _require_str_list(value, "seeds"),
        "persistent_peers": _require_str_list(value, "persistent_peers"),
    }


def _normalize_bds_service(payload: dict, key: str) -> dict:
    _reject_unknown_object_fields(
        payload,
        allowed=set(DEFAULT_SERVICES["bds"]),
        label=key,
    )
    value = _merge_defaults(DEFAULT_SERVICES["bds"], payload)
    pool_min_size = _require_non_negative_int(
        value, "pool_min_size", default=_BDS_DEFAULTS.pool_min_size
    )
    pool_max_size = _require_positive_int(
        value, "pool_max_size", default=_BDS_DEFAULTS.pool_max_size
    )
    if pool_min_size > pool_max_size:
        raise ValueError(f"{key}.pool_min_size must be <= pool_max_size")
    catchup_poll_seconds = _require_float(
        value,
        "catchup_poll_seconds",
        default=_BDS_DEFAULTS.catchup_poll_seconds,
    )
    if catchup_poll_seconds <= 0:
        raise ValueError(f"{key}.catchup_poll_seconds must be greater than zero")
    return {
        "enabled": _require_bool(value, "enabled", default=RUNTIME_DEFAULTS.bds_enabled),
        "dsn": _require_optional_str(value, "dsn") or "",
        "host": _require_optional_str(value, "host") or "",
        "port": _require_port(value, "port", default=_BDS_DEFAULTS.port),
        "database": _require_str(value, "database"),
        "user": _require_optional_str(value, "user") or "",
        "pool_min_size": pool_min_size,
        "pool_max_size": pool_max_size,
        "statement_timeout_ms": _require_non_negative_int(
            value,
            "statement_timeout_ms",
            default=_BDS_DEFAULTS.statement_timeout_ms,
        ),
        "acquire_timeout_ms": _require_non_negative_int(
            value,
            "acquire_timeout_ms",
            default=_BDS_DEFAULTS.acquire_timeout_ms,
        ),
        "application_name": _require_str(value, "application_name"),
        "queue_max_size": _require_positive_int(
            value, "queue_max_size", default=_BDS_DEFAULTS.queue_max_size
        ),
        "catchup_enabled": _require_bool(
            value,
            "catchup_enabled",
            default=_BDS_DEFAULTS.catchup_enabled,
        ),
        "catchup_poll_seconds": catchup_poll_seconds,
        "rpc_url": _require_optional_str(value, "rpc_url"),
        "spool_dir": _require_optional_str(value, "spool_dir") or "",
        "spool_warn_entries": _require_non_negative_int(
            value,
            "spool_warn_entries",
            default=_BDS_DEFAULTS.spool_warn_entries,
        ),
        "spool_warn_bytes": _require_non_negative_int(
            value,
            "spool_warn_bytes",
            default=_BDS_DEFAULTS.spool_warn_bytes,
        ),
        "disk_free_warn_bytes": _require_non_negative_int(
            value,
            "disk_free_warn_bytes",
            default=_BDS_DEFAULTS.disk_free_warn_bytes,
        ),
    }


def _normalize_services(
    payload: dict,
    *,
    intentkit_network_id_default: str | None = None,
) -> dict:
    services_input = payload.get("services")
    _reject_unknown_object_fields(
        services_input,
        allowed=set(DEFAULT_SERVICES),
        label="services",
    )
    if isinstance(services_input, dict):
        for service_name, defaults in DEFAULT_SERVICES.items():
            _reject_unknown_object_fields(
                services_input.get(service_name),
                allowed=set(defaults),
                label=f"services.{service_name}",
            )
    raw = _merge_defaults(DEFAULT_SERVICES, services_input)
    if not isinstance(raw, dict):
        raise ValueError("services must be an object")
    dashboard = _merge_defaults(DEFAULT_SERVICES["dashboard"], raw.get("dashboard"))
    intentkit_defaults = deepcopy(DEFAULT_SERVICES["intentkit"])
    if intentkit_network_id_default is not None:
        intentkit_defaults["network_id"] = intentkit_network_id_default
    intentkit = _merge_defaults(intentkit_defaults, raw.get("intentkit"))
    dex = _merge_defaults(DEFAULT_SERVICES["dex_automation"], raw.get("dex_automation"))
    relayer = _merge_defaults(DEFAULT_SERVICES["shielded_relayer"], raw.get("shielded_relayer"))
    return {
        "bds": _normalize_bds_service(raw.get("bds") or {}, "services.bds"),
        "dashboard": {
            "enabled": _require_bool(
                dashboard, "enabled", default=DEFAULT_SERVICES["dashboard"]["enabled"]
            ),
            "host": _require_str(dashboard, "host"),
            "port": _require_port(dashboard, "port", default=DEFAULT_SERVICES["dashboard"]["port"]),
        },
        "monitoring": {
            "enabled": _require_bool(
                raw.get("monitoring") or {},
                "enabled",
                default=DEFAULT_SERVICES["monitoring"]["enabled"],
            ),
        },
        "intentkit": {
            "enabled": _require_bool(
                intentkit, "enabled", default=DEFAULT_SERVICES["intentkit"]["enabled"]
            ),
            "network_id": _require_optional_choice(
                intentkit,
                "network_id",
                supported=SUPPORTED_INTENTKIT_NETWORK_IDS,
                default=intentkit.get("network_id"),
            ),
            "host": _require_str(intentkit, "host"),
            "port": _require_port(intentkit, "port", default=DEFAULT_SERVICES["intentkit"]["port"]),
            "api_port": _require_port(
                intentkit, "api_port", default=DEFAULT_SERVICES["intentkit"]["api_port"]
            ),
        },
        "dex_automation": {
            "enabled": _require_bool(
                dex, "enabled", default=DEFAULT_SERVICES["dex_automation"]["enabled"]
            ),
            "host": _require_str(dex, "host"),
            "port": _require_port(dex, "port", default=DEFAULT_SERVICES["dex_automation"]["port"]),
            "config": _require_optional_str(dex, "config"),
        },
        "shielded_relayer": {
            "enabled": _require_bool(
                relayer, "enabled", default=DEFAULT_SERVICES["shielded_relayer"]["enabled"]
            ),
            "host": _require_str(relayer, "host"),
            "port": _require_port(
                relayer, "port", default=DEFAULT_SERVICES["shielded_relayer"]["port"]
            ),
        },
    }


def _normalize_template_services(payload: dict) -> dict:
    services_input = payload.get("services")
    if isinstance(services_input, dict):
        _reject_unknown_object_fields(
            services_input.get("bds"),
            allowed=set(DEFAULT_TEMPLATE_SERVICES["bds"]),
            label="network template services.bds",
        )
    services = _normalize_services(payload)
    services["bds"] = {"enabled": services["bds"]["enabled"]}
    return services


def _normalize_advanced_cometbft(cometbft: dict) -> dict:
    return {
        "allow_cors": _require_bool(
            cometbft, "allow_cors", default=DEFAULT_ADVANCED_RUNTIME["cometbft"]["allow_cors"]
        ),
        "prometheus": _require_bool(
            cometbft, "prometheus", default=DEFAULT_ADVANCED_RUNTIME["cometbft"]["prometheus"]
        ),
        "proxy_app": _require_str(cometbft, "proxy_app"),
        "external_address": _require_text(cometbft, "external_address"),
    }


def _validate_legacy_state_sync(section: object) -> None:
    if not isinstance(section, dict):
        raise ValueError("advanced.statesync must be an object")
    _reject_unknown_fields(
        section,
        allowed={"enabled", "rpc_servers", "trust_height", "trust_hash", "trust_period"},
        label="advanced.statesync",
    )
    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("advanced.statesync.enabled must be a boolean")
    if enabled:
        raise ValueError(P2P_STATE_SYNC_UNSUPPORTED)


def _normalize_advanced_runtime(payload: dict) -> dict:
    advanced_input = payload.get("advanced")
    _reject_unknown_object_fields(
        advanced_input,
        allowed=set(DEFAULT_ADVANCED_RUNTIME) | {"statesync"},
        label="advanced",
    )
    if isinstance(advanced_input, dict):
        if "statesync" in advanced_input:
            _validate_legacy_state_sync(advanced_input["statesync"])
        for section_name, defaults in DEFAULT_ADVANCED_RUNTIME.items():
            _reject_unknown_object_fields(
                advanced_input.get(section_name),
                allowed=set(defaults),
                label=f"advanced.{section_name}",
            )
    raw = _merge_defaults(DEFAULT_ADVANCED_RUNTIME, advanced_input)
    if not isinstance(raw, dict):
        raise ValueError("advanced must be an object")
    cometbft = _merge_defaults(DEFAULT_ADVANCED_RUNTIME["cometbft"], raw.get("cometbft"))
    metrics = _merge_defaults(DEFAULT_ADVANCED_RUNTIME["metrics"], raw.get("metrics"))
    pending_nonce = _merge_defaults(
        DEFAULT_ADVANCED_RUNTIME["pending_nonce"], raw.get("pending_nonce")
    )
    metrics_refresh = _require_float(
        metrics, "bds_refresh_seconds", default=RUNTIME_DEFAULTS.metrics_bds_refresh_seconds
    )
    if metrics_refresh <= 0:
        raise ValueError("advanced.metrics.bds_refresh_seconds must be greater than zero")
    pending_nonce_ttl = _require_float(
        pending_nonce,
        "reservation_ttl_seconds",
        default=RUNTIME_DEFAULTS.pending_nonce_reservation_ttl_seconds,
    )
    if pending_nonce_ttl < 0:
        raise ValueError("advanced.pending_nonce.reservation_ttl_seconds must be non-negative")
    return {
        "cometbft": _normalize_advanced_cometbft(cometbft),
        "metrics": {
            "enabled": _require_bool(
                metrics, "enabled", default=DEFAULT_ADVANCED_RUNTIME["metrics"]["enabled"]
            ),
            "host": _require_str(metrics, "host"),
            "port": _require_port(
                metrics, "port", default=DEFAULT_ADVANCED_RUNTIME["metrics"]["port"]
            ),
            "bds_refresh_seconds": metrics_refresh,
        },
        "pending_nonce": {
            "reservation_ttl_seconds": pending_nonce_ttl,
            "max_per_sender": _require_positive_int(
                pending_nonce,
                "max_per_sender",
                default=DEFAULT_ADVANCED_RUNTIME["pending_nonce"]["max_per_sender"],
            ),
        },
    }


def normalize_network_manifest(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("network manifest must be a JSON object")
    _reject_unknown_fields(
        payload,
        allowed={
            "schema_version",
            "name",
            "chain_id",
            "genesis",
            "genesis_sha256",
            "genesis_build",
            "snapshot_url",
            "snapshot_signing_keys",
            "p2p",
            "block_policy_mode",
            "block_policy_interval",
            "node_image_mode",
            "node_integrated_image",
            "shielded_relayers",
            "privacy_artifact_catalog",
            "shielded_history_policy",
            "privacy_submission_policy",
            "runtime_features",
            "fee_policy",
            "node_release_manifest",
        },
        label="network manifest",
    )
    shielded_relayers = _normalize_shielded_relayers_manifest(payload)

    node_image_mode, node_integrated_image = _validate_node_image_config(
        mode=_require_node_image_mode(payload, "node_image_mode"),
        integrated_image=_require_optional_str(payload, "node_integrated_image"),
    )

    return {
        "schema_version": _require_schema_version(payload),
        "name": _require_str(payload, "name"),
        "chain_id": _require_str(payload, "chain_id"),
        "genesis": (genesis := _normalize_genesis(payload)),
        "genesis_sha256": _require_optional_sha256(payload, "genesis_sha256"),
        "genesis_build": _require_optional_object(payload, "genesis_build"),
        "snapshot_url": _require_optional_str(payload, "snapshot_url"),
        "snapshot_signing_keys": _require_str_list(payload, "snapshot_signing_keys"),
        "p2p": _normalize_p2p(payload),
        "block_policy_mode": _require_block_policy_mode(payload, "block_policy_mode"),
        "block_policy_interval": _require_block_policy_interval(payload, "block_policy_interval"),
        "node_image_mode": node_image_mode,
        "node_integrated_image": node_integrated_image,
        "shielded_relayers": shielded_relayers,
        "privacy_artifact_catalog": _normalize_privacy_artifact_catalog(
            payload, "privacy_artifact_catalog"
        ),
        "shielded_history_policy": _normalize_shielded_history_policy(
            payload, "shielded_history_policy"
        ),
        "privacy_submission_policy": _normalize_privacy_submission_policy(
            payload, "privacy_submission_policy"
        ),
        "runtime_features": _normalize_runtime_features(payload),
        "fee_policy": _normalize_manifest_fee_policy(payload, genesis),
        "node_release_manifest": _normalize_node_release_manifest(
            payload,
            "node_release_manifest",
        ),
    }


def _normalize_app_log_budgets(payload: dict) -> dict[str, int]:
    file_mb = _require_positive_int(
        payload, "app_log_max_file_mb", default=RUNTIME_DEFAULTS.app_log_max_file_mb
    )
    total_mb = _require_positive_int(
        payload, "app_log_max_total_mb", default=RUNTIME_DEFAULTS.app_log_max_total_mb
    )
    if total_mb < file_mb:
        raise ValueError("app_log_max_total_mb must be at least app_log_max_file_mb")
    return {"app_log_max_file_mb": file_mb, "app_log_max_total_mb": total_mb}


def normalize_node_profile(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("node profile must be a JSON object")
    _reject_node_local_fee_settings(payload)
    _reject_unknown_fields(
        payload,
        allowed={
            "schema_version",
            "name",
            "network",
            "moniker",
            "validator_key_ref",
            "node_image_mode",
            "node_integrated_image",
            "node_release_manifest",
            "stack_dir",
            "p2p",
            "genesis",
            "snapshot_url",
            "snapshot_signing_keys",
            "home",
            "pruning_enabled",
            "blocks_to_keep",
            "block_policy_mode",
            "block_policy_interval",
            "transaction_trace_logging",
            "app_log_level",
            "app_log_json",
            "app_log_rotation_hours",
            "app_log_retention_days",
            "app_log_max_file_mb",
            "app_log_max_total_mb",
            "simulation_enabled",
            "simulation_max_concurrency",
            "simulation_timeout_ms",
            "simulation_max_chi",
            "operator_profile",
            "services",
            "advanced",
        },
        label="node profile",
    )

    node_image_mode, node_integrated_image = _validate_node_image_config(
        mode=_require_optional_node_image_mode(payload, "node_image_mode"),
        integrated_image=_require_optional_str(payload, "node_integrated_image"),
    )

    return {
        "schema_version": _require_schema_version(payload),
        "name": _require_str(payload, "name"),
        "network": _require_str(payload, "network"),
        "moniker": _require_str(payload, "moniker"),
        "validator_key_ref": _require_optional_str(
            payload,
            "validator_key_ref",
        ),
        "node_image_mode": node_image_mode,
        "node_integrated_image": node_integrated_image,
        "node_release_manifest": _normalize_node_release_manifest(
            payload,
            "node_release_manifest",
        ),
        "stack_dir": _require_optional_str(payload, "stack_dir"),
        "p2p": _normalize_p2p(payload),
        "genesis": _normalize_optional_genesis(payload),
        "snapshot_url": _require_optional_str(payload, "snapshot_url"),
        "snapshot_signing_keys": _require_str_list(payload, "snapshot_signing_keys"),
        "home": _require_optional_str(payload, "home"),
        "pruning_enabled": _require_bool(
            payload, "pruning_enabled", default=RUNTIME_DEFAULTS.pruning_enabled
        ),
        "blocks_to_keep": _require_positive_int(
            payload, "blocks_to_keep", default=RUNTIME_DEFAULTS.blocks_to_keep
        ),
        "block_policy_mode": _require_block_policy_mode(payload, "block_policy_mode"),
        "block_policy_interval": _require_block_policy_interval(payload, "block_policy_interval"),
        "transaction_trace_logging": _require_bool(
            payload, "transaction_trace_logging", default=RUNTIME_DEFAULTS.transaction_trace_logging
        ),
        "app_log_level": _require_app_log_level(payload, "app_log_level"),
        "app_log_json": _require_bool(
            payload, "app_log_json", default=RUNTIME_DEFAULTS.app_log_json
        ),
        "app_log_rotation_hours": _require_positive_int(
            payload, "app_log_rotation_hours", default=RUNTIME_DEFAULTS.app_log_rotation_hours
        ),
        "app_log_retention_days": _require_positive_int(
            payload, "app_log_retention_days", default=RUNTIME_DEFAULTS.app_log_retention_days
        ),
        **_normalize_app_log_budgets(payload),
        "simulation_enabled": _require_bool(
            payload, "simulation_enabled", default=RUNTIME_DEFAULTS.simulation_enabled
        ),
        "simulation_max_concurrency": _require_positive_int(
            payload,
            "simulation_max_concurrency",
            default=RUNTIME_DEFAULTS.simulation_max_concurrency,
        ),
        "simulation_timeout_ms": _require_positive_int(
            payload, "simulation_timeout_ms", default=RUNTIME_DEFAULTS.simulation_timeout_ms
        ),
        # Optional operator cap on one readonly simulation. Unset, the node
        # simulates with the chain's max_tx_chi; a value only ever lowers it.
        "simulation_max_chi": _require_optional_positive_int(
            payload, "simulation_max_chi", default=RUNTIME_DEFAULTS.simulation_max_chi
        ),
        "operator_profile": _require_optional_choice(
            payload,
            "operator_profile",
            supported=SUPPORTED_OPERATOR_PROFILES,
            default=None,
        ),
        "services": _normalize_services(
            payload,
            intentkit_network_id_default=None,
        ),
        "advanced": _normalize_advanced_runtime(payload),
    }


def normalize_network_template(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("network template must be a JSON object")
    _reject_unknown_fields(
        payload,
        allowed={
            "schema_version",
            "name",
            "display_name",
            "description",
            "block_policy_mode",
            "block_policy_interval",
            "transaction_trace_logging",
            "app_log_level",
            "app_log_json",
            "app_log_rotation_hours",
            "app_log_retention_days",
            "app_log_max_file_mb",
            "app_log_max_total_mb",
            "simulation_enabled",
            "simulation_max_concurrency",
            "simulation_timeout_ms",
            "simulation_max_chi",
            "tx_fee_mode",
            "max_tx_chi",
            "max_block_chi",
            "operator_profile",
            "bootstrap_node_name",
            "additional_validator_names",
            "services",
            "advanced",
            "pruning_enabled",
            "blocks_to_keep",
        },
        label="network template",
    )

    return {
        "schema_version": _require_schema_version(payload),
        "name": _require_str(payload, "name"),
        "display_name": _require_str(payload, "display_name"),
        "description": _require_str(payload, "description"),
        "block_policy_mode": _require_block_policy_mode(payload, "block_policy_mode"),
        "block_policy_interval": _require_block_policy_interval(payload, "block_policy_interval"),
        "transaction_trace_logging": _require_bool(
            payload, "transaction_trace_logging", default=RUNTIME_DEFAULTS.transaction_trace_logging
        ),
        "app_log_level": _require_app_log_level(payload, "app_log_level"),
        "app_log_json": _require_bool(
            payload, "app_log_json", default=RUNTIME_DEFAULTS.app_log_json
        ),
        "app_log_rotation_hours": _require_positive_int(
            payload, "app_log_rotation_hours", default=RUNTIME_DEFAULTS.app_log_rotation_hours
        ),
        "app_log_retention_days": _require_positive_int(
            payload, "app_log_retention_days", default=RUNTIME_DEFAULTS.app_log_retention_days
        ),
        **_normalize_app_log_budgets(payload),
        "simulation_enabled": _require_bool(
            payload, "simulation_enabled", default=RUNTIME_DEFAULTS.simulation_enabled
        ),
        "simulation_max_concurrency": _require_positive_int(
            payload,
            "simulation_max_concurrency",
            default=RUNTIME_DEFAULTS.simulation_max_concurrency,
        ),
        "simulation_timeout_ms": _require_positive_int(
            payload, "simulation_timeout_ms", default=RUNTIME_DEFAULTS.simulation_timeout_ms
        ),
        # Optional operator cap on one readonly simulation. Unset, the node
        # simulates with the chain's max_tx_chi; a value only ever lowers it.
        "simulation_max_chi": _require_optional_positive_int(
            payload, "simulation_max_chi", default=RUNTIME_DEFAULTS.simulation_max_chi
        ),
        "tx_fee_mode": _require_tx_fee_mode(payload, "tx_fee_mode"),
        # Unset caps fall back to the chain defaults when genesis is built.
        **{name: _require_optional_positive_int(payload, name) for name in CHI_CAP_FIELDS},
        "operator_profile": _require_optional_choice(
            payload,
            "operator_profile",
            supported=SUPPORTED_OPERATOR_PROFILES,
        ),
        "bootstrap_node_name": _require_optional_str(payload, "bootstrap_node_name"),
        "additional_validator_names": _require_str_list(payload, "additional_validator_names"),
        "services": _normalize_template_services(payload),
        "advanced": _normalize_advanced_runtime(payload),
        "pruning_enabled": _require_bool(
            payload, "pruning_enabled", default=RUNTIME_DEFAULTS.pruning_enabled
        ),
        "blocks_to_keep": _require_positive_int(
            payload, "blocks_to_keep", default=RUNTIME_DEFAULTS.blocks_to_keep
        ),
    }


def normalize_recovery_plan(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("recovery plan must be a JSON object")

    artifact = payload.get("artifact")
    if not isinstance(artifact, dict):
        raise ValueError("artifact must be a JSON object")
    artifact_kind = _require_str(artifact, "kind")
    if artifact_kind not in SUPPORTED_RECOVERY_ARTIFACT_KINDS:
        raise ValueError(
            f"artifact.kind must be one of {sorted(SUPPORTED_RECOVERY_ARTIFACT_KINDS)}"
        )

    runtime = payload.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ValueError("runtime must be a JSON object when provided")

    follow_up_state_patch = payload.get("follow_up_state_patch")
    if follow_up_state_patch is not None and not isinstance(follow_up_state_patch, dict):
        raise ValueError("follow_up_state_patch must be a JSON object when provided")

    normalized = {
        "schema_version": _require_schema_version(payload),
        "name": _require_str(payload, "name"),
        "chain_id": _require_str(payload, "chain_id"),
        "target_height": _require_positive_int_no_default(payload, "target_height"),
        "trusted_block_hash": _require_str(payload, "trusted_block_hash"),
        "trusted_app_hash": _require_str(payload, "trusted_app_hash"),
        "reason": _require_str(payload, "reason"),
        "artifact": {
            "kind": artifact_kind,
            "uri": _require_str(artifact, "uri"),
            "sha256": _require_optional_str(artifact, "sha256"),
        },
        "runtime": {
            "xian_abci_version": _require_optional_str(runtime, "xian_abci_version"),
            "cometbft_version": _require_optional_str(runtime, "cometbft_version"),
        },
        "follow_up_state_patch": None,
    }
    if follow_up_state_patch is not None:
        activation_height = _require_positive_int_no_default(
            follow_up_state_patch, "activation_height"
        )
        if activation_height <= normalized["target_height"]:
            raise ValueError(
                "follow_up_state_patch.activation_height must be greater than target_height"
            )
        normalized["follow_up_state_patch"] = {
            "patch_id": _require_str(follow_up_state_patch, "patch_id"),
            "bundle_hash": _require_str(follow_up_state_patch, "bundle_hash"),
            "activation_height": activation_height,
        }

    return normalized


@dataclass(slots=True)
class NetworkManifest:
    name: str
    chain_id: str
    genesis: dict = field(
        default_factory=lambda: {
            "kind": "bundle",
            "bundle": "local",
            "genesis_time": None,
        }
    )
    genesis_sha256: str | None = None
    genesis_build: dict | None = None
    node_image_mode: str = "local_build"
    node_integrated_image: str | None = None
    shielded_relayers: list[dict] = field(default_factory=list)
    privacy_artifact_catalog: dict | None = None
    shielded_history_policy: dict | None = None
    privacy_submission_policy: dict | None = None
    fee_policy: dict | None = None
    node_release_manifest: dict | None = None
    snapshot_url: str | None = None
    snapshot_signing_keys: list[str] = field(default_factory=list)
    p2p: dict = field(default_factory=lambda: deepcopy(DEFAULT_P2P))
    block_policy_mode: str = DEFAULT_BLOCK_POLICY_MODE
    block_policy_interval: str = DEFAULT_BLOCK_POLICY_INTERVAL
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class NodeProfile:
    name: str
    network: str
    moniker: str
    validator_key_ref: str | None = None
    node_image_mode: str | None = None
    node_integrated_image: str | None = None
    node_release_manifest: dict | None = None
    stack_dir: str | None = None
    p2p: dict = field(default_factory=lambda: deepcopy(DEFAULT_P2P))
    genesis: dict | None = None
    snapshot_url: str | None = None
    snapshot_signing_keys: list[str] = field(default_factory=list)
    home: str | None = None
    pruning_enabled: bool = RUNTIME_DEFAULTS.pruning_enabled
    blocks_to_keep: int = RUNTIME_DEFAULTS.blocks_to_keep
    block_policy_mode: str = DEFAULT_BLOCK_POLICY_MODE
    block_policy_interval: str = DEFAULT_BLOCK_POLICY_INTERVAL
    transaction_trace_logging: bool = RUNTIME_DEFAULTS.transaction_trace_logging
    app_log_level: str = RUNTIME_DEFAULTS.app_log_level
    app_log_json: bool = RUNTIME_DEFAULTS.app_log_json
    app_log_rotation_hours: int = RUNTIME_DEFAULTS.app_log_rotation_hours
    app_log_retention_days: int = RUNTIME_DEFAULTS.app_log_retention_days
    app_log_max_file_mb: int = RUNTIME_DEFAULTS.app_log_max_file_mb
    app_log_max_total_mb: int = RUNTIME_DEFAULTS.app_log_max_total_mb
    simulation_enabled: bool = RUNTIME_DEFAULTS.simulation_enabled
    simulation_max_concurrency: int = RUNTIME_DEFAULTS.simulation_max_concurrency
    simulation_timeout_ms: int = RUNTIME_DEFAULTS.simulation_timeout_ms
    simulation_max_chi: int | None = RUNTIME_DEFAULTS.simulation_max_chi
    operator_profile: str | None = None
    services: dict = field(default_factory=lambda: deepcopy(DEFAULT_SERVICES))
    advanced: dict = field(default_factory=lambda: deepcopy(DEFAULT_ADVANCED_RUNTIME))
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class NetworkTemplate:
    name: str
    display_name: str
    description: str
    block_policy_mode: str = DEFAULT_BLOCK_POLICY_MODE
    block_policy_interval: str = DEFAULT_BLOCK_POLICY_INTERVAL
    transaction_trace_logging: bool = RUNTIME_DEFAULTS.transaction_trace_logging
    app_log_level: str = RUNTIME_DEFAULTS.app_log_level
    app_log_json: bool = RUNTIME_DEFAULTS.app_log_json
    app_log_rotation_hours: int = RUNTIME_DEFAULTS.app_log_rotation_hours
    app_log_retention_days: int = RUNTIME_DEFAULTS.app_log_retention_days
    app_log_max_file_mb: int = RUNTIME_DEFAULTS.app_log_max_file_mb
    app_log_max_total_mb: int = RUNTIME_DEFAULTS.app_log_max_total_mb
    simulation_enabled: bool = RUNTIME_DEFAULTS.simulation_enabled
    simulation_max_concurrency: int = RUNTIME_DEFAULTS.simulation_max_concurrency
    simulation_timeout_ms: int = RUNTIME_DEFAULTS.simulation_timeout_ms
    simulation_max_chi: int | None = RUNTIME_DEFAULTS.simulation_max_chi
    tx_fee_mode: str = DEFAULT_TX_FEE_MODE
    max_tx_chi: int | None = None
    max_block_chi: int | None = None
    operator_profile: str | None = None
    bootstrap_node_name: str | None = None
    additional_validator_names: list[str] = field(default_factory=list)
    services: dict = field(default_factory=lambda: deepcopy(DEFAULT_TEMPLATE_SERVICES))
    advanced: dict = field(default_factory=lambda: deepcopy(DEFAULT_ADVANCED_RUNTIME))
    pruning_enabled: bool = RUNTIME_DEFAULTS.pruning_enabled
    blocks_to_keep: int = RUNTIME_DEFAULTS.blocks_to_keep
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class ExampleStarterStep:
    title: str
    commands: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class ExampleStarterFlow:
    name: str
    display_name: str
    template: str
    summary: str
    network_name: str | None = None
    node_name: str | None = None
    steps: list[ExampleStarterStep] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _normalize_json_value(value):
    if isinstance(value, dict):
        return {str(key): _normalize_json_value(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_json_value(item) for item in value]
    if isinstance(value, (ContractingDecimal, decimal.Decimal, Datetime)):
        return str(value)
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    return value


def write_json(
    path: Path,
    payload: dict,
    *,
    force: bool = False,
    preserve_runtime_types: bool = False,
    private: bool = False,
) -> None:
    normalized = (
        normalize_wire_numbers(payload)
        if preserve_runtime_types
        else _normalize_json_value(payload)
    )
    content = json.dumps(normalized, indent=2) + "\n"
    if private:
        secure_write_text(path, content, force=force)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; pass --force to overwrite")
    path.write_text(content, encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_network_manifest(path: Path) -> dict:
    return normalize_network_manifest(read_json(path))


def read_node_profile(path: Path, *, ignore_legacy_state_sync: bool = False) -> dict:
    """Read a profile and tolerate retired settings only for offline or diagnostic use."""
    payload = read_json(path)
    if ignore_legacy_state_sync and isinstance(payload, dict):
        advanced = payload.get("advanced")
        if isinstance(advanced, dict):
            advanced.pop("statesync", None)
    return normalize_node_profile(payload)


def read_network_template(path: Path) -> dict:
    return normalize_network_template(read_json(path))


def read_recovery_plan(path: Path) -> dict:
    return normalize_recovery_plan(read_json(path))
