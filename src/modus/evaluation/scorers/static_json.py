"""Static-JSON deterministic scorer: structured-output comparison without an LLM.

The scorer takes a scenario's ``expected`` JSON and the trajectory's answer
(recovered from the run's ``final_result`` or its latest host_response /
terminal event), noise-parses it (markdown fences, ``Answer:`` prefixes,
balanced-bracket extraction), flattens both sides into key-path leaf maps, and
compares leaf values with numeric tolerance / interval / delta-1 matching.

The result is a pure dict (no I/O, no model calls) so it can be unit-tested in
isolation and is safe to run in the offline evaluator.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Relative-error bandwidths the design names explicitly (1% / 5% / 10%).
DEFAULT_RELATIVE_TOLERANCE = 0.05

# Field pairs that describe an inclusive range instead of two independent
# numbers, e.g. ``start_point``/``end_point``.  When a dict carries both keys of
# a pair, the flatten step emits a single ``{prefix}_range`` interval leaf.
_RANGE_FIELD_PAIRS: tuple[tuple[str, str], ...] = (
    ("start_point", "end_point"),
    ("start", "end"),
)

_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)
_ANSWER_PREFIX_RE = re.compile(r"(?im)^\s*(?:answer|答案|result|output|out)\s*[:：]\s*")


# ── noise parsing ────────────────────────────────────────────────────────


def extract_answer(text: str) -> str:
    """Extract the JSON-bearing answer text from a noisy model response.

    Applies, in order: markdown-fence extraction, ``Answer:``-style prefix
    stripping, and balanced-bracket extraction.  A non-empty inner candidate
    wins; otherwise the whole (trimmed) text is returned so a plain-text answer
    still reaches the comparator.
    """
    if not text:
        return ""
    raw = str(text)
    fence = _FENCE_RE.search(raw)
    if fence:
        inner = fence.group(1).strip()
        if inner:
            raw = inner
    raw = _ANSWER_PREFIX_RE.sub("", raw).strip()
    candidate = _extract_balanced_json(raw)
    return candidate if candidate and candidate.strip() else raw


def _extract_balanced_json(text: str) -> str:
    """Return the first brace/bracket-balanced JSON substring of ``text``.

    Respects string literals and backslash escapes while walking so a `}`
    inside a string never terminates the match.  Returns the unmodified text
    when no balanced object/array is found.
    """
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            ch = text[index]
            if escaped:
                escaped = False
                continue
            if ch == "\\" and in_string:
                escaped = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0:
                    return text[start:index + 1]
    return text


def parse_json_answer(text: str) -> Any:
    """Noise-parse ``text`` and return the parsed JSON value, or None.

    ``None`` is returned when no JSON is recoverable; the caller distinguishes
    "no structured answer" from "wrong answer" in its score.
    """
    candidate = extract_answer(text)
    if not candidate:
        return None
    try:
        value = json.loads(candidate)
        if isinstance(value, (dict, list)) or value is None:
            return value
        return value
    except json.JSONDecodeError:
        return None


# ── flattening ───────────────────────────────────────────────────────────


def flatten_answer(value: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a nested JSON value into ``key-path -> leaf`` pairs.

    Dict keys join with ``.``, list indices with ``[i]``.  A dict that is
    *exactly* a range field pair (``start_point``/``end_point``, ``start``/
    ``end``, or a ``*_start``/``*_end`` pair) collapses into one ``{base}_range``
    leaf holding the inclusive ``[lo, hi]`` interval; the comparator then uses
    interval semantics instead of two independent tolerance checks.  A dict
    that carries a range pair *plus* other fields flattens normally so no leaf
    is dropped.
    """
    flattened: dict[str, Any] = {}

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            range_pair = _matching_range_pair(node)
            if range_pair is not None and len(node) == 2:
                lo, hi = node[range_pair[0]], node[range_pair[1]]
                base = _range_base(path, range_pair[0])
                leaf = _range_leaf(path, base)
                if _is_number(lo) and _is_number(hi):
                    flattened[leaf] = [float(lo), float(hi)]
                    return
            for key, value in node.items():
                child = f"{path}.{key}" if path else str(key)
                walk(value, child)
        elif isinstance(node, list):
            if not node:
                flattened[path] = []
                return
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        else:
            flattened[path] = node

    walk(value, prefix)
    return flattened


