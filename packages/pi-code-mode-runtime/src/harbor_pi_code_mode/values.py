"""Validate untrusted JSON at protocol and provider boundaries."""

import math
from typing import cast


def record(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ValueError("Expected a JSON object")
    return cast(dict[str, object], value)


def number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Expected a nonnegative finite number")
    if not math.isfinite(value) or value < 0:
        raise ValueError("Expected a nonnegative finite number")
    return float(value)


def count(value: object) -> int:
    result = number(value)
    if not result.is_integer():
        raise ValueError("Expected an integer count")
    return int(result)
