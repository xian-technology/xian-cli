from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def get_node_setup_module():
    try:
        from xian import node_setup  # type: ignore

        return node_setup
    except ModuleNotFoundError as exc:
        if not exc.name.startswith("xian"):
            raise

    workspace_src = Path(__file__).resolve().parents[3] / "xian-abci" / "src"
    if workspace_src.exists():
        sys.path.insert(0, str(workspace_src))
        from xian import node_setup  # type: ignore

        return node_setup

    raise RuntimeError(
        "xian-abci helpers are required for node init; install xian-abci or run "
        "xian-cli from the shared workspace"
    )
