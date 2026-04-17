from __future__ import annotations

import dataclasses
import decimal
import json
import sys
from typing import Any

from xian_runtime_types.decimal import ContractingDecimal
from xian_runtime_types.time import Datetime


def to_jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return to_jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, (ContractingDecimal, decimal.Decimal, Datetime)):
        return str(value)
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    return value


def emit_json(payload: Any) -> None:
    json.dump(to_jsonable(payload), sys.stdout, indent=2)
    sys.stdout.write("\n")
