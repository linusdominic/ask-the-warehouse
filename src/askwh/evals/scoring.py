"""Scoring without a model: compare what an agent retrieved and said with reference rows.

Two checks per question. ``result_match`` asks whether a query the agent ran returned the
reference figures. ``answer_match`` asks whether the written answer states the key figure.
A question counts as correct when both hold.
"""

import re
from typing import Any

_NUMBER = re.compile(
    r"(?<![\w.])(-?\d[\d,]*(?:\.\d+)?)\s?(%|[KkMmBb]\b|thousand|million|billion)?", re.IGNORECASE
)
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}


def numbers_in(text: str) -> list[tuple[float, bool]]:
    """Every number in a sentence as (value, is_percentage), with K, M and B expanded."""
    found = []
    for digits, suffix in _NUMBER.findall(text):
        value = float(digits.replace(",", ""))
        suffix = suffix.lower()
        if suffix == "%":
            found.append((value, True))
        else:
            found.append((value * _SCALE.get(suffix, 1.0), False))
    return found


def close(expected: float, actual: float, unit: str, written: bool = False) -> bool:
    """Whether two figures are the same, allowing for rounding.

    Query results must agree closely. A written answer may round the way people do: $1.24M
    for 1,243,567, or 3.4% for 0.0338.
    """
    if unit == "ratio":
        return abs(expected - actual) <= (0.00055 if written else 0.00006)
    if written:
        return abs(expected - actual) <= max(0.5, abs(expected) * 0.01)
    return abs(expected - actual) <= max(0.006, abs(expected) * 1e-6)


def _numeric(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _same_label(expected: Any, actual: Any) -> bool:
    if not isinstance(actual, str):
        return False
    wanted, got = str(expected).strip().lower(), actual.strip().lower()
    return wanted == got or (len(wanted) == 10 and got.startswith(wanted))  # dates


def _has_value(row: list[Any], expected: float, unit: str) -> bool:
    for value in row:
        if not _numeric(value):
            continue
        if close(expected, value, unit):
            return True
        # A hand-written query may return a rate as a percentage: 3.38 for 0.0338.
        if unit == "ratio" and abs(expected * 100 - value) <= 0.006:
            return True
    return False


def retrieved_rows(events: list[dict[str, Any]]) -> list[list[Any]]:
    """Every row of every successful query in a turn, as lists of values."""
    rows: list[list[Any]] = []
    for event in events:
        if event.get("type") != "result":
            continue
        rows += [list(row.values()) for row in event["rows"]]
        comparison = event.get("comparison")
        if comparison and comparison.get("previous_value") is not None:
            rows.append([comparison["previous_value"]])
    return rows


def result_match(kind: str, reference: list[list[Any]], events: list[dict], unit: str) -> bool:
    rows = retrieved_rows(events)
    if not reference or not rows:
        return False
    if kind == "scalar":
        return any(_has_value(row, reference[0][0], unit) for row in rows)
    if kind == "comparison":
        current, previous = reference[0][0], reference[0][1]
        return all(any(_has_value(row, v, unit) for row in rows) for v in (current, previous))
    # breakdown and series: every reference row, label and value together, in one agent row
    return all(
        any(
            any(_same_label(label, cell) for cell in row) and _has_value(row, value, unit)
            for row in rows
        )
        for label, value in reference
    )


def answer_match(kind: str, reference: list[list[Any]], text: str, unit: str) -> bool | None:
    """Whether the written answer states the key figure. None when the kind is not scored."""
    numbers = numbers_in(text)
    if kind == "series" or not reference:
        return None

    def states(expected: float) -> bool:
        if unit == "ratio":
            return any(
                close(expected, value / 100, unit, written=True) for value, pct in numbers if pct
            )
        return any(close(expected, value, unit, written=True) for value, pct in numbers if not pct)

    if kind == "scalar":
        return states(reference[0][0])
    if kind == "breakdown":
        label, value = reference[0]
        return str(label).lower() in text.lower() and states(value)
    change = abs(reference[0][2])  # comparison: the relative change, as a percentage
    # Half a point of slack: "about 40%" is a fair way to say 40.4%.
    # The sign is ignored: "down 1.2%" and "(-1.2%)" say the same thing.
    return any(abs(abs(value) / 100 - change) <= 0.005 for value, pct in numbers if pct)
