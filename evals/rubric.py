"""The rubric: the dimensions every call is scored on.

This file is the contract between the scorer and the scorecard. The rule-based scorer fills these
fields from the transcript; the optional LLM judge receives `tool_schema()` as a forced tool, so the
model has to return exactly these fields as structured output (no free-form JSON parsing).

Each dimension is one requirement for the agent, written as something a reviewer can check by
reading the call. "na" means the requirement did not apply to this call (no order was placed, so
there was nothing to wrap up). Aggregates exclude "na" so they are not diluted.

Stdlib only. No side effects on import.
"""
from __future__ import annotations

BOOL = ["true", "false"]
BOOL_NA = ["true", "false", "na"]

# key, type, which value is bad, and the plain description a human or a model grades against.
DIMENSIONS = [
    {"key": "filler_used", "type": "bool", "bad_when": "true",
     "desc": "The agent said filler such as 'one moment' or 'let me check' around a tool call. "
             "Tools are fast; filler on a phone line sounds like stalling."},
    {"key": "one_question_per_turn", "type": "bool", "bad_when": "false",
     "desc": "Every agent turn asked at most one question. Two questions in one breath on a phone "
             "line means the caller answers one and the other is lost."},
    {"key": "reasked_something_already_said", "type": "bool", "bad_when": "true",
     "desc": "The agent asked for something the caller had already given (a name, a choice)."},
    {"key": "greeted_returning_by_name", "type": "bool_na", "bad_when": "false",
     "desc": "A returning caller was greeted by their saved name. 'na' for a first-time caller."},
    {"key": "language_honored", "type": "bool_na", "bad_when": "false",
     "desc": "When the caller explicitly asked for another language, every later agent turn used it. "
             "'na' if no language was asked for."},
    {"key": "offered_upsell", "type": "bool_na", "bad_when": "false",
     "desc": "One add-on was offered before the order was read back. 'na' if no order was formed."},
    {"key": "order_completed", "type": "bool_na", "bad_when": "false",
     "desc": "The order reached the POS (place_order succeeded). 'na' for info-only calls."},
    {"key": "pay_link_sent", "type": "bool_na", "bad_when": "false",
     "desc": "After placing, the pay link was texted to the caller. 'na' if no order was placed."},
    {"key": "gave_wrapup", "type": "bool_na", "bad_when": "false",
     "desc": "After placing, the agent stated the ready time, where to pick up and when we close "
             "today. 'na' if no order was placed."},
    {"key": "dead_ended", "type": "bool", "bad_when": "true",
     "desc": "The caller asked something answerable and left without an answer."},
    {"key": "customer_sentiment", "type": "int", "min": 1, "max": 5,
     "desc": "How the caller seemed by the end: 1 = frustrated, 3 = neutral, 5 = pleased."},
    {"key": "main_failure", "type": "str",
     "desc": "The single most important thing that went wrong, in six words or fewer. Empty if clean."},
]

KEYS = [d["key"] for d in DIMENSIONS]
BY_KEY = {d["key"]: d for d in DIMENSIONS}
# The dimensions a release cannot regress on. A replay that flips one of these is a failed check.
GATING = ["filler_used", "one_question_per_turn", "reasked_something_already_said",
          "order_completed", "pay_link_sent", "dead_ended"]


def _prop(d: dict) -> dict:
    if d["type"] == "bool":
        return {"type": "string", "enum": BOOL, "description": d["desc"]}
    if d["type"] == "bool_na":
        return {"type": "string", "enum": BOOL_NA, "description": d["desc"]}
    if d["type"] == "int":
        return {"type": "integer", "minimum": d["min"], "maximum": d["max"], "description": d["desc"]}
    return {"type": "string", "description": d["desc"]}


def tool_schema() -> dict:
    """The tool the LLM judge is forced to call, so its answer is always structured."""
    return {
        "name": "score_call",
        "description": "Score one restaurant phone call against the rubric. Call exactly once.",
        "input_schema": {
            "type": "object",
            "properties": {d["key"]: _prop(d) for d in DIMENSIONS},
            "required": KEYS,
        },
    }


def is_bad(key: str, value) -> bool:
    d = BY_KEY[key]
    if d["type"] in ("bool", "bool_na"):
        return value == d["bad_when"]
    if d["type"] == "int":
        return isinstance(value, int) and 0 < value <= 2
    return bool(value)


def empty_score(reason: str) -> dict:
    """A valid all-na score for a call that could not be graded. Marked with `_error` and excluded
    from aggregates, so one bad call never crashes or skews the batch."""
    out = {}
    for d in DIMENSIONS:
        out[d["key"]] = 0 if d["type"] == "int" else ("" if d["type"] == "str" else "na")
    out["_error"] = reason
    return out
