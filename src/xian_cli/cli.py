from __future__ import annotations

import argparse
import os
import subprocess
import sys

from xian_cli.commands import catalog, doctor, network, node, recovery
from xian_cli.parser import build_parser as _build_parser

_fallback_node_endpoints = node._fallback_node_endpoints
_collect_node_status = node._collect_node_status
_HANDLER_MODULES = (catalog, doctor, network, node, recovery)


def build_parser() -> argparse.ArgumentParser:
    return _build_parser()


def _resolve_handler(args: argparse.Namespace):
    handler = getattr(args, "handler", None)
    if callable(handler):
        return handler

    handler_name = getattr(args, "handler_name", None)
    if isinstance(handler_name, str):
        for module in _HANDLER_MODULES:
            handler = getattr(module, handler_name, None)
            if callable(handler):
                return handler

    raise ValueError("parsed command has no callable handler")


def _should_raise_cli_errors(argv: list[str] | None) -> bool:
    return argv is not None or bool(os.environ.get("XIAN_CLI_DEBUG"))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        try:
            handler = _resolve_handler(args)
        except ValueError as exc:
            parser.error(str(exc))
        return handler(args)
    except KeyboardInterrupt:
        if _should_raise_cli_errors(argv):
            raise
        print("xian: interrupted", file=sys.stderr)
        return 130
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        if _should_raise_cli_errors(argv):
            raise
        print(f"xian: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
