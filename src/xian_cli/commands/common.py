from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from xian_cli.abci_bridge import get_node_setup_module
from xian_cli.models import SUPPORTED_NODE_IMAGE_MODES, write_json


def _block_age_seconds(block_time: object) -> float | None:
    """
    Seconds elapsed since ``block_time``.

    Surfaces sync lag in ``node status`` — fresh blocks return a small value,
    a stalled chain returns a large one. Returns None if the input is missing
    or unparseable so the caller can omit the field instead of showing 0.
    """
    if not isinstance(block_time, str) or not block_time:
        return None
    try:
        # CometBFT emits e.g. "2024-01-01T12:00:00.123456789Z" — Python's
        # datetime can't parse sub-microsecond precision or the trailing Z,
        # so normalize before handing to fromisoformat.
        normalized = block_time.rstrip("Z")
        if "." in normalized:
            head, frac = normalized.split(".", 1)
            frac = frac[:6]
            normalized = f"{head}.{frac}"
        parsed = datetime.fromisoformat(normalized).replace(tzinfo=UTC)
    except ValueError:
        return None
    delta = datetime.now(UTC) - parsed
    return max(delta.total_seconds(), 0.0)


def _write_validator_material_files(
    *,
    out_dir: Path,
    private_key: str | None = None,
    force: bool = False,
) -> Path:
    node_setup = get_node_setup_module()
    payload = node_setup.generate_validator_material(private_key)
    write_json(
        out_dir / "priv_validator_key.json",
        payload["priv_validator_key"],
        force=force,
    )
    metadata_path = out_dir / "validator_key_info.json"
    write_json(metadata_path, payload, force=force)
    return metadata_path


def _stringify_path_for_profile(path: Path, *, base_dir: Path) -> str:
    resolved_path = path
    if not resolved_path.is_absolute():
        resolved_path = (base_dir / resolved_path).resolve()
    else:
        resolved_path = resolved_path.resolve()
    try:
        return str(resolved_path.relative_to(base_dir.resolve()))
    except ValueError:
        return str(resolved_path)


def _default_network_manifest_path(base_dir: Path, network_name: str) -> Path:
    return base_dir / "networks" / network_name / "manifest.json"


def _resolve_output_path(
    *,
    base_dir: Path,
    explicit_output: Path | None,
    default_path: Path,
) -> Path:
    target = explicit_output or default_path
    if not target.is_absolute():
        target = (base_dir / target).resolve()
    return target


def _pick_template_value(
    explicit,
    template_value,
    default,
):
    if explicit is not None:
        return explicit
    if template_value is not None:
        return template_value
    return default


def _default_intentkit_network_id(network_name: str | None) -> str:
    normalized = (network_name or "").strip().lower()
    if normalized in {"mainnet", "xian-mainnet"}:
        return "xian-mainnet"
    if normalized in {"testnet", "xian-testnet"}:
        return "xian-testnet"
    if normalized in {"devnet", "xian-devnet"}:
        return "xian-devnet"
    return "xian-localnet"


def _effective_node_image_config(
    profile: dict[str, object],
    network: dict[str, object] | None = None,
) -> tuple[str, str | None, str | None]:
    network_mode = None if network is None else network.get("node_image_mode") or "local_build"
    return _resolve_node_image_settings(
        node_image_mode=str(profile.get("node_image_mode") or network_mode or "local_build"),
        node_integrated_image=(
            profile.get("node_integrated_image")
            or (None if network is None else network.get("node_integrated_image"))
        ),
        node_split_image=(
            profile.get("node_split_image")
            or (None if network is None else network.get("node_split_image"))
        ),
    )


def _effective_node_release_manifest(
    profile: dict[str, object],
    network: dict[str, object] | None = None,
) -> dict[str, object] | None:
    node_image_mode, _, _ = _effective_node_image_config(profile, network)
    if node_image_mode != "registry":
        return None
    profile_manifest = profile.get("node_release_manifest")
    if isinstance(profile_manifest, dict):
        return profile_manifest
    network_manifest = None if network is None else network.get("node_release_manifest")
    return network_manifest if isinstance(network_manifest, dict) else None