def _range_base(path: str, range_key: str) -> str:
    """Derive the leaf base name for a range pair at a given path.

    ``start_point``/``end_point`` or ``start``/``end`` at the root yields
    ``range``; a ``foo_start``/``foo_end`` pair yields ``foo``.  Nested pairs
    keep the parent leaf name so both sides flatten identically.
    """
    if range_key in {"start_point", "start"}:
        base = "range"
    elif range_key.endswith("_start"):
        base = range_key[:-len("_start")]
    elif range_key.endswith("_point"):
        base = range_key[:-len("_point")]
    else:
        base = "range"
    if not path:
        return base
    return path.rpartition(".")[2] or base


def _range_leaf(path: str, base: str) -> str:
    """Full leaf key for a range: ``base_range`` when nested, ``range`` at root."""
    if not path:
        return base if base == "range" else f"{base}_range"
    return f"{path}.{base}_range"


def _matching_range_pair(node: dict[str, Any]) -> tuple[str, str] | None:
    """Return the range field pair present on ``node``, or None."""
    for pair in _RANGE_FIELD_PAIRS:
        if pair[0] in node and pair[1] in node:
            return pair
    suffix_keys = {
        key: key[:-len("_end")]
        for key in node if key.endswith("_end")
    }
    for base in suffix_keys.values():
        if f"{base}_start" in node:
            return (f"{base}_start", f"{base}_end")
    return None


# ── value comparison ─────────────────────────────────────────────────────


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _compare_value(expected: Any, predicted: Any, match: dict[str, Any]) -> tuple[bool, str]:
    """Compare one leaf pair.  Returns (matched, mode).

    Modes: ``exact`` (string/bool/None equality), ``numeric`` (relative-error
    bandwidth, default 5%), ``delta1`` (absolute difference <= 1), ``range``
    (predicted number inside an expected interval), ``interval`` (two intervals
    overlap).  A pair mismatch always returns False.
    """
    tolerance = float(match.get("tolerance", DEFAULT_RELATIVE_TOLERANCE))
    allow_delta1 = bool(match.get("delta1", False))

    # Interval / range matching: expected or predicted is a 2-number list.
    expected_range = _as_range(expected)
    predicted_range = _as_range(predicted)
    if expected_range is not None and _is_number(predicted):
        lo, hi = expected_range
        slack = tolerance * max(1.0, abs(hi - lo))
        if (lo - slack) <= float(predicted) <= (hi + slack):
            return True, "range"
        return False, "range"
    if expected_range is not None and predicted_range is not None:
        e_lo, e_hi = expected_range
        p_lo, p_hi = predicted_range
        slack = tolerance * max(1.0, abs(e_hi - e_lo))
        if p_hi < (e_lo - slack) or p_lo > (e_hi + slack):
            return False, "interval"
        return True, "interval"

    # Numeric matching with relative-error bandwidth and optional delta-1.
    if _is_number(expected) and _is_number(predicted):
        expected_f, predicted_f = float(expected), float(predicted)
        if allow_delta1 and abs(predicted_f - expected_f) <= 1.0:
            return True, "delta1"
        if abs(expected_f) <= 1e-12:
            matched = abs(predicted_f - expected_f) <= max(tolerance, 1e-9)
        else:
            matched = abs(predicted_f - expected_f) <= tolerance * abs(expected_f)
        return matched, "numeric"

    # Boolean, null, and string equality (lenient numeric-string coercion).
    if isinstance(expected, bool) or isinstance(predicted, bool):
        return expected is predicted, "exact"
    if expected is None or predicted is None:
        return expected is None and predicted is None, "exact"
    if isinstance(expected, str) and isinstance(predicted, str):
        if _is_numeric(expected) and _is_numeric(predicted):
            return _compare_value(float(expected), float(predicted), match)
        return expected.strip() == predicted.strip(), "exact"
    return expected == predicted, "exact"


