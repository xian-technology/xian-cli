from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(slots=True)
class NetworkManifest:
    name: str
    chain_id: str
    mode: str = "join"
    runtime_backend: str = "xian-stack"
    genesis_source: str | None = None
    snapshot_url: str | None = None
    seed_nodes: list[str] = field(default_factory=list)

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
