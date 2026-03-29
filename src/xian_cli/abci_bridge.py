from __future__ import annotations

from functools import lru_cache
from importlib import import_module


def _load_xian_module(module_name: str):
    try:
        return import_module(f"xian.{module_name}")
    except ModuleNotFoundError as exc:
        if exc.name != "xian":
            raise
        raise RuntimeError from exc


@lru_cache(maxsize=1)
def get_node_setup_module():
    try:
        return _load_xian_module("node_setup")
    except RuntimeError as exc:
        raise RuntimeError(
            "xian-abci helpers are required for node init; "
            "install xian-tech-abci in the current environment"
        ) from exc


@lru_cache(maxsize=1)
def get_node_admin_module():
    try:
        return _load_xian_module("node_admin")
    except RuntimeError as exc:
        raise RuntimeError(
            "xian-abci helpers are required for snapshot restore; "
            "install xian-tech-abci in the current environment"
        ) from exc


@lru_cache(maxsize=1)
def get_genesis_builder_module():
    try:
        return _load_xian_module("genesis_builder")
    except RuntimeError as exc:
        raise RuntimeError(
            "xian-abci helpers are required for network creation; "
            "install xian-tech-abci in the current environment"
        ) from exc
