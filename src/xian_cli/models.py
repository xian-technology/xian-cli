from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1
SUPPORTED_NETWORK_MODES = {"join", "create"}
SUPPORTED_RUNTIME_BACKENDS = {"xian-stack"}
SUPPORTED_BLOCK_POLICY_MODES = {"on_demand", "idle_interval", "periodic"}
SUPPORTED_TRACER_MODES = {"python_line_v1", "native_instruction_v1"}


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
    }


def normalize_node_profile(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("node profile must be a JSON object")

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
        "dashboard_enabled": _require_bool(
            payload, "dashboard_enabled", default=False
        ),
        "dashboard_host": _require_str(payload, "dashboard_host")
        if "dashboard_host" in payload
        else "127.0.0.1",
        "dashboard_port": _require_int(payload, "dashboard_port", default=8080),
    }


@dataclass(slots=True)
class NetworkManifest:
    name: str
    chain_id: str
    mode: str = "join"
    runtime_backend: str = "xian-stack"
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
    dashboard_enabled: bool = False
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


def write_json(path: Path, payload: dict, *, force: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        raise FileExistsError(
            f"{path} already exists; pass --force to overwrite"
        )
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_network_manifest(path: Path) -> dict:
    return normalize_network_manifest(read_json(path))


def read_node_profile(path: Path) -> dict:
    return normalize_node_profile(read_json(path))
