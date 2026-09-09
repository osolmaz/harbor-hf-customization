import math

import pytest

from harbor_openclaw_native.values import count, number, record


def test_valid_values() -> None:
    assert record({"ok": True}) == {"ok": True}
    assert number(1.25) == 1.25
    assert count(2.0) == 2


@pytest.mark.parametrize("value", [[], {1: "bad"}])
def test_record_rejects_non_string_object(value: object) -> None:
    with pytest.raises(ValueError, match="^Expected a JSON object$"):
        record(value)


@pytest.mark.parametrize("value", [None, True, "1", -1, math.inf, math.nan])
def test_number_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match="^Expected a nonnegative finite number$"):
        number(value)


def test_count_rejects_fraction() -> None:
    with pytest.raises(ValueError, match="^Expected an integer count$"):
        count(0.5)
