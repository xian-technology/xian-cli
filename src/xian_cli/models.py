from __future__ import annotations

import decimal
import json
import math
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path

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
    "indexed_development",
    "shared_network",
}
SUPPORTED_MONITORING_PROFILES = {
    "none",
    "local_stack",
    "bds",
}
SUPPORTED_INTENTKIT_NETWORK_IDS = {
    "xian-mainnet",
    "xian-testnet",
    "xian-devnet",
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
# Operator cap on one readonly simulation (the VM ceiling). The node never
# simulates with more than the chain's max_tx_chi, so this only ever lowers it.
DEFAULT_SIMULATION_MAX_CHI = 1_000_000
SUPPORTED_RECOVERY_ARTIFACT_KINDS = {"snapshot_url"}
SUPPORTED_RUNTIME_FEATURES = {"zk"}

DEFAULT_P2P = {
    "seeds": [],
    "persistent_peers": [],
}

DEFAULT_SERVICES = {
    "bds": {
        "enabled": False,
        "dsn": "",
        "host": "",
        "port": 5432,
        "database": "xian",
        "user": "",
        "password": "",
        "pool_min_size": 1,
        "pool_max_size": 10,
        "statement_timeout_ms": 0,
        "acquire_timeout_ms": 10000,
        "application_name": "xian-bds",
        "queue_max_size": 128,
        "catchup_enabled": True,
        "catchup_poll_seconds": 1.0,
        "rpc_url": None,
        "spool_dir": "",
        "spool_warn_entries": 256,
        "spool_warn_bytes": 536_870_912,
        "disk_free_warn_bytes": 2_147_483_648,
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
    "bds": {"enabled": False},
}

DEFAULT_ADVANCED_RUNTIME = {
    "cometbft": {
        "allow_cors": True,
        "prometheus": True,
        "proxy_app": "unix:///tmp/abci.sock",
    },
    "statesync": {
        "enabled": False,
        "rpc_servers": [],
        "trust_height": 0,
        "trust_hash": "",
        "trust_period": "168h0m0s",
    },
    "metrics": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 9108,
        "bds_refresh_seconds": 5.0,
    },
    "pending_nonce": {
        "reservation_ttl_seconds": 60.0,
        "max_per_sender": 128,
    },
}


def _require_str(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
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


def _require_optional_positive_int(payload: dict, key: str) -> int | None:
    if payload.get(key) is None:
        return None
    return _require_positive_int_no_default(payload, key)


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


def _normalize_node_release_manifest(
    payload: dict,
    key: str,
) -> dict | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object when provided")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"{key}.schema_version must be {SCHEMA_VERSION}")

    components_raw = value.get("components")
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

    images_raw = value.get("images")
    if not isinstance(images_raw, dict):
        raise ValueError(f"{key}.images must be an object")
    images = {
        "integrated": _require_str(images_raw, "integrated"),
        "split": _require_str(images_raw, "split"),
    }

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
    split_image: str | None,
) -> tuple[str | None, str | None, str | None]:
    if mode != "registry" and (integrated_image is not None or split_image is not None):
        raise ValueError(
            "node_integrated_image and node_split_image require node_image_mode=registry"
        )
    if mode == "registry" and (integrated_image is None or split_image is None):
        raise ValueError(
            "registry node image mode requires both node_integrated_image and node_split_image"
        )
    return mode, integrated_image, split_image


def _require_block_policy_mode(payload: dict, key: str) -> str:
    value = payload.get(key, "on_demand")
    if not isinstance(value, str) or value not in SUPPORTED_BLOCK_POLICY_MODES:
        raise ValueError(f"{key} must be one of {sorted(SUPPORTED_BLOCK_POLICY_MODES)}")
    return value


def _require_block_policy_interval(payload: dict, key: str) -> str:
    value = payload.get(key, "0s")
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _require_app_log_level(payload: dict, key: str) -> str:
    value = payload.get(key, "INFO")
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
    pool_min_size = _require_non_negative_int(value, "pool_min_size", default=1)
    pool_max_size = _require_positive_int(value, "pool_max_size", default=10)
    if pool_min_size > pool_max_size:
        raise ValueError(f"{key}.pool_min_size must be <= pool_max_size")
    catchup_poll_seconds = _require_float(value, "catchup_poll_seconds", default=1.0)
    if catchup_poll_seconds <= 0:
        raise ValueError(f"{key}.catchup_poll_seconds must be greater than zero")
    return {
        "enabled": _require_bool(value, "enabled", default=False),
        "dsn": _require_optional_str(value, "dsn") or "",
        "host": _require_optional_str(value, "host") or "",
        "port": _require_port(value, "port", default=5432),
        "database": _require_str(value, "database"),
        "user": _require_optional_str(value, "user") or "",
        "password": _require_optional_str(value, "password") or "",
        "pool_min_size": pool_min_size,
        "pool_max_size": pool_max_size,
        "statement_timeout_ms": _require_non_negative_int(value, "statement_timeout_ms", default=0),
        "acquire_timeout_ms": _require_non_negative_int(value, "acquire_timeout_ms", default=10000),
        "application_name": _require_str(value, "application_name"),
        "queue_max_size": _require_positive_int(value, "queue_max_size", default=128),
        "catchup_enabled": _require_bool(value, "catchup_enabled", default=True),
        "catchup_poll_seconds": catchup_poll_seconds,
        "rpc_url": _require_optional_str(value, "rpc_url"),
        "spool_dir": _require_optional_str(value, "spool_dir") or "",
        "spool_warn_entries": _require_non_negative_int(value, "spool_warn_entries", default=256),
        "spool_warn_bytes": _require_non_negative_int(
            value, "spool_warn_bytes", default=536_870_912
        ),
        "disk_free_warn_bytes": _require_non_negative_int(
            value, "disk_free_warn_bytes", default=2_147_483_648
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
            "enabled": _require_bool(dashboard, "enabled", default=False),
            "host": _require_str(dashboard, "host"),
            "port": _require_port(dashboard, "port", default=8080),
        },
        "monitoring": {
            "enabled": _require_bool(raw.get("monitoring") or {}, "enabled", default=False),
        },
        "intentkit": {
            "enabled": _require_bool(intentkit, "enabled", default=False),
            "network_id": _require_optional_choice(
                intentkit,
                "network_id",
                supported=SUPPORTED_INTENTKIT_NETWORK_IDS,
                default=intentkit.get("network_id"),
            ),
            "host": _require_str(intentkit, "host"),
            "port": _require_port(intentkit, "port", default=38000),
            "api_port": _require_port(intentkit, "api_port", default=38080),
        },
        "dex_automation": {
            "enabled": _require_bool(dex, "enabled", default=False),
            "host": _require_str(dex, "host"),
            "port": _require_port(dex, "port", default=38280),
            "config": _require_optional_str(dex, "config"),
        },
        "shielded_relayer": {
            "enabled": _require_bool(relayer, "enabled", default=False),
            "host": _require_str(relayer, "host"),
            "port": _require_port(relayer, "port", default=38180),
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


def _normalize_advanced_runtime(payload: dict) -> dict:
    advanced_input = payload.get("advanced")
    _reject_unknown_object_fields(
        advanced_input,
        allowed=set(DEFAULT_ADVANCED_RUNTIME),
        label="advanced",
    )
    if isinstance(advanced_input, dict):
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
    statesync = _merge_defaults(DEFAULT_ADVANCED_RUNTIME["statesync"], raw.get("statesync"))
    metrics = _merge_defaults(DEFAULT_ADVANCED_RUNTIME["metrics"], raw.get("metrics"))
    pending_nonce = _merge_defaults(
        DEFAULT_ADVANCED_RUNTIME["pending_nonce"], raw.get("pending_nonce")
    )
    metrics_refresh = _require_float(metrics, "bds_refresh_seconds", default=5.0)
    if metrics_refresh <= 0:
        raise ValueError("advanced.metrics.bds_refresh_seconds must be greater than zero")
    statesync_enabled = _require_bool(statesync, "enabled", default=False)
    statesync_servers = _require_str_list(statesync, "rpc_servers")
    statesync_trust_height = _require_non_negative_int(statesync, "trust_height", default=0)
    statesync_trust_hash = _require_optional_str(statesync, "trust_hash") or ""
    statesync_trust_period = _require_str(statesync, "trust_period")
    if statesync_enabled:
        if len(statesync_servers) < 2:
            raise ValueError(
                "advanced.statesync.rpc_servers must include at least two "
                "servers when state sync is enabled"
            )
        if statesync_trust_height <= 0:
            raise ValueError(
                "advanced.statesync.trust_height must be greater than zero "
                "when state sync is enabled"
            )
        if not statesync_trust_hash:
            raise ValueError("advanced.statesync.trust_hash is required when state sync is enabled")
    pending_nonce_ttl = _require_float(pending_nonce, "reservation_ttl_seconds", default=60.0)
    if pending_nonce_ttl < 0:
        raise ValueError("advanced.pending_nonce.reservation_ttl_seconds must be non-negative")
    return {
        "cometbft": {
            "allow_cors": _require_bool(cometbft, "allow_cors", default=True),
            "prometheus": _require_bool(cometbft, "prometheus", default=True),
            "proxy_app": _require_str(cometbft, "proxy_app"),
        },
        "statesync": {
            "enabled": statesync_enabled,
            "rpc_servers": statesync_servers,
            "trust_height": statesync_trust_height,
            "trust_hash": statesync_trust_hash,
            "trust_period": statesync_trust_period,
        },
        "metrics": {
            "enabled": _require_bool(metrics, "enabled", default=True),
            "host": _require_str(metrics, "host"),
            "port": _require_port(metrics, "port", default=9108),
            "bds_refresh_seconds": metrics_refresh,
        },
        "pending_nonce": {
            "reservation_ttl_seconds": pending_nonce_ttl,
            "max_per_sender": _require_positive_int(pending_nonce, "max_per_sender", default=128),
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
            "genesis_build",
            "snapshot_url",
            "snapshot_signing_keys",
            "p2p",
            "block_policy_mode",
            "block_policy_interval",
            "node_image_mode",
            "node_integrated_image",
            "node_split_image",
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

    node_image_mode, node_integrated_image, node_split_image = _validate_node_image_config(
        mode=_require_node_image_mode(payload, "node_image_mode"),
        integrated_image=_require_optional_str(payload, "node_integrated_image"),
        split_image=_require_optional_str(payload, "node_split_image"),
    )

    return {
        "schema_version": _require_schema_version(payload),
        "name": _require_str(payload, "name"),
        "chain_id": _require_str(payload, "chain_id"),
        "genesis": (genesis := _normalize_genesis(payload)),
        "genesis_build": _require_optional_object(payload, "genesis_build"),
        "snapshot_url": _require_optional_str(payload, "snapshot_url"),
        "snapshot_signing_keys": _require_str_list(payload, "snapshot_signing_keys"),
        "p2p": _normalize_p2p(payload),
        "block_policy_mode": _require_block_policy_mode(payload, "block_policy_mode"),
        "block_policy_interval": _require_block_policy_interval(payload, "block_policy_interval"),
        "node_image_mode": node_image_mode,
        "node_integrated_image": node_integrated_image,
        "node_split_image": node_split_image,
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
    file_mb = _require_positive_int(payload, "app_log_max_file_mb", default=50)
    total_mb = _require_positive_int(payload, "app_log_max_total_mb", default=250)
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
            "node_split_image",
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
            "monitoring_profile",
            "services",
            "advanced",
        },
        label="node profile",
    )

    node_image_mode, node_integrated_image, node_split_image = _validate_node_image_config(
        mode=_require_optional_node_image_mode(payload, "node_image_mode"),
        integrated_image=_require_optional_str(payload, "node_integrated_image"),
        split_image=_require_optional_str(payload, "node_split_image"),
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
        "node_split_image": node_split_image,
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
        "pruning_enabled": _require_bool(payload, "pruning_enabled", default=False),
        "blocks_to_keep": _require_positive_int(payload, "blocks_to_keep", default=100000),
        "block_policy_mode": _require_block_policy_mode(payload, "block_policy_mode"),
        "block_policy_interval": _require_block_policy_interval(payload, "block_policy_interval"),
        "transaction_trace_logging": _require_bool(
            payload, "transaction_trace_logging", default=False
        ),
        "app_log_level": _require_app_log_level(payload, "app_log_level"),
        "app_log_json": _require_bool(payload, "app_log_json", default=False),
        "app_log_rotation_hours": _require_positive_int(
            payload, "app_log_rotation_hours", default=1
        ),
        "app_log_retention_days": _require_positive_int(
            payload, "app_log_retention_days", default=7
        ),
        **_normalize_app_log_budgets(payload),
        "simulation_enabled": _require_bool(payload, "simulation_enabled", default=True),
        "simulation_max_concurrency": _require_positive_int(
            payload, "simulation_max_concurrency", default=2
        ),
        "simulation_timeout_ms": _require_positive_int(
            payload, "simulation_timeout_ms", default=3000
        ),
        "simulation_max_chi": _require_positive_int(
            payload, "simulation_max_chi", default=DEFAULT_SIMULATION_MAX_CHI
        ),
        "operator_profile": _require_optional_choice(
            payload,
            "operator_profile",
            supported=SUPPORTED_OPERATOR_PROFILES,
            default=None,
        ),
        "monitoring_profile": _require_optional_choice(
            payload,
            "monitoring_profile",
            supported=SUPPORTED_MONITORING_PROFILES,
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
            "monitoring_profile",
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
            payload, "transaction_trace_logging", default=False
        ),
        "app_log_level": _require_app_log_level(payload, "app_log_level"),
        "app_log_json": _require_bool(payload, "app_log_json", default=False),
        "app_log_rotation_hours": _require_positive_int(
            payload, "app_log_rotation_hours", default=1
        ),
        "app_log_retention_days": _require_positive_int(
            payload, "app_log_retention_days", default=7
        ),
        **_normalize_app_log_budgets(payload),
        "simulation_enabled": _require_bool(payload, "simulation_enabled", default=True),
        "simulation_max_concurrency": _require_positive_int(
            payload, "simulation_max_concurrency", default=2
        ),
        "simulation_timeout_ms": _require_positive_int(
            payload, "simulation_timeout_ms", default=3000
        ),
        "simulation_max_chi": _require_positive_int(
            payload, "simulation_max_chi", default=DEFAULT_SIMULATION_MAX_CHI
        ),
        "tx_fee_mode": _require_tx_fee_mode(payload, "tx_fee_mode"),
        # Unset caps fall back to the chain defaults when genesis is built.
        **{name: _require_optional_positive_int(payload, name) for name in CHI_CAP_FIELDS},
        "operator_profile": _require_optional_choice(
            payload,
            "operator_profile",
            supported=SUPPORTED_OPERATOR_PROFILES,
        ),
        "monitoring_profile": _require_optional_choice(
            payload,
            "monitoring_profile",
            supported=SUPPORTED_MONITORING_PROFILES,
        ),
        "bootstrap_node_name": _require_optional_str(payload, "bootstrap_node_name"),
        "additional_validator_names": _require_str_list(payload, "additional_validator_names"),
        "services": _normalize_template_services(payload),
        "advanced": _normalize_advanced_runtime(payload),
        "pruning_enabled": _require_bool(payload, "pruning_enabled", default=False),
        "blocks_to_keep": _require_positive_int(payload, "blocks_to_keep", default=100000),
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
    genesis_build: dict | None = None
    node_image_mode: str = "local_build"
    node_integrated_image: str | None = None
    node_split_image: str | None = None
    shielded_relayers: list[dict] = field(default_factory=list)
    privacy_artifact_catalog: dict | None = None
    shielded_history_policy: dict | None = None
    privacy_submission_policy: dict | None = None
    fee_policy: dict | None = None
    node_release_manifest: dict | None = None
    snapshot_url: str | None = None
    snapshot_signing_keys: list[str] = field(default_factory=list)
    p2p: dict = field(default_factory=lambda: deepcopy(DEFAULT_P2P))
    block_policy_mode: str = "on_demand"
    block_policy_interval: str = "0s"
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
    node_split_image: str | None = None
    node_release_manifest: dict | None = None
    stack_dir: str | None = None
    p2p: dict = field(default_factory=lambda: deepcopy(DEFAULT_P2P))
    genesis: dict | None = None
    snapshot_url: str | None = None
    snapshot_signing_keys: list[str] = field(default_factory=list)
    home: str | None = None
    pruning_enabled: bool = False
    blocks_to_keep: int = 100000
    block_policy_mode: str = "on_demand"
    block_policy_interval: str = "0s"
    transaction_trace_logging: bool = False
    app_log_level: str = "INFO"
    app_log_json: bool = False
    app_log_rotation_hours: int = 1
    app_log_retention_days: int = 7
    app_log_max_file_mb: int = 50
    app_log_max_total_mb: int = 250
    simulation_enabled: bool = True
    simulation_max_concurrency: int = 2
    simulation_timeout_ms: int = 3000
    simulation_max_chi: int = DEFAULT_SIMULATION_MAX_CHI
    operator_profile: str | None = None
    monitoring_profile: str | None = None
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
    block_policy_mode: str = "on_demand"
    block_policy_interval: str = "0s"
    transaction_trace_logging: bool = False
    app_log_level: str = "INFO"
    app_log_json: bool = False
    app_log_rotation_hours: int = 1
    app_log_retention_days: int = 7
    app_log_max_file_mb: int = 50
    app_log_max_total_mb: int = 250
    simulation_enabled: bool = True
    simulation_max_concurrency: int = 2
    simulation_timeout_ms: int = 3000
    simulation_max_chi: int = DEFAULT_SIMULATION_MAX_CHI
    tx_fee_mode: str = DEFAULT_TX_FEE_MODE
    max_tx_chi: int | None = None
    max_block_chi: int | None = None
    operator_profile: str | None = None
    monitoring_profile: str | None = None
    bootstrap_node_name: str | None = None
    additional_validator_names: list[str] = field(default_factory=list)
    services: dict = field(default_factory=lambda: deepcopy(DEFAULT_TEMPLATE_SERVICES))
    advanced: dict = field(default_factory=lambda: deepcopy(DEFAULT_ADVANCED_RUNTIME))
    pruning_enabled: bool = False
    blocks_to_keep: int = 100000
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


def read_node_profile(path: Path) -> dict:
    return normalize_node_profile(read_json(path))


def read_network_template(path: Path) -> dict:
    return normalize_network_template(read_json(path))


def read_recovery_plan(path: Path) -> dict:
    return normalize_recovery_plan(read_json(path))