def _is_numeric(value: Any) -> bool:
    """True when ``value`` is a real number or a numeric-looking string."""
    if _is_number(value):
        return True
    if not isinstance(value, str):
        return False
    try:
        float(value.strip())
        return True
    except (TypeError, ValueError):
        return False


def _as_range(value: Any) -> tuple[float, float] | None:
    """Coerce a 2-number list into an inclusive ``(lo, hi)`` interval, else None."""
    if (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and _is_number(value[0])
        and _is_number(value[1])
    ):
        lo, hi = float(value[0]), float(value[1])
        return (min(lo, hi), max(lo, hi))
    return None


def _leaf_basename(path: str) -> str:
    return path.rpartition(".")[2] if "." in path else path


def _expected_flat_keys(expected: Any) -> set[str]:
    """Flatten keys of an expected value for metric accounting when no answer."""
    return set(flatten_answer(expected))


# ── scoring ──────────────────────────────────────────────────────────────


def _compute_metrics(matched_keys: set[str], expected_keys: set[str],
                     predicted_keys: set[str]) -> dict[str, Any]:
    expected_total = len(expected_keys)
    predicted_total = len(predicted_keys)
    matched_total = len(matched_keys)
    precision = matched_total / predicted_total if predicted_total else 0.0
    recall = matched_total / expected_total if expected_total else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) else 0.0
    )
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "matched": matched_total,
        "expected_count": expected_total,
        "predicted_count": predicted_total,
    }


