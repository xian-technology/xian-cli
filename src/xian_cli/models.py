from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import json


@dataclass(slots=True)
class NetworkManifest:
    name: str
    chain_id: str
    genesis_source: str | None = None
    snapshot_url: str | None = None
    seed_nodes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class NodeProfile:
    name: str
    chain_id: str
    moniker: str
    seeds: list[str] = field(default_factory=list)
    genesis_url: str | None = None
    snapshot_url: str | None = None
    service_node: bool = False
    home: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def write_json(path: Path, payload: dict, *, force: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        raise FileExistsError(f"{path} already exists; pass --force to overwrite")
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

