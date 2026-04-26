from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CONTRACT_BUNDLE_SCHEMA = "xian.contract_bundle.v1"


def _require_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _require_sha256(payload: dict[str, Any], key: str) -> str:
    value = _require_str(payload, key).lower()
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{key} must be a 64-character lowercase hex sha256")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_bundle_source_path(bundle_path: Path, source_path: str) -> Path:
    raw_path = Path(source_path)
    if raw_path.is_absolute() or ".." in raw_path.parts:
        raise ValueError(
            f"contract path must stay inside bundle: {source_path}"
        )
    resolved = (bundle_path.parent / raw_path).resolve()
    try:
        resolved.relative_to(bundle_path.parent.resolve())
    except ValueError as exc:
        raise ValueError(
            f"contract path escapes bundle directory: {source_path}"
        ) from exc
    return resolved


def read_contract_bundle(bundle_path: Path) -> dict[str, Any]:
    with bundle_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("contract bundle must be a JSON object")
    return payload


def validate_contract_bundle(bundle_path: Path) -> dict[str, Any]:
    resolved_bundle_path = bundle_path.expanduser().resolve()
    payload = read_contract_bundle(resolved_bundle_path)

    schema = _require_str(payload, "schema")
    if schema != CONTRACT_BUNDLE_SCHEMA:
        raise ValueError(f"unsupported contract bundle schema: {schema}")
    if payload.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")

    bundle_name = _require_str(payload, "name")
    _require_str(payload, "display_name")
    _require_str(payload, "version")

    contracts = payload.get("contracts")
    if (
        not isinstance(contracts, list)
        or not contracts
        or any(not isinstance(item, dict) for item in contracts)
    ):
        raise ValueError("contracts must be a non-empty list of objects")

    names: set[str] = set()
    roles: set[str] = set()
    normalized_contracts: list[dict[str, Any]] = []
    for contract in contracts:
        name = _require_str(contract, "name")
        if name in names:
            raise ValueError(f"duplicate contract name in bundle: {name}")
        names.add(name)

        role = contract.get("role")
        if role is not None:
            if not isinstance(role, str) or not role:
                raise ValueError(f"{name}.role must be a non-empty string")
            if role in roles:
                raise ValueError(f"duplicate contract role in bundle: {role}")
            roles.add(role)

        source_path = _require_str(contract, "path")
        resolved_source_path = _resolve_bundle_source_path(
            resolved_bundle_path, source_path
        )
        if not resolved_source_path.exists():
            raise FileNotFoundError(
                f"bundle contract source not found: {resolved_source_path}"
            )

        expected_sha256 = _require_sha256(contract, "sha256")
        actual_sha256 = _sha256_file(resolved_source_path)
        if actual_sha256 != expected_sha256:
            raise ValueError(
                f"{source_path} sha256 mismatch: expected "
                f"{expected_sha256}, got {actual_sha256}"
            )

        deploy_order = contract.get("deploy_order", 100)
        if isinstance(deploy_order, bool) or not isinstance(deploy_order, int):
            raise ValueError(f"{name}.deploy_order must be an integer")
        default_chi = contract.get("default_chi")
        if default_chi is not None and (
            isinstance(default_chi, bool)
            or not isinstance(default_chi, int)
            or default_chi <= 0
        ):
            raise ValueError(f"{name}.default_chi must be a positive integer")
        deploy_default = contract.get("deploy_default", True)
        if not isinstance(deploy_default, bool):
            raise ValueError(f"{name}.deploy_default must be a boolean")

        normalized_contracts.append(
            {
                "name": name,
                "role": role,
                "path": source_path,
                "sha256": actual_sha256,
                "deploy_order": deploy_order,
                "default_chi": default_chi,
                "deploy_default": deploy_default,
            }
        )

    return {
        "ok": True,
        "path": str(resolved_bundle_path),
        "schema": schema,
        "schema_version": 1,
        "name": bundle_name,
        "version": payload["version"],
        "contracts": sorted(
            normalized_contracts,
            key=lambda item: (item["deploy_order"], item["name"]),
        ),
    }


def contract_by_role(
    bundle: dict[str, Any],
    role: str,
) -> dict[str, Any] | None:
    for contract in bundle.get("contracts", []):
        if isinstance(contract, dict) and contract.get("role") == role:
            return contract
    return None


def read_contract_source_from_bundle(
    bundle_path: Path,
    contract: dict[str, Any],
) -> str:
    source_path = _resolve_bundle_source_path(
        bundle_path.expanduser().resolve(),
        _require_str(contract, "path"),
    )
    expected_sha256 = _require_sha256(contract, "sha256")
    actual_sha256 = _sha256_file(source_path)
    if actual_sha256 != expected_sha256:
        raise ValueError(
            f"{contract['path']} sha256 mismatch: expected "
            f"{expected_sha256}, got {actual_sha256}"
        )
    return source_path.read_text(encoding="utf-8")
