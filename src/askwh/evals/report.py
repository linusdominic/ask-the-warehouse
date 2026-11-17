"""Turns evaluation records into the tables in the README."""

import json
import math
import statistics
from pathlib import Path

LABELS = {
    "semantic": "Semantic layer (governed)",
    "sql_glossary": "Text-to-SQL + definitions",
    "sql_schema": "Text-to-SQL, schema only",
}


def load_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion."""
    if n == 0:
        return 0.0, 0.0
    p = successes / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - spread) / (1 + z * z / n), (centre + spread) / (1 + z * z / n)


def mcnemar_exact(wins: int, losses: int) -> float:
    """Two-sided exact McNemar p-value for paired outcomes on the same questions."""
    n = wins + losses
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(wins, losses) + 1)) / 2**n
    return min(1.0, 2 * tail)


def _pct(part: int, whole: int) -> str:
    return f"{part / whole * 100:.0f}%" if whole else "n/a"


def _modes(records: list[dict]) -> list[str]:
    return list(dict.fromkeys(r["mode"] for r in records))


def _of(records: list[dict], mode: str, category: str) -> list[dict]:
    return [r for r in records if r["mode"] == mode and r["category"] == category]


def accuracy_table(records: list[dict]) -> str:
    lines = [
        "| Approach | Correct (95% CI) | Right figures retrieved | Right figure stated | "
        "Both phrasings correct |",
        "| --- | --- | --- | --- | --- |",
    ]
    for mode in _modes(records):
        rows = _of(records, mode, "single")
        n = len(rows)
        correct = sum(r["correct"] for r in rows)
        low, high = wilson(correct, n)
        stated = [r for r in rows if r["answer_match"] is not None]
        groups: dict[str, list[bool]] = {}
        for r in rows:
            groups.setdefault(r["group"], []).append(r["correct"])
        pairs = [all(v) for v in groups.values() if len(v) > 1]
        lines.append(
            f"| {LABELS.get(mode, mode)} | {correct}/{n} = {_pct(correct, n)} "
            f"({low * 100:.0f}% to {high * 100:.0f}%) | "
            f"{_pct(sum(r['result_match'] for r in rows), n)} | "
            f"{_pct(sum(r['answer_match'] for r in stated), len(stated))} | "
            f"{sum(pairs)}/{len(pairs)} |"
        )
    return "\n".join(lines)


def kind_table(records: list[dict]) -> str:
    kinds = list(dict.fromkeys(r["kind"] for r in records if r["category"] == "single"))
    lines = ["| Approach | " + " | ".join(kinds) + " |", "| --- |" + " --- |" * len(kinds)]
    for mode in _modes(records):
        cells = []
        for kind in kinds:
            rows = [r for r in _of(records, mode, "single") if r["kind"] == kind]
            cells.append(f"{sum(r['correct'] for r in rows)}/{len(rows)}")
        lines.append(f"| {LABELS.get(mode, mode)} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def behaviour_table(records: list[dict]) -> str:
    lines = [
        "| Approach | Follow-up questions | Asked when ambiguous | Warehouse unchanged after "
        "write requests | Logged what it could not answer |",
        "| --- | --- | --- | --- | --- |",
    ]
    for mode in _modes(records):
        cells = []
        for category in ("followup", "ambiguous", "unsafe", "gap"):
            rows = [r for r in _of(records, mode, category) if r.get("correct") is not None]
            cells.append(f"{sum(r['correct'] for r in rows)}/{len(rows)}" if rows else "not scored")
        lines.append(f"| {LABELS.get(mode, mode)} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def cost_table(records: list[dict]) -> str:
    lines = [
        "| Approach | Model calls per question | Tokens per question | Cost per question | "
        "Latency p50 | Tool errors per question |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for mode in _modes(records):
        rows = _of(records, mode, "single")
        tokens = statistics.mean(r["input_tokens"] + r["output_tokens"] for r in rows)
        lines.append(
            f"| {LABELS.get(mode, mode)} | {statistics.mean(r['steps'] for r in rows):.1f} | "
            f"{tokens:,.0f} | ${statistics.mean(r['cost_usd'] for r in rows):.5f} | "
            f"{statistics.median(r['latency_s'] for r in rows):.1f} s | "
            f"{statistics.mean(r['tool_errors'] for r in rows):.2f} |"
        )
    return "\n".join(lines)


def paired_table(records: list[dict], baseline: str = "semantic") -> str:
    by_mode = {mode: {r["id"]: r for r in _of(records, mode, "single")} for mode in _modes(records)}
    if baseline not in by_mode or len(by_mode) < 2:
        return ""
    lines = [
        f"| {LABELS[baseline]} against | It was right, the other wrong | It was wrong, the other "
        "right | p |",
        "| --- | --- | --- | --- |",
    ]
    base = by_mode[baseline]
    for mode, rows in by_mode.items():
        if mode == baseline:
            continue
        shared = [q for q in base if q in rows]
        wins = sum(base[q]["correct"] and not rows[q]["correct"] for q in shared)
        losses = sum(rows[q]["correct"] and not base[q]["correct"] for q in shared)
        lines.append(
            f"| {LABELS.get(mode, mode)} | {wins} | {losses} | {mcnemar_exact(wins, losses):.3f} |"
        )
    return "\n".join(lines)


def stability_table(first: list[dict], second: list[dict]) -> str:
    """The same questions asked in two separate runs: how much do the verdicts move?"""
    lines = [
        "| Approach | Correct in run 1 | Correct in run 2 | Questions whose verdict changed |",
        "| --- | --- | --- | --- |",
    ]
    for mode in _modes(first):
        one = {r["id"]: r["correct"] for r in _of(first, mode, "single")}
        two = {r["id"]: r["correct"] for r in _of(second, mode, "single")}
        shared = [q for q in one if q in two]
        flipped = sum(one[q] != two[q] for q in shared)
        lines.append(
            f"| {LABELS.get(mode, mode)} | {sum(one[q] for q in shared)}/{len(shared)} | "
            f"{sum(two[q] for q in shared)}/{len(shared)} | {flipped} |"
        )
    return "\n".join(lines)


def render(records: list[dict], repeat: list[dict] | None = None) -> str:
    singles = len({r["id"] for r in records if r["category"] == "single"})
    parts = [
        f"Accuracy on {singles} single questions:",
        accuracy_table(records),
        "Correct answers by kind of question:",
        kind_table(records),
        "Behaviour on the other question types:",
        behaviour_table(records),
        "Cost and speed on the single questions:",
        cost_table(records),
    ]
    paired = paired_table(records)
    if paired:
        parts += ["Paired comparison (exact McNemar test on the same questions):", paired]
    if repeat:
        parts += [
            "Run-to-run stability (a second, separate run):",
            stability_table(records, repeat),
        ]
    return "\n\n".join(parts)
