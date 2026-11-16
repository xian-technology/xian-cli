"""Intended CometBFT build metadata; never attestation of a running binary.

The private capability handshake and verified historical bootstrap enforce safety.
These checks only catch an unsupported or incomplete configured artifact early.
"""

from __future__ import annotations

import re

HISTORY_BUILD_FIELDS = (
    "cometbft_source_commit",
    "cometbft_patch_path",
    "cometbft_patch_sha256",
    "cometbft_history_revision",
)


def _require_pattern(build: dict, field: str, pattern: str) -> str:
    value = build.get(field)
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError(f"history bootstrap metadata has invalid {field}")
    return value


def normalize_history_build(build: dict) -> dict[str, str]:
    """Keep legacy ordinary manifests, but reject partial/new malformed identity."""
    if not any(field in build for field in HISTORY_BUILD_FIELDS):
        return {}
    identity = {
        "cometbft_source_commit": _require_pattern(
            build, "cometbft_source_commit", r"[0-9a-f]{40}"
        ),
        "cometbft_patch_path": _require_pattern(
            build,
            "cometbft_patch_path",
            r"docker/cometbft-patches/[A-Za-z0-9][A-Za-z0-9_.-]*\.patch",
        ),
        "cometbft_patch_sha256": _require_pattern(build, "cometbft_patch_sha256", r"[0-9a-f]{64}"),
        "cometbft_history_revision": _require_pattern(build, "cometbft_history_revision", r"1"),
    }
    if build.get("cometbft_version") != "0.40.0":
        raise ValueError("history bootstrap metadata requires CometBFT 0.40.0 for a fresh chain")
    expected_url = (
        "https://codeload.github.com/cometbft/cometbft/tar.gz/" + identity["cometbft_source_commit"]
    )
    if build.get("cometbft_source_url") != expected_url:
        raise ValueError("history bootstrap metadata source URL must match its source commit")
    _require_pattern(build, "cometbft_source_sha256", r"[0-9a-f]{64}")
    return identity


def history_build_metadata(manifest: object) -> dict[str, str]:
    if not isinstance(manifest, dict):
        raise ValueError("state sync requires history-aware intended release metadata")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise ValueError("history bootstrap release metadata requires integer schema_version 1")
    build = manifest.get("build")
    if not isinstance(build, dict):
        raise ValueError("history bootstrap release metadata requires a build object")
    identity = normalize_history_build(build)
    if not identity:
        raise ValueError("state sync requires authenticated-history revision 1 build metadata")
    return {
        **{
            key: build[key]
            for key in ("cometbft_version", "cometbft_source_url", "cometbft_source_sha256")
        },
        **identity,
    }
