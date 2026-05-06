from __future__ import annotations

from dataclasses import asdict, dataclass, field

SETUP_CONTRACT_VERSION = 1


@dataclass(frozen=True, slots=True)
class BackendRequest:
    command: str
    options: dict[str, object] = field(default_factory=dict)
    schema_version: int = SETUP_CONTRACT_VERSION

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def drop_none(payload: dict[str, object | None]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if value is not None}
