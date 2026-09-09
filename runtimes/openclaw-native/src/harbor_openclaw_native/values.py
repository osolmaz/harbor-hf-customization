"""Small validators for data received outside the adapter."""

import math


def record(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("Expected a JSON object")
        result[key] = item
    return result


def number(value: object) -> float:
    valid_type = isinstance(value, (int, float)) and not isinstance(value, bool)
    result = float(value) if valid_type else -1.0
    if not valid_type or result < 0 or not math.isfinite(result):
        raise ValueError("Expected a nonnegative finite number")
    return result


def count(value: object) -> int:
    result = number(value)
    integer = int(result)
    if result != integer:
        raise ValueError("Expected an integer count")
    return integer
