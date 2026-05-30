from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import urlparse

from xian_cli.abci_bridge import (
    get_genesis_builder_module,
    get_node_admin_module,
)
from xian_cli.config_repo import (
    resolve_configs_dir,
    resolve_network_manifest_path,
)
from xian_cli.models import read_json, read_network_manifest, read_node_profile
from xian_cli.runtime import default_home_for_backend, fetch_json


def _resolve_path(
    value: str | None, *, base_dir: Path, fallback_dir: Path | None = None
) -> Path | None:
    if value is None:
        return None

    raw_path = Path(value).expanduser()
    if raw_path.is_absolute():
        return raw_path

    candidates = [base_dir / raw_path]
    if fallback_dir is not None:
        candidates.append(fallback_dir / raw_path)

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()

    return candidates[0].resolve()


def _resolve_stack_dir_from_profile(
    *,
    base_dir: Path,
    profile: dict,
    explicit_stack_dir: Path | None,
) -> Path | None:
    stack_dir = explicit_stack_dir
    if stack_dir is None and profile.get("stack_dir"):
        stack_dir = _resolve_path(
            profile["stack_dir"],
            base_dir=base_dir,
        )
    if stack_dir is not None and not stack_dir.is_absolute():
        stack_dir = (base_dir / stack_dir).resolve()
    return stack_dir


def _load_genesis_payload(genesis_source: str, *, base_dir: Path, manifest_path: Path) -> dict:
    parsed = urlparse(genesis_source)
    if parsed.scheme in {"http", "https"}:
        return fetch_json(genesis_source)

    genesis_path = _resolve_path(
        genesis_source,
        base_dir=base_dir,
        fallback_dir=manifest_path.parent,
    )
    if genesis_path is None or not genesis_path.exists():
        raise FileNotFoundError(f"genesis source not found: {genesis_source}")

    return read_json(genesis_path)


def _build_bundle_genesis_payload(
    *,
    base_dir: Path,
    chain_id: str,
    genesis_bundle: str,
    genesis_time: str | None,
    configs_dir: Path | None,
) -> dict:
    genesis_builder = get_genesis_builder_module()
    resolved_configs_dir = resolve_configs_dir(base_dir, explicit=configs_dir)
    return genesis_builder.build_bundle_network_genesis(
        chain_id=chain_id,
        network=genesis_bundle,
        contracts_dir=resolved_configs_dir / "contracts",
        genesis_time=genesis_time,
    )


def _resolve_effective_genesis_payload(
    *,
    profile: dict,
    network: dict,
    base_dir: Path,
    manifest_path: Path,
    configs_dir: Path | None,
) -> tuple[dict, str]:
    genesis = profile.get("genesis") or network.get("genesis")
    if not isinstance(genesis, dict):
        raise ValueError("network manifest must define genesis")

    if genesis.get("kind") == "source":
        genesis_source = genesis["source"]
        return (
            _load_genesis_payload(
                genesis_source,
                base_dir=base_dir,
                manifest_path=manifest_path,
            ),
            genesis_source,
        )

    if genesis.get("kind") == "bundle":
        genesis_bundle = genesis["bundle"]
        return (
            _build_bundle_genesis_payload(
                base_dir=base_dir,
                chain_id=network["chain_id"],
                genesis_bundle=genesis_bundle,
                genesis_time=genesis.get("genesis_time"),
                configs_dir=configs_dir,
            ),
            f"bundle:{genesis_bundle}",
        )

    raise ValueError("genesis.kind must be source or bundle")


def _extract_priv_validator_key(payload: dict) -> dict:
    if "priv_validator_key" in payload:
        return payload["priv_validator_key"]

    if {"address", "pub_key", "priv_key"}.issubset(payload.keys()):
        return payload

    raise ValueError(
        "validator key file must contain either priv_validator_key or a raw "
        "priv_validator_key.json payload"
    )


def _extract_validator_private_key_hex(payload: dict) -> str:
    private_key_hex = payload.get("validator_private_key_hex")
    if private_key_hex is not None:
        return private_key_hex

    priv_validator_key = _extract_priv_validator_key(payload)
    try:
        raw_private_key = base64.b64decode(priv_validator_key["priv_key"]["value"].encode("ascii"))
    except (KeyError, ValueError) as exc:
        raise ValueError("validator key file does not contain a usable private key") from exc

    if len(raw_private_key) < 32:
        raise ValueError("validator key file does not contain a usable private key")
    return raw_private_key[:32].hex()


def _extract_validator_public_key_hex(payload: dict) -> str:
    public_key_hex = payload.get("validator_public_key_hex")
    if public_key_hex is not None:
        return public_key_hex

    priv_validator_key = _extract_priv_validator_key(payload)
    try:
        raw_public_key = base64.b64decode(priv_validator_key["pub_key"]["value"].encode("ascii"))
    except (KeyError, ValueError) as exc:
        raise ValueError("validator key file does not contain a usable public key") from exc

    if len(raw_public_key) != 32:
        raise ValueError("validator key file does not contain a usable public key")
    return raw_public_key.hex()


