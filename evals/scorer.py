"""Score one call transcript against the rubric.

Two graders share one output shape:

- `rule`  (default): deterministic checks on the transcript and the tool trace. Offline, free, and
  the same answer every run, so it can gate CI.
- `llm`   (opt-in): an LLM judge. The rubric goes to the Anthropic Messages API as a forced tool, so
  the reply is a structured object, never prose to parse. Needs ANTHROPIC_API_KEY in the
  environment. Retries transient errors; any failure returns `rubric.empty_score` instead of raising.

Transcript shape (see evals/transcripts/*.json):
    {"id": "...", "returning_name": "Sam" | null,
     "turns": [{"agent": "...", "lang": "en"}, {"caller": "..."},
               {"tool": "place_order", "args": {...}, "result": {...}}]}
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

from . import rubric

FILLER = re.compile(r"\b(one moment|one sec(ond)?|just a sec(ond)?|let me check|hold on|bear with me)\b",
                    re.I)
NAME_GIVEN = re.compile(r"(?i:\b(my name is|name'?s|it'?s for|under the name|this is))\s+([A-Z][a-z]+)")
NAME_ASK = re.compile(r"\b(what('?s| is) (the|your) name|name for the order|who('?s| is) (it|the order) for)\b",
                      re.I)
LANG_ASK = re.compile(r"\b(urdu|hindi|spanish|english|espa[nñ]ol)\b.*\b(please|mein|bolo)\b|"
                      r"\b(speak|in|switch to)\s+(urdu|hindi|spanish|english)\b", re.I)
LANG_CODE = {"urdu": "ur", "hindi": "hi", "spanish": "es", "espanol": "es", "español": "es",
             "english": "en"}
UNSURE = re.compile(r"\b(i('?m| am) not sure|i don'?t know|i can'?t help with that|no idea)\b", re.I)
_ADDON = r"(drink|lassi|naan|soup|dessert|appetizer|side)"
UPSELL = re.compile(rf"\b(add|would you like|want)\b.*\b{_ADDON}\b|\b{_ADDON}\b.*\badd\b", re.I)
# "How many would you like, and do you want sauce?" is two questions behind one question mark.
SECOND_Q = re.compile(r",\s*and\s+(do|did|would|what|how|which|can|could|is|are|should)\b", re.I)
HAPPY = re.compile(r"\b(thanks|thank you|perfect|great|awesome|shukriya|gracias)\b", re.I)
UNHAPPY = re.compile(r"\b(already told you|i said|forget it|never mind|ugh|frustrat|useless)\b", re.I)


def _agent(turns):
    return [t for t in turns if "agent" in t]


def _caller(turns):
    return [t for t in turns if "caller" in t]


def _tool(turns, name):
    return [t for t in turns if t.get("tool") == name]


def _placed(turns) -> bool:
    return any((t.get("result") or {}).get("ok") for t in _tool(turns, "place_order"))


def _requested_language(text: str) -> str | None:
    m = LANG_ASK.search(text)
    if not m:
        return None
    word = next(w for w in m.groups() if w and w.lower() in LANG_CODE)
    return LANG_CODE[word.lower()]


def questions_in(text: str) -> int:
    """How many questions one agent turn asks: question marks, plus a second question joined by
    ', and' inside a questioning sentence."""
    n = 0
    for sentence in re.findall(r"[^.?!]*\?", text):
        n += 1 + len(SECOND_Q.findall(sentence))
    return n


def score_rule(call: dict) -> dict:
    turns = call.get("turns") or []
    agent = _agent(turns)
    out: dict = {}

    out["filler_used"] = "true" if any(FILLER.search(t["agent"]) for t in agent) else "false"
    out["one_question_per_turn"] = "false" if any(questions_in(t["agent"]) > 1 for t in agent) else "true"

    # re-ask: the agent asks for a name after the caller already gave one
    name_known = bool(call.get("returning_name"))
    reasked = False
    for t in turns:
        if "caller" in t and NAME_GIVEN.search(t["caller"]):
            name_known = True
        elif "agent" in t and name_known and NAME_ASK.search(t["agent"]):
            reasked = True
    out["reasked_something_already_said"] = "true" if reasked else "false"

    saved = call.get("returning_name")
    if saved:
        out["greeted_returning_by_name"] = "true" if agent and saved in agent[0]["agent"] else "false"
    else:
        out["greeted_returning_by_name"] = "na"

    # language: after an explicit ask, every later agent turn must carry that language
    want, honored = None, True
    for t in turns:
        if "caller" in t:
            want = _requested_language(t["caller"]) or want
        elif "agent" in t and want and t.get("lang", "en") != want:
            honored = False
    out["language_honored"] = "na" if want is None else ("true" if honored else "false")

    formed = bool(_tool(turns, "add_to_cart"))
    placed = _placed(turns)
    out["offered_upsell"] = ("true" if any(UPSELL.search(t["agent"]) for t in agent) else "false") \
        if formed else "na"
    out["order_completed"] = ("true" if placed else "false") if formed else "na"
    out["pay_link_sent"] = ("true" if _tool(turns, "send_pay_link") else "false") if placed else "na"

    if placed:
        tail = " ".join(t["agent"] for t in agent[-2:]).lower()
        ready = re.search(r"\bready\b", tail)
        where = re.search(r"\b(pick (it )?up|pickup)\b", tail)
        close = re.search(r"\bclose\b|\bopen until\b", tail)
        out["gave_wrapup"] = "true" if (ready and where and close) else "false"
    else:
        out["gave_wrapup"] = "na"

    out["dead_ended"] = "true" if any(UNSURE.search(t["agent"]) for t in agent) else "false"

    said = " ".join(t["caller"] for t in _caller(turns))
    sentiment = 3
    if HAPPY.search(said):
        sentiment = 4 if out["dead_ended"] == "false" else 3
    if UNHAPPY.search(said):
        sentiment = 2
    if out["dead_ended"] == "true" and not HAPPY.search(said):
        sentiment = min(sentiment, 2)
    out["customer_sentiment"] = sentiment

    out["main_failure"] = _main_failure(out)
    return out


# The order a reviewer would fix them in: money path first, then the caller's experience.
_FAILURE_LABEL = [
    ("order_completed", "order never placed"),
    ("pay_link_sent", "no pay link"),
    ("dead_ended", "dead-ended a question"),
    ("reasked_something_already_said", "re-asked a given answer"),
    ("language_honored", "ignored language ask"),
    ("one_question_per_turn", "two questions at once"),
    ("filler_used", "filler around a tool"),
    ("gave_wrapup", "incomplete wrap-up"),
    ("greeted_returning_by_name", "missed returning name"),
    ("offered_upsell", "no add-on offered"),
]


def _main_failure(score: dict) -> str:
    for key, label in _FAILURE_LABEL:
        if rubric.is_bad(key, score.get(key)):
            return label
    return ""


# ---- optional LLM judge ---------------------------------------------------------------------------
API_URL = "https://api.anthropic.com/v1/messages"
SYSTEM = ("You are a QA reviewer for an AI phone host that takes pickup orders for a restaurant. "
          "Grade ONE call against the rubric by calling score_call exactly once. Reward what served "
          "the caller; flag only what hurt them. Speech-to-text spellings of correct audio are not "
          "agent errors.")


def render(call: dict) -> str:
    lines = [f"CALL {call.get('id', '?')}"]
    if call.get("returning_name"):
        lines.append(f"RETURNING CALLER, saved name: {call['returning_name']}")
    for t in call.get("turns") or []:
        if "agent" in t:
            lines.append(f"AGENT[{t.get('lang', 'en')}]: {t['agent']}")
        elif "caller" in t:
            lines.append(f"CALLER: {t['caller']}")
        elif "tool" in t:
            lines.append(f"  [tool] {t['tool']}({json.dumps(t.get('args', {}))[:200]}) -> "
                         f"{json.dumps(t.get('result', {}))[:200]}")
    return "\n".join(lines)[:9000]


def _coerce(raw: dict) -> dict:
    out = {}
    for d in rubric.DIMENSIONS:
        v = raw.get(d["key"])
        if d["type"] == "int":
            try:
                out[d["key"]] = max(0, min(5, int(v)))
            except (TypeError, ValueError):
                out[d["key"]] = 0
        elif d["type"] == "str":
            out[d["key"]] = str(v)[:80] if v is not None else ""
        else:
            s = str(v).strip().lower() if v is not None else "na"
            out[d["key"]] = s if s in ("true", "false", "na") else "na"
    return out


def score_llm(call: dict, retries: int = 3) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        return rubric.empty_score("no ANTHROPIC_API_KEY set")
    body = json.dumps({
        "model": os.environ.get("EVAL_MODEL", "claude-sonnet-4-5"),
        "max_tokens": 700,
        "system": SYSTEM,
        "tools": [rubric.tool_schema()],
        "tool_choice": {"type": "tool", "name": "score_call"},
        "messages": [{"role": "user", "content": "Grade this call.\n\n" + render(call)}],
    }).encode()
    last = "grade failed"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(API_URL, data=body, headers={
                "x-api-key": key, "anthropic-version": "2023-06-01",
                "content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                resp = json.loads(r.read().decode())
            for block in resp.get("content", []):
                if block.get("type") == "tool_use" and block.get("name") == "score_call":
                    return _coerce(block.get("input") or {})
            last = "model did not call score_call"
        except urllib.error.HTTPError as e:
            last = f"http {e.code}"
            if e.code not in (429, 500, 502, 503, 529):
                break
        except Exception as e:  # noqa: BLE001 - one call never takes down the batch
            last = str(e)[:120]
        time.sleep(1.5 * (attempt + 1))
    return rubric.empty_score(last)


def score(call: dict, judge: str = "rule") -> dict:
    s = score_llm(call) if judge == "llm" else score_rule(call)
    s["id"] = call.get("id", "?")
    return s