def _stack_runtime_profile_kwargs(
    profile: dict[str, object],
    network: dict[str, object] | None = None,
) -> dict[str, object]:
    node_image_mode, node_integrated_image, node_split_image = _effective_node_image_config(
        profile, network
    )
    services = profile.get("services")
    if not isinstance(services, dict):
        services = {}
    bds = services.get("bds") if isinstance(services.get("bds"), dict) else {}
    dashboard = services.get("dashboard") if isinstance(services.get("dashboard"), dict) else {}
    monitoring = services.get("monitoring") if isinstance(services.get("monitoring"), dict) else {}
    intentkit = services.get("intentkit") if isinstance(services.get("intentkit"), dict) else {}
    dex_automation = (
        services.get("dex_automation") if isinstance(services.get("dex_automation"), dict) else {}
    )
    shielded_relayer = (
        services.get("shielded_relayer")
        if isinstance(services.get("shielded_relayer"), dict)
        else {}
    )
    return {
        "node_image_mode": node_image_mode,
        "node_integrated_image": node_integrated_image,
        "node_split_image": node_split_image,
        "bds_enabled": bool(bds.get("enabled")),
        "dashboard_enabled": bool(dashboard.get("enabled")),
        "monitoring_enabled": bool(monitoring.get("enabled")),
        "dashboard_host": str(dashboard.get("host", "127.0.0.1")),
        "dashboard_port": int(dashboard.get("port", 8080)),
        "intentkit_enabled": bool(intentkit.get("enabled")),
        "intentkit_network_id": str(
            intentkit.get("network_id") or _default_intentkit_network_id(profile.get("network"))
        ),
        "intentkit_host": str(intentkit.get("host", "127.0.0.1")),
        "intentkit_port": int(intentkit.get("port", 38000)),
        "intentkit_api_port": int(intentkit.get("api_port", 38080)),
        "dex_automation_enabled": bool(dex_automation.get("enabled")),
        "dex_automation_host": str(dex_automation.get("host", "127.0.0.1")),
        "dex_automation_port": int(dex_automation.get("port", 38280)),
        "dex_automation_config": dex_automation.get("config"),
        "shielded_relayer_enabled": bool(shielded_relayer.get("enabled")),
        "shielded_relayer_host": str(shielded_relayer.get("host", "127.0.0.1")),
        "shielded_relayer_port": int(shielded_relayer.get("port", 38180)),
    }


def _network_shielded_relayer_endpoints(
    network: dict[str, object] | None,
) -> dict[str, object]:
    relayers_raw = []
    if isinstance(network, dict):
        list_value = network.get("shielded_relayers")
        if isinstance(list_value, list):
            relayers_raw = [item for item in list_value if isinstance(item, dict)]
    if not relayers_raw:
        return {}
    relayers = sorted(
        relayers_raw,
        key=lambda item: (
            int(item.get("priority", 100)),
            str(item.get("id", "")),
            str(item.get("base_url", "")),
        ),
    )
    catalog: list[dict[str, object]] = []
    for relayer in relayers:
        base_url = relayer.get("base_url")
        if not isinstance(base_url, str) or not base_url:
            continue
        normalized_base = base_url.rstrip("/")
        catalog.append(
            {
                "id": relayer.get("id"),
                "base_url": normalized_base,
                "auth_scheme": relayer.get("auth_scheme"),
                "public_info": relayer.get("public_info"),
                "public_quote": relayer.get("public_quote"),
                "public_job_lookup": relayer.get("public_job_lookup"),
                "priority": relayer.get("priority", 100),
                "submission_kinds": relayer.get("submission_kinds", []),
                "endpoints": {
                    "shielded_relayer": normalized_base,
                    "shielded_relayer_info": f"{normalized_base}/v1/info",
                    "shielded_relayer_metrics": f"{normalized_base}/metrics",
                    "shielded_relayer_quote": f"{normalized_base}/v1/quote",
                    "shielded_relayer_jobs": f"{normalized_base}/v1/jobs",
                },
            }
        )
    if not catalog:
        return {}
    primary = catalog[0]
    endpoints = dict(primary["endpoints"])
    endpoints["shielded_relayer_primary_id"] = str(primary.get("id") or "")
    endpoints["shielded_relayers"] = catalog
    return endpoints


def _resolve_node_image_settings(
    *,
    node_image_mode: str,
    node_integrated_image: str | None,
    node_split_image: str | None,
) -> tuple[str, str | None, str | None]:
    if node_image_mode not in SUPPORTED_NODE_IMAGE_MODES:
        raise ValueError(f"node_image_mode must be one of {sorted(SUPPORTED_NODE_IMAGE_MODES)}")
    if node_image_mode == "registry" and (not node_integrated_image or not node_split_image):
        raise ValueError(
            "registry node image mode requires both --node-integrated-image and --node-split-image"
        )
    return node_image_mode, node_integrated_image, node_split_image
