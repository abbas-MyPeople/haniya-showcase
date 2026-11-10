"""Replay runner: score every transcript, print a scorecard, diff against the last baseline.

    python3 -m evals.run                    # score all transcripts, print the scorecard
    python3 -m evals.run --baseline         # record evals/baseline.json
    python3 -m evals.run --check            # diff against baseline; exit 1 if a gating field changed
    python3 -m evals.run --judge llm        # grade with the LLM judge (needs ANTHROPIC_API_KEY)

In production the same loop runs over real calls before every release: a change to the prompt,
a tool or the resolver is replayed against calls that already happened, and any call whose
verdict moves on a gating dimension blocks the release until someone has looked at it.

Each transcript may carry `expected`: the labels a human gave it. `--agreement` compares the
scorer against those labels, which is how the scorer itself is tested.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

from . import rubric, scorer

HERE = os.path.dirname(os.path.abspath(__file__))
TRANSCRIPTS = os.path.join(HERE, "transcripts")
BASELINE = os.path.join(HERE, "baseline.json")


def load_calls(folder: str = TRANSCRIPTS) -> list[dict]:
    calls = []
    for path in sorted(glob.glob(os.path.join(folder, "*.json"))):
        with open(path, encoding="utf-8") as f:
            calls.append(json.load(f))
    return calls


def aggregate(scores: list[dict]) -> dict:
    """Per dimension: how many calls it applied to and how many went the bad way."""
    graded = [s for s in scores if not s.get("_error")]
    out = {"calls": len(scores), "graded": len(graded), "errors": len(scores) - len(graded)}
    for d in rubric.DIMENSIONS:
        k = d["key"]
        if d["type"] in ("bool", "bool_na"):
            applies = [s for s in graded if s.get(k) in ("true", "false")]
            out[k] = {"applied": len(applies), "bad": sum(rubric.is_bad(k, s[k]) for s in applies)}
        elif d["type"] == "int":
            vals = [s[k] for s in graded if isinstance(s.get(k), int) and s[k] > 0]
            out[k] = {"avg": round(sum(vals) / len(vals), 2) if vals else None}
    fails: dict = {}
    for s in graded:
        if s.get("main_failure"):
            fails[s["main_failure"]] = fails.get(s["main_failure"], 0) + 1
    out["top_failures"] = sorted(fails.items(), key=lambda kv: -kv[1])
    return out


def diff(old: dict, new: dict) -> list[tuple[str, str, object, object]]:
    """(call id, field, before, after) for every gating field that changed."""
    changes = []
    for cid, s in new.items():
        before = old.get(cid)
        if before is None:
            changes.append((cid, "(new call)", None, None))
            continue
        for k in rubric.GATING:
            if before.get(k) != s.get(k):
                changes.append((cid, k, before.get(k), s.get(k)))
    for cid in old:
        if cid not in new:
            changes.append((cid, "(call removed)", None, None))
    return changes


def agreement(calls: list[dict], scores: list[dict]) -> list[tuple[str, str, object, object]]:
    """(call id, field, expected, got) wherever the scorer disagrees with the human label."""
    out = []
    for c, s in zip(calls, scores):
        for k, want in (c.get("expected") or {}).items():
            if s.get(k) != want:
                out.append((c["id"], k, want, s.get(k)))
    return out


def _print_scorecard(scores: list[dict], agg: dict) -> None:
    cols = ["filler_used", "one_question_per_turn", "reasked_something_already_said",
            "language_honored", "order_completed", "pay_link_sent", "gave_wrapup", "dead_ended"]
    short = ["filler", "1q", "reask", "lang", "placed", "paylink", "wrapup", "deadend"]
    print(f"{'call':<26}" + "".join(f"{h:>9}" for h in short) + "  sent  main failure")
    for s in scores:
        row = f"{s['id']:<26}" + "".join(f"{str(s.get(k, '')):>9}" for k in cols)
        print(row + f"  {s.get('customer_sentiment', 0):>4}  {s.get('main_failure', '') or '-'}")
    print()
    print(f"graded {agg['graded']} of {agg['calls']} calls ({agg['errors']} could not be graded)")
    for k in cols:
        a = agg[k]
        print(f"  {k:<34} bad on {a['bad']} of {a['applied']} calls it applied to")
    print(f"  {'customer_sentiment avg':<34} {agg['customer_sentiment']['avg']}")
    if agg["top_failures"]:
        print("  top failures: " + ", ".join(f"{f} ({n})" for f, n in agg["top_failures"]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m evals.run")
    ap.add_argument("--judge", choices=["rule", "llm"], default="rule")
    ap.add_argument("--baseline", action="store_true", help="write evals/baseline.json")
    ap.add_argument("--check", action="store_true", help="diff against baseline, exit 1 on change")
    ap.add_argument("--agreement", action="store_true", help="compare against human labels")
    ap.add_argument("--transcripts", default=TRANSCRIPTS)
    args = ap.parse_args(argv)

    calls = load_calls(args.transcripts)
    if not calls:
        print("no transcripts found")
        return 1
    scores = [scorer.score(c, args.judge) for c in calls]
    agg = aggregate(scores)
    _print_scorecard(scores, agg)
    by_id = {s["id"]: s for s in scores}
    status = 0

    if args.agreement:
        mism = agreement(calls, scores)
        print(f"\nscorer vs human labels: {len(mism)} disagreement(s)")
        for cid, k, want, got in mism:
            print(f"  {cid}: {k} expected {want}, got {got}")
        status = status or (1 if mism else 0)

    if args.baseline:
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump(by_id, f, indent=2, sort_keys=True)
        print(f"\nbaseline written: {len(by_id)} calls")

    if args.check:
        if not os.path.exists(BASELINE):
            print("\nno baseline yet; run with --baseline first")
            return 1
        with open(BASELINE, encoding="utf-8") as f:
            old = json.load(f)
        changes = diff(old, by_id)
        print(f"\nreplay vs baseline: {len(changes)} gating change(s)")
        for cid, k, a, b in changes:
            print(f"  {cid}: {k} {a} -> {b}")
        status = status or (1 if changes else 0)

    return status


if __name__ == "__main__":
    sys.exit(main())
