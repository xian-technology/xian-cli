from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path


def _get_workspace_src() -> Path:
    return Path(__file__).resolve().parents[3] / "xian-abci" / "src"


def _load_xian_module(module_name: str):
    try:
        module = __import__("xian", fromlist=[module_name])
        return getattr(module, module_name)
    except ModuleNotFoundError as exc:
        if not exc.name.startswith("xian"):
            raise

    workspace_src = _get_workspace_src()
    if workspace_src.exists():
        sys.path.insert(0, str(workspace_src))
        module = __import__("xian", fromlist=[module_name])
        return getattr(module, module_name)

    raise RuntimeError


@lru_cache(maxsize=1)
def get_node_setup_module():
    try:
        return _load_xian_module("node_setup")
    except RuntimeError as exc:
        raise RuntimeError(
            "xian-abci helpers are required for node init; "
            "install xian-abci or run xian-cli from the shared workspace"
        ) from exc


@lru_cache(maxsize=1)
def get_node_admin_module():
    try:
        return _load_xian_module("node_admin")
    except RuntimeError as exc:
        raise RuntimeError(
            "xian-abci helpers are required for snapshot restore; "
            "install xian-abci or run xian-cli from the shared workspace"
        ) from exc