def _build_creation_validator_entries(
    *,
    validators: list[dict[str, object]],
) -> list[dict[str, object]]:
    return [
        {
            "account_public_key": _extract_validator_public_key_hex(
                validator["validator_key_payload"]
            ),
            "name": validator["name"],
            "power": validator["power"],
            "priv_validator_key": _extract_priv_validator_key(validator["validator_key_payload"]),
        }
        for validator in validators
    ]


def _build_creation_genesis(
    *,
    chain_id: str,
    founder_private_key: str,
    validators: list[dict[str, object]],
    genesis_bundle: str,
) -> dict:
    genesis_builder = get_genesis_builder_module()
    return genesis_builder.build_local_network_genesis(
        chain_id=chain_id,
        founder_private_key=founder_private_key,
        validators=_build_creation_validator_entries(validators=validators),
        network=genesis_bundle,
    )


def _resolve_home(
    *,
    base_dir: Path,
    profile: dict,
    profile_path: Path,
    stack_dir: Path | None,
    explicit_home: Path | None = None,
) -> Path:
    resolved_home = explicit_home
    if resolved_home is not None and not resolved_home.is_absolute():
        resolved_home = (base_dir / resolved_home).resolve()

    home = resolved_home or _resolve_path(
        profile.get("home"),
        base_dir=base_dir,
        fallback_dir=profile_path.parent,
    )
    if home is None:
        home = default_home_for_backend(
            base_dir=base_dir,
            stack_dir=stack_dir,
        )
    return home


def _resolve_effective_snapshot_url(
    *,
    profile: dict,
    network: dict,
    explicit_snapshot_url: str | None = None,
) -> str | None:
    return explicit_snapshot_url or profile.get("snapshot_url") or network.get("snapshot_url")


def _resolve_effective_snapshot_signing_keys(
    *,
    profile: dict,
    network: dict,
) -> list[str]:
    profile_keys = profile.get("snapshot_signing_keys")
    if isinstance(profile_keys, list) and profile_keys:
        return list(profile_keys)
    network_keys = network.get("snapshot_signing_keys")
    if isinstance(network_keys, list):
        return list(network_keys)
    return []


def _restore_snapshot(
    *,
    base_dir: Path,
    profile: dict,
    profile_path: Path,
    network: dict,
    stack_dir: Path | None,
    explicit_home: Path | None = None,
    explicit_snapshot_url: str | None = None,
) -> dict:
    home = _resolve_home(
        base_dir=base_dir,
        profile=profile,
        profile_path=profile_path,
        stack_dir=stack_dir,
        explicit_home=explicit_home,
    )
    config_path = home / "config" / "config.toml"
    if not config_path.exists():
        raise FileNotFoundError(
            f"{config_path} does not exist; run `xian node init {profile['name']}` first"
        )

    snapshot_url = _resolve_effective_snapshot_url(
        profile=profile,
        network=network,
        explicit_snapshot_url=explicit_snapshot_url,
    )
    snapshot_signing_keys = _resolve_effective_snapshot_signing_keys(
        profile=profile,
        network=network,
    )
    if not snapshot_url:
        raise ValueError(
            "no snapshot source configured; "
            "set snapshot_url in the network manifest or node profile"
        )

    node_admin = get_node_admin_module()
    snapshot_archive_name = node_admin.apply_snapshot_archive(
        snapshot_url,
        home,
        trusted_manifest_public_keys=snapshot_signing_keys,
        expected_chain_id=network.get("chain_id"),
    )
    return {
        "home": str(home),
        "snapshot_url": snapshot_url,
        "snapshot_archive_name": snapshot_archive_name,
    }


def _load_profile_and_network(
    *,
    base_dir: Path,
    name: str,
    profile_arg: Path | None,
    network_arg: Path | None,
    configs_dir: Path | None = None,
) -> tuple[Path, dict, Path, dict]:
    profile_path = profile_arg or base_dir / "nodes" / f"{name}.json"
    if not profile_path.is_absolute():
        profile_path = (base_dir / profile_path).resolve()
    if not profile_path.exists():
        raise FileNotFoundError(f"node profile not found: {profile_path}")

    profile = read_node_profile(profile_path)
    network_name = profile.get("network")
    if not network_name:
        raise ValueError("node profile is missing network; recreate it with xian network join")

    network_path = resolve_network_manifest_path(
        base_dir=base_dir,
        network_name=network_name,
        explicit_manifest=network_arg,
        configs_dir=configs_dir,
    )
    network = read_network_manifest(network_path)
    return profile_path, profile, network_path, network
