from __future__ import annotations

import decimal
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.encoding import encode
from xian_runtime_types.time import Datetime

SCHEMA_VERSION = 1
SUPPORTED_NETWORK_MODES = {"join", "create"}
SUPPORTED_RUNTIME_BACKENDS = {"xian-stack"}
SUPPORTED_BLOCK_POLICY_MODES = {"on_demand", "idle_interval", "periodic"}
SUPPORTED_TRACER_MODES = {"python_line_v1", "native_instruction_v1"}
SUPPORTED_NODE_IMAGE_MODES = {"local_build", "registry"}
SUPPORTED_OPERATOR_PROFILES = {
    "local_development",
    "indexed_development",
    "shared_network",
    "embedded_backend",
}
SUPPORTED_MONITORING_PROFILES = {
    "none",
    "local_stack",
    "service_node",
}
SUPPORTED_INTENTKIT_NETWORK_IDS = {
    "xian-mainnet",
    "xian-testnet",
    "xian-devnet",
    "xian-localnet",
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
SUPPORTED_RECOVERY_ARTIFACT_KINDS = {"snapshot_url"}


def _require_str(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _require_optional_str(payload: dict, key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string when provided")
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


def _require_bool(payload: dict, key: str, *, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _require_int(payload: dict, key: str, *, default: int) -> int:
    value = payload.get(key, default)
    if not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


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


def _require_str_list(payload: dict, key: str) -> list[str]:
    value = payload.get(key, [])
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{key} must be a list of non-empty strings")
    return value


def _require_schema_version(payload: dict) -> int:
    schema_version = payload.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported schema_version: {schema_version}; "
            f"expected {SCHEMA_VERSION}"
        )
    return schema_version


def _require_runtime_backend(payload: dict) -> str:
    runtime_backend = _require_str(payload, "runtime_backend")
    if runtime_backend not in SUPPORTED_RUNTIME_BACKENDS:
        raise ValueError(
            "runtime_backend must be one of "
            f"{sorted(SUPPORTED_RUNTIME_BACKENDS)}"
        )
    return runtime_backend


def _require_node_image_mode(payload: dict, key: str) -> str:
    value = payload.get(key, "local_build")
    if not isinstance(value, str) or value not in SUPPORTED_NODE_IMAGE_MODES:
        raise ValueError(
            f"{key} must be one of {sorted(SUPPORTED_NODE_IMAGE_MODES)}"
        )
    return value


def _require_optional_node_image_mode(
    payload: dict, key: str
) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or value not in SUPPORTED_NODE_IMAGE_MODES:
        raise ValueError(
            f"{key} must be one of {sorted(SUPPORTED_NODE_IMAGE_MODES)}"
        )
    return value


def _validate_node_image_config(
    *,
    mode: str | None,
    integrated_image: str | None,
    split_image: str | None,
) -> tuple[str | None, str | None, str | None]:
    if mode != "registry" and (
        integrated_image is not None or split_image is not None
    ):
        raise ValueError(
            "node_integrated_image and node_split_image require "
            "node_image_mode=registry"
        )
    if mode == "registry" and (
        integrated_image is None or split_image is None
    ):
        raise ValueError(
            "registry node image mode requires both "
            "node_integrated_image and node_split_image"
        )
    return mode, integrated_image, split_image


def _require_block_policy_mode(payload: dict, key: str) -> str:
    value = payload.get(key, "on_demand")
    if not isinstance(value, str) or value not in SUPPORTED_BLOCK_POLICY_MODES:
        raise ValueError(
            f"{key} must be one of {sorted(SUPPORTED_BLOCK_POLICY_MODES)}"
        )
    return value


def _require_block_policy_interval(payload: dict, key: str) -> str:
    value = payload.get(key, "0s")
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _require_tracer_mode(payload: dict, key: str) -> str:
    value = payload.get(key, "python_line_v1")
    if not isinstance(value, str) or value not in SUPPORTED_TRACER_MODES:
        raise ValueError(
            f"{key} must be one of {sorted(SUPPORTED_TRACER_MODES)}"
        )
    return value


def _require_app_log_level(payload: dict, key: str) -> str:
    value = payload.get(key, "INFO")
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    normalized = value.upper()
    if normalized not in SUPPORTED_APP_LOG_LEVELS:
        raise ValueError(
            f"{key} must be one of {sorted(SUPPORTED_APP_LOG_LEVELS)}"
        )
    return normalized


def _require_mode(payload: dict) -> str:
    mode = _require_str(payload, "mode")
    if mode not in SUPPORTED_NETWORK_MODES:
        raise ValueError(
            f"mode must be one of {sorted(SUPPORTED_NETWORK_MODES)}"
        )
    return mode


def normalize_network_manifest(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("network manifest must be a JSON object")

    node_image_mode, node_integrated_image, node_split_image = (
        _validate_node_image_config(
            mode=_require_node_image_mode(payload, "node_image_mode"),
            integrated_image=_require_optional_str(
                payload, "node_integrated_image"
            ),
            split_image=_require_optional_str(payload, "node_split_image"),
        )
    )

    return {
        "schema_version": _require_schema_version(payload),
        "name": _require_str(payload, "name"),
        "chain_id": _require_str(payload, "chain_id"),
        "mode": _require_mode(payload),
        "runtime_backend": _require_runtime_backend(payload),
        "genesis_source": _require_optional_str(payload, "genesis_source"),
        "snapshot_url": _require_optional_str(payload, "snapshot_url"),
        "seed_nodes": _require_str_list(payload, "seed_nodes"),
        "block_policy_mode": _require_block_policy_mode(
            payload, "block_policy_mode"
        ),
        "block_policy_interval": _require_block_policy_interval(
            payload, "block_policy_interval"
        ),
        "tracer_mode": _require_tracer_mode(payload, "tracer_mode"),
        "node_image_mode": node_image_mode,
        "node_integrated_image": node_integrated_image,
        "node_split_image": node_split_image,
    }


def normalize_node_profile(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("node profile must be a JSON object")

    node_image_mode, node_integrated_image, node_split_image = (
        _validate_node_image_config(
            mode=_require_optional_node_image_mode(
                payload, "node_image_mode"
            ),
            integrated_image=_require_optional_str(
                payload, "node_integrated_image"
            ),
            split_image=_require_optional_str(payload, "node_split_image"),
        )
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
        "runtime_backend": _require_runtime_backend(payload),
        "node_image_mode": node_image_mode,
        "node_integrated_image": node_integrated_image,
        "node_split_image": node_split_image,
        "stack_dir": _require_optional_str(payload, "stack_dir"),
        "seeds": _require_str_list(payload, "seeds"),
        "genesis_url": _require_optional_str(payload, "genesis_url"),
        "snapshot_url": _require_optional_str(payload, "snapshot_url"),
        "service_node": _require_bool(payload, "service_node", default=False),
        "home": _require_optional_str(payload, "home"),
        "pruning_enabled": _require_bool(
            payload, "pruning_enabled", default=False
        ),
        "blocks_to_keep": _require_int(
            payload, "blocks_to_keep", default=100000
        ),
        "block_policy_mode": _require_block_policy_mode(
            payload, "block_policy_mode"
        ),
        "block_policy_interval": _require_block_policy_interval(
            payload, "block_policy_interval"
        ),
        "tracer_mode": _require_tracer_mode(payload, "tracer_mode"),
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
        "simulation_enabled": _require_bool(
            payload, "simulation_enabled", default=True
        ),
        "simulation_max_concurrency": _require_positive_int(
            payload, "simulation_max_concurrency", default=2
        ),
        "simulation_timeout_ms": _require_positive_int(
            payload, "simulation_timeout_ms", default=3000
        ),
        "simulation_max_stamps": _require_positive_int(
            payload, "simulation_max_stamps", default=1_000_000
        ),
        "parallel_execution_enabled": _require_bool(
            payload, "parallel_execution_enabled", default=False
        ),
        "parallel_execution_workers": _require_non_negative_int(
            payload, "parallel_execution_workers", default=0
        ),
        "parallel_execution_min_transactions": _require_non_negative_int(
            payload, "parallel_execution_min_transactions", default=8
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
        "dashboard_enabled": _require_bool(
            payload, "dashboard_enabled", default=False
        ),
        "monitoring_enabled": _require_bool(
            payload, "monitoring_enabled", default=False
        ),
        "dashboard_host": _require_str(payload, "dashboard_host")
        if "dashboard_host" in payload
        else "127.0.0.1",
        "dashboard_port": _require_int(payload, "dashboard_port", default=8080),
        "intentkit_enabled": _require_bool(
            payload, "intentkit_enabled", default=False
        ),
        "intentkit_network_id": _require_optional_choice(
            payload,
            "intentkit_network_id",
            supported=SUPPORTED_INTENTKIT_NETWORK_IDS,
            default=None,
        ),
        "intentkit_host": _require_str(payload, "intentkit_host")
        if "intentkit_host" in payload
        else "127.0.0.1",
        "intentkit_port": _require_int(
            payload,
            "intentkit_port",
            default=38000,
        ),
        "intentkit_api_port": _require_int(
            payload, "intentkit_api_port", default=38080
        ),
    }


def normalize_network_template(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("network template must be a JSON object")

    return {
        "schema_version": _require_schema_version(payload),
        "name": _require_str(payload, "name"),
        "display_name": _require_str(payload, "display_name"),
        "description": _require_str(payload, "description"),
        "runtime_backend": _require_runtime_backend(payload),
        "block_policy_mode": _require_block_policy_mode(
            payload, "block_policy_mode"
        ),
        "block_policy_interval": _require_block_policy_interval(
            payload, "block_policy_interval"
        ),
        "tracer_mode": _require_tracer_mode(payload, "tracer_mode"),
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
        "simulation_enabled": _require_bool(
            payload, "simulation_enabled", default=True
        ),
        "simulation_max_concurrency": _require_positive_int(
            payload, "simulation_max_concurrency", default=2
        ),
        "simulation_timeout_ms": _require_positive_int(
            payload, "simulation_timeout_ms", default=3000
        ),
        "simulation_max_stamps": _require_positive_int(
            payload, "simulation_max_stamps", default=1_000_000
        ),
        "parallel_execution_enabled": _require_bool(
            payload, "parallel_execution_enabled", default=False
        ),
        "parallel_execution_workers": _require_non_negative_int(
            payload, "parallel_execution_workers", default=0
        ),
        "parallel_execution_min_transactions": _require_non_negative_int(
            payload, "parallel_execution_min_transactions", default=8
        ),
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
        "bootstrap_node_name": _require_optional_str(
            payload, "bootstrap_node_name"
        ),
        "additional_validator_names": _require_str_list(
            payload, "additional_validator_names"
        ),
        "service_node": _require_bool(payload, "service_node", default=False),
        "dashboard_enabled": _require_bool(
            payload, "dashboard_enabled", default=False
        ),
        "monitoring_enabled": _require_bool(
            payload, "monitoring_enabled", default=False
        ),
        "dashboard_host": _require_str(payload, "dashboard_host")
        if "dashboard_host" in payload
        else "127.0.0.1",
        "dashboard_port": _require_int(payload, "dashboard_port", default=8080),
        "intentkit_enabled": _require_bool(
            payload, "intentkit_enabled", default=False
        ),
        "intentkit_network_id": _require_optional_choice(
            payload,
            "intentkit_network_id",
            supported=SUPPORTED_INTENTKIT_NETWORK_IDS,
            default=None,
        ),
        "intentkit_host": _require_str(payload, "intentkit_host")
        if "intentkit_host" in payload
        else "127.0.0.1",
        "intentkit_port": _require_int(
            payload,
            "intentkit_port",
            default=38000,
        ),
        "intentkit_api_port": _require_int(
            payload, "intentkit_api_port", default=38080
        ),
        "pruning_enabled": _require_bool(
            payload, "pruning_enabled", default=False
        ),
        "blocks_to_keep": _require_int(
            payload, "blocks_to_keep", default=100000
        ),
    }


def _normalize_solution_pack_step(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("solution pack step must be a JSON object")

    commands = _require_str_list(payload, "commands")
    if not commands:
        raise ValueError("solution pack step commands must not be empty")

    return {
        "title": _require_str(payload, "title"),
        "commands": commands,
        "notes": _require_str_list(payload, "notes"),
    }


def _normalize_solution_pack_flow(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("solution pack starter flow must be a JSON object")

    steps = payload.get("steps")
    if (
        not isinstance(steps, list)
        or not steps
        or any(not isinstance(item, dict) for item in steps)
    ):
        raise ValueError(
            "solution pack starter flow steps must be a non-empty "
            "list of objects"
        )

    return {
        "name": _require_str(payload, "name"),
        "display_name": _require_str(payload, "display_name"),
        "template": _require_str(payload, "template"),
        "summary": _require_str(payload, "summary"),
        "network_name": _require_optional_str(payload, "network_name"),
        "node_name": _require_optional_str(payload, "node_name"),
        "steps": [_normalize_solution_pack_step(item) for item in steps],
    }


def normalize_solution_pack(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("solution pack must be a JSON object")

    starter_flows = payload.get("starter_flows")
    if (
        not isinstance(starter_flows, list)
        or not starter_flows
        or any(not isinstance(item, dict) for item in starter_flows)
    ):
        raise ValueError(
            "starter_flows must be a non-empty list of flow objects"
        )

    return {
        "schema_version": _require_schema_version(payload),
        "name": _require_str(payload, "name"),
        "display_name": _require_str(payload, "display_name"),
        "description": _require_str(payload, "description"),
        "use_case": _require_str(payload, "use_case"),
        "recommended_local_template": _require_str(
            payload, "recommended_local_template"
        ),
        "recommended_remote_template": _require_str(
            payload, "recommended_remote_template"
        ),
        "docs_path": _require_str(payload, "docs_path"),
        "example_dir": _require_str(payload, "example_dir"),
        "contract_paths": _require_str_list(payload, "contract_paths"),
        "starter_flows": [
            _normalize_solution_pack_flow(item) for item in starter_flows
        ],
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
            "artifact.kind must be one of "
            f"{sorted(SUPPORTED_RECOVERY_ARTIFACT_KINDS)}"
        )

    runtime = payload.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ValueError("runtime must be a JSON object when provided")

    follow_up_state_patch = payload.get("follow_up_state_patch")
    if follow_up_state_patch is not None and not isinstance(
        follow_up_state_patch, dict
    ):
        raise ValueError(
            "follow_up_state_patch must be a JSON object when provided"
        )

    normalized = {
        "schema_version": _require_schema_version(payload),
        "name": _require_str(payload, "name"),
        "chain_id": _require_str(payload, "chain_id"),
        "target_height": _require_positive_int_no_default(
            payload, "target_height"
        ),
        "trusted_block_hash": _require_str(payload, "trusted_block_hash"),
        "trusted_app_hash": _require_str(payload, "trusted_app_hash"),
        "reason": _require_str(payload, "reason"),
        "artifact": {
            "kind": artifact_kind,
            "uri": _require_str(artifact, "uri"),
            "sha256": _require_optional_str(artifact, "sha256"),
        },
        "runtime": {
            "xian_abci_version": _require_optional_str(
                runtime, "xian_abci_version"
            ),
            "cometbft_version": _require_optional_str(
                runtime, "cometbft_version"
            ),
        },
        "follow_up_state_patch": None,
    }
    if follow_up_state_patch is not None:
        activation_height = _require_positive_int_no_default(
            follow_up_state_patch, "activation_height"
        )
        if activation_height <= normalized["target_height"]:
            raise ValueError(
                "follow_up_state_patch.activation_height must be greater than "
                "target_height"
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
    mode: str = "join"
    runtime_backend: str = "xian-stack"
    node_image_mode: str = "local_build"
    node_integrated_image: str | None = None
    node_split_image: str | None = None
    genesis_source: str | None = None
    snapshot_url: str | None = None
    seed_nodes: list[str] = field(default_factory=list)
    block_policy_mode: str = "on_demand"
    block_policy_interval: str = "0s"
    tracer_mode: str = "python_line_v1"
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class NodeProfile:
    name: str
    network: str
    moniker: str
    validator_key_ref: str | None = None
    runtime_backend: str = "xian-stack"
    node_image_mode: str | None = None
    node_integrated_image: str | None = None
    node_split_image: str | None = None
    stack_dir: str | None = None
    seeds: list[str] = field(default_factory=list)
    genesis_url: str | None = None
    snapshot_url: str | None = None
    service_node: bool = False
    home: str | None = None
    pruning_enabled: bool = False
    blocks_to_keep: int = 100000
    block_policy_mode: str = "on_demand"
    block_policy_interval: str = "0s"
    tracer_mode: str = "python_line_v1"
    transaction_trace_logging: bool = False
    app_log_level: str = "INFO"
    app_log_json: bool = False
    app_log_rotation_hours: int = 1
    app_log_retention_days: int = 7
    simulation_enabled: bool = True
    simulation_max_concurrency: int = 2
    simulation_timeout_ms: int = 3000
    simulation_max_stamps: int = 1_000_000
    parallel_execution_enabled: bool = False
    parallel_execution_workers: int = 0
    parallel_execution_min_transactions: int = 8
    operator_profile: str | None = None
    monitoring_profile: str | None = None
    dashboard_enabled: bool = False
    monitoring_enabled: bool = False
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    intentkit_enabled: bool = False
    intentkit_network_id: str | None = None
    intentkit_host: str = "127.0.0.1"
    intentkit_port: int = 38000
    intentkit_api_port: int = 38080
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class NetworkTemplate:
    name: str
    display_name: str
    description: str
    runtime_backend: str = "xian-stack"
    block_policy_mode: str = "on_demand"
    block_policy_interval: str = "0s"
    tracer_mode: str = "python_line_v1"
    transaction_trace_logging: bool = False
    app_log_level: str = "INFO"
    app_log_json: bool = False
    app_log_rotation_hours: int = 1
    app_log_retention_days: int = 7
    simulation_enabled: bool = True
    simulation_max_concurrency: int = 2
    simulation_timeout_ms: int = 3000
    simulation_max_stamps: int = 1_000_000
    parallel_execution_enabled: bool = False
    parallel_execution_workers: int = 0
    parallel_execution_min_transactions: int = 8
    operator_profile: str | None = None
    monitoring_profile: str | None = None
    bootstrap_node_name: str | None = None
    additional_validator_names: list[str] = field(default_factory=list)
    service_node: bool = False
    dashboard_enabled: bool = False
    monitoring_enabled: bool = False
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    intentkit_enabled: bool = False
    intentkit_network_id: str | None = None
    intentkit_host: str = "127.0.0.1"
    intentkit_port: int = 38000
    intentkit_api_port: int = 38080
    pruning_enabled: bool = False
    blocks_to_keep: int = 100000
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class SolutionPackStarterStep:
    title: str
    commands: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class SolutionPackStarterFlow:
    name: str
    display_name: str
    template: str
    summary: str
    network_name: str | None = None
    node_name: str | None = None
    steps: list[SolutionPackStarterStep] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class SolutionPack:
    name: str
    display_name: str
    description: str
    use_case: str
    recommended_local_template: str
    recommended_remote_template: str
    docs_path: str
    example_dir: str
    contract_paths: list[str] = field(default_factory=list)
    starter_flows: list[SolutionPackStarterFlow] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


def _normalize_json_value(value, *, preserve_runtime_types: bool = False):
    if isinstance(value, dict):
        return {
            str(key): _normalize_json_value(
                nested, preserve_runtime_types=preserve_runtime_types
            )
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [
            _normalize_json_value(
                item, preserve_runtime_types=preserve_runtime_types
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return [
            _normalize_json_value(
                item, preserve_runtime_types=preserve_runtime_types
            )
            for item in value
        ]
    if isinstance(value, (ContractingDecimal, decimal.Decimal, Datetime)):
        if preserve_runtime_types:
            return json.loads(encode(value))
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
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        raise FileExistsError(
            f"{path} already exists; pass --force to overwrite"
        )
    normalized = _normalize_json_value(
        payload, preserve_runtime_types=preserve_runtime_types
    )
    path.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_network_manifest(path: Path) -> dict:
    return normalize_network_manifest(read_json(path))


def read_node_profile(path: Path) -> dict:
    return normalize_node_profile(read_json(path))


def read_network_template(path: Path) -> dict:
    return normalize_network_template(read_json(path))


def read_solution_pack(path: Path) -> dict:
    return normalize_solution_pack(read_json(path))


def read_recovery_plan(path: Path) -> dict:
    return normalize_recovery_plan(read_json(path))
