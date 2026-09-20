from __future__ import annotations

import hashlib
import json
from typing import Any, TypeAlias, Union

JsonValue: TypeAlias = Union[None, bool, int, float, str, list["JsonValue"], dict[str, "JsonValue"]]


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Value is not JSON-safe: {exc}") from exc


def validate_json(value: Any) -> JsonValue:
    return json.loads(canonical_json(value))


def arguments_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()