def evaluate_static_json(
    scenario: dict[str, Any],
    trajectory: dict[str, Any],
    **opts: Any,
) -> dict[str, Any]:
    """Score a scenario against a trajectory with the static-json comparator.

    ``scenario`` carries ``expected`` (the reference JSON) and optional
    ``match`` options (``tolerance``, ``delta1``).  ``trajectory`` provides the
    answer via its ``final_result`` field or the latest host_response /
    terminal event text.  Returns a pure score dict with
    ``pass``/``partial``/``strict``, precision/recall/F1, and the missing/extra
    key lists.
    """
    expected = scenario.get("expected")
    match = scenario.get("match") or {}
    if isinstance(match, str):
        try:
            match = json.loads(match)
        except json.JSONDecodeError:
            match = {}
    if not isinstance(match, dict):
        match = {}
    match = {**opts, **match}  # explicit call options take precedence

    answer_text = _trajectory_answer(trajectory)
    parsed = parse_json_answer(answer_text)

    missing: list[str] = []
    extra: list[str] = []
    diffs: list[dict[str, Any]] = []
    strict = False
    partial = False

    if parsed is None:
        reason = "no structured answer recovered from the trajectory"
        if isinstance(expected, dict):
            missing = list(flatten_answer(expected))
        elif isinstance(expected, list):
            missing = list(flatten_answer(expected))
        else:
            missing = [""]
        metrics = _compute_metrics(set(), set(_expected_flat_keys(expected)), set())
    elif isinstance(expected, dict):
        expected_flat = flatten_answer(expected)
        predicted_flat = flatten_answer(parsed)
        matched_keys: set[str] = set()
        predicted_pool = dict(predicted_flat)

        for key, expected_value in expected_flat.items():
            candidate_key = key if key in predicted_pool else _unique_basename_match(
                _leaf_basename(key), predicted_pool,
            )
            if candidate_key is None:
                missing.append(key)
                diffs.append({
                    "key": key, "expected": expected_value,
                    "predicted": None, "matched": False, "mode": "missing",
                })
                continue
            predicted_value = predicted_pool.pop(candidate_key)
            matched, mode = _compare_value(expected_value, predicted_value, match)
            diffs.append({
                "key": key,
                "expected": _jsonable(expected_value),
                "predicted": _jsonable(predicted_value),
                "matched": matched,
                "mode": mode,
            })
            if matched:
                matched_keys.add(key)
            else:
                missing.append(key)

        extra = [key for key in predicted_pool]
        strict = len(missing) == 0
        partial = len(matched_keys) > 0
        metrics = _compute_metrics(matched_keys, set(expected_flat), set(predicted_flat))
        reason = (
            "strict match"
            if strict
            else f"{len(missing)} expected key(s) missing or mismatched; "
                 f"{len(extra)} unexpected key(s)"
        )
    elif isinstance(expected, list):
        expected_flat = flatten_answer(expected)
        predicted_flat = flatten_answer(parsed)
        matched_keys = set(expected_flat) & set(predicted_flat)
        missing = [key for key in expected_flat if key not in matched_keys]
        extra = [key for key in predicted_flat if key not in matched_keys]
        strict = not missing
        partial = bool(matched_keys)
        metrics = _compute_metrics(set(matched_keys), set(expected_flat), set(predicted_flat))
        reason = "strict match" if strict else f"{len(missing)} expected item(s) missing"
    else:
        # Scalar expected: compare the parsed value directly.
        matched, mode = _compare_value(expected, parsed, match)
        strict = partial = matched
        metrics = _compute_metrics(
            {""} if matched else set(), {""}, {""},
        )
        missing = [] if matched else [""]
        extra = [] if matched else ([""] if parsed is not None else [])
        diffs = [{
            "key": "answer", "expected": _jsonable(expected),
            "predicted": _jsonable(parsed), "matched": matched, "mode": mode,
        }]
        reason = "strict match" if matched else "expected and predicted answers differ"

    return {
        "pass": strict,
        "partial": partial,
        "strict": strict,
        "precision": metrics["precision"],
        "recall": metrics["recall"],
        "f1": metrics["f1"],
        "matched": metrics["matched"],
        "expected_count": metrics["expected_count"],
        "predicted_count": metrics["predicted_count"],
        "missing_keys": missing,
        "extra_keys": extra,
        "diffs": diffs[:40],
        "reason": reason,
        "answer": answer_text[:2000],
        "parsed": parsed,
    }


def _unique_basename_match(basename: str, pool: dict[str, Any]) -> str | None:
    """Match ``basename`` to the single pool key sharing it, if unambiguous."""
    candidates = [key for key in pool if _leaf_basename(key) == basename]
    return candidates[0] if len(candidates) == 1 else None


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def _trajectory_answer(trajectory: dict[str, Any]) -> str:
    """Recover the answer text from a trajectory document.

    Priority: ``final_result`` (the run's durable outcome) → the last
    host_response payload text → the terminal event message.  An empty result
    makes the scorer report "no structured answer".
    """
    final_result = str(trajectory.get("final_result") or "").strip()
    if final_result:
        return final_result
    events = trajectory.get("events") or []
    if not isinstance(events, list):
        return ""
    for event in reversed(events):
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if event_type == "host_response":
            text = str(payload.get("text") or payload.get("markdown") or "").strip()
            if text:
                return text
        if event_type in {"run_completed", "run_error"}:
            message = str(payload.get("message") or "").strip()
            if message:
                return message
    return ""


# Round/floor helpers kept for callers that need explicit bandwidth selection.
def relative_error(expected: float, predicted: float) -> float:
    """Absolute relative error between two numbers (0 for both-zero)."""
    e, p = float(expected), float(predicted)
    if abs(e) <= 1e-12:
        return abs(p) if abs(p) > 0 else 0.0
    return abs(p - e) / abs(e)
