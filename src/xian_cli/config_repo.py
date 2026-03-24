from __future__ import annotations

import os
from pathlib import Path

CONFIGS_REPO_NAME = "xian-configs"


def resolve_configs_dir(
    base_dir: Path, *, explicit: Path | None = None
) -> Path:
    candidates: list[Path] = []

    if explicit is not None:
        candidates.append(explicit)

    env_value = os.environ.get("XIAN_CONFIGS_DIR")
    if env_value:
        candidates.append(Path(env_value))

    candidates.append(base_dir / CONFIGS_REPO_NAME)
    candidates.append(Path(__file__).resolve().parents[3] / CONFIGS_REPO_NAME)

    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.exists():
            return resolved

    raise FileNotFoundError(
        "unable to resolve xian-configs directory; "
        "pass --configs-dir, set XIAN_CONFIGS_DIR, or use the sibling "
        "workspace layout"
    )


def resolve_network_manifest_path(
    *,
    base_dir: Path,
    network_name: str,
    explicit_manifest: Path | None = None,
    configs_dir: Path | None = None,
) -> Path:
    if explicit_manifest is not None:
        manifest_path = explicit_manifest
        if not manifest_path.is_absolute():
            manifest_path = (base_dir / manifest_path).resolve()
        if not manifest_path.exists():
            raise FileNotFoundError(
                f"network manifest not found: {manifest_path}"
            )
        return manifest_path

    local_manifest_dir = (
        base_dir / "networks" / network_name / "manifest.json"
    ).resolve()
    if local_manifest_dir.exists():
        return local_manifest_dir

    resolved_configs_dir = resolve_configs_dir(base_dir, explicit=configs_dir)
    canonical_manifest = (
        resolved_configs_dir / "networks" / network_name / "manifest.json"
    ).resolve()
    if canonical_manifest.exists():
        return canonical_manifest

    raise FileNotFoundError(
        "network manifest not found in local workspace or xian-configs: "
        f"{local_manifest_dir} or {canonical_manifest}"
    )


def resolve_network_template_path(
    *,
    base_dir: Path,
    template_name: str,
    configs_dir: Path | None = None,
) -> Path:
    local_template = (
        base_dir / "templates" / f"{template_name}.json"
    ).resolve()
    if local_template.exists():
        return local_template

    resolved_configs_dir = resolve_configs_dir(base_dir, explicit=configs_dir)
    canonical_template = (
        resolved_configs_dir / "templates" / f"{template_name}.json"
    ).resolve()
    if canonical_template.exists():
        return canonical_template

    raise FileNotFoundError(
        "network template not found in local workspace or xian-configs: "
        f"{local_template} or {canonical_template}"
    )


def list_network_template_paths(
    *,
    base_dir: Path,
    configs_dir: Path | None = None,
) -> list[Path]:
    templates: dict[str, Path] = {}

    try:
        resolved_configs_dir = resolve_configs_dir(
            base_dir,
            explicit=configs_dir,
        )
    except FileNotFoundError:
        resolved_configs_dir = None

    if resolved_configs_dir is not None:
        canonical_dir = resolved_configs_dir / "templates"
        if canonical_dir.exists():
            for path in sorted(canonical_dir.glob("*.json")):
                templates[path.stem] = path.resolve()

    local_dir = base_dir / "templates"
    if local_dir.exists():
        for path in sorted(local_dir.glob("*.json")):
            templates[path.stem] = path.resolve()

    return [templates[name] for name in sorted(templates)]
