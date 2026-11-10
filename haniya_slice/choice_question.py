"""Every question about a choice is built from the choice data, never composed by the model.

WHY THIS EXISTS. When a dish has a required choice (which rice, which side, which sauce), the
model used to receive the option names and write the question itself. A sentence the model writes
about options and upcharges is a sentence nobody checked: it can drop options, call a paid option
included, or read fifteen names in one breath over a phone line.

So the server builds the sentence and hands it over. What the caller hears is a property of the
data, not of the model's diligence:

  - options in the order people actually choose them (counts from past orders), included first;
  - at most FEW named when the list is long, with "or I can list the rest";
  - every option that costs extra says how much extra, in words ("two dollars more"), and only
    those; the rest are called "included" only when some are not;
  - one question per turn: the sentence asks exactly one thing.

`popular_set` turns a multi-choice combo into ONE question ("Most people go with X and Y - want
that?") instead of one question per group, and only when every group has real pick counts,
because "most people" is a claim and it has to be true.

Pure functions, stdlib only. Pick counts are passed in (production reads them from the order
archive); here they are an argument so the module has no I/O.
"""
from __future__ import annotations

import re

FEW = 4  # options named before "or I can list the rest"

_NUM = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight",
        9: "nine", 10: "ten", 12: "twelve", 15: "fifteen", 20: "twenty"}


def label(group: str) -> str:
    """POS group name -> what a person says. 'a Soup Large' -> 'soup'; 'Carb 2' -> 'second carb'."""
    g = re.sub(r"\([^)]*\)", " ", str(group or ""))
    g = re.sub(r"^\s*an?\s+", "", g, flags=re.I)
    g = re.sub(r"\b(large|individual|half|deal)\b", " ", g, flags=re.I)
    g = " ".join(g.split()).lower()
    m = re.match(r"^(.*?)\s*(?:#\s*)?([12])$", g)
    if m:
        g = {"1": "first ", "2": "second "}[m.group(2)] + m.group(1).strip()
    return g or "choice"


def say_name(name: str) -> str:
    """The POS spelling -> the spoken one: drop parentheticals, '&' becomes 'and'."""
    return re.sub(r"\s*\([^)]*\)", "", str(name or "")).strip().replace(" & ", " and ")


def money_words(dollars: float) -> str:
    """2 -> 'two dollars'. Amounts without a clean word stay as digits."""
    d = round(float(dollars or 0), 2)
    if abs(d - round(d)) < 0.005 and int(round(d)) in _NUM:
        n = int(round(d))
        return f"{_NUM[n]} dollar{'s' if n != 1 else ''}"
    return f"${d:.2f}"


def _join(words: list, last: str = "or") -> str:
    words = [w for w in words if w]
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + f" {last} " + words[-1]


def ordered(options: list, counts: dict | None = None) -> list:
    """Included first, then extra-cost; inside each, most chosen first, menu order breaking ties."""
    counts = counts or {}
    idx = {o["name"]: i for i, o in enumerate(options)}
    return sorted(options, key=lambda o: (float(o.get("price") or 0) > 0,
                                          -int(counts.get(o["name"], 0)), idx[o["name"]]))


def question(group: str, options: list, counts: dict | None = None, few: int = FEW) -> dict:
    """{say, named, more, order}: the one sentence to ask, built only from `options`.

    `options` are dicts {name, price} where price is the extra cost in dollars (0 = included).
    `named` is what the sentence names; `more` is whether some options were left unnamed."""
    opts = [o if isinstance(o, dict) else {"name": str(o), "price": 0} for o in options or []]
    if not opts:
        return {"say": "", "named": [], "more": False, "order": []}
    order = ordered(opts, counts)
    inc = [o for o in order if float(o.get("price") or 0) <= 0]
    dear = [o for o in order if float(o.get("price") or 0) > 0]
    lab = label(group)
    if len(order) <= few + 1:
        named_inc, named_dear = inc, dear
    else:
        n_dear = 1 if dear else 0
        named_inc = inc[:max(1, few - n_dear)]
        named_dear = dear[:n_dear] if inc else dear[:few]
    more = len(named_inc) + len(named_dear) < len(order)

    parts = []
    if named_inc:
        parts.append(_join([say_name(o["name"]) for o in named_inc]) + (" - included" if dear else ""))
    by_price: dict = {}
    for o in named_dear:
        by_price.setdefault(round(float(o["price"]), 2), []).append(say_name(o["name"]))
    for p, names in by_price.items():
        parts.append(f"{_join(names)} for {money_words(p)} more")

    if not more and not dear and len(order) <= 3:
        say = f"Which {lab} - {_join([say_name(o['name']) for o in order])}?"
    else:
        say = f"For the {lab}: " + "; or ".join(parts) + "."
        say += " Want one of those, or shall I list the rest?" if more else " Which would you like?"
    return {"say": say, "named": [o["name"] for o in named_inc + named_dear], "more": more,
            "order": [o["name"] for o in order]}


def popular_set(groups: list, counts_by_group: dict) -> dict | None:
    """The set most people order, one pick per group, as ONE question. None if we cannot say so.

    `groups` are dicts {group, group_id, options}; `counts_by_group` maps group_id -> {name: count}.
    Prefers the most chosen INCLUDED option, so "most people" never quietly upsells. Paired groups
    (first side / second side with the same list) get the second favourite, not the first twice."""
    pick = {}
    for g in groups:
        opts = g.get("options") or []
        c = counts_by_group.get(str(g.get("group_id") or "")) or {}
        live = [o for o in opts if c.get(o["name"])]
        if not live:
            return None
        inc = [o for o in live if float(o.get("price") or 0) <= 0] or live
        taken = {p["name"] for p in pick.values()}
        fresh = [o for o in inc if o["name"] not in taken] or inc
        pick[g["group"]] = max(fresh, key=lambda o: c.get(o["name"], 0))
    if not pick:
        return None
    words = []
    for o in pick.values():
        w = say_name(o["name"])
        if float(o.get("price") or 0) > 0:
            w += f" ({money_words(o['price'])} more)"
        words.append(w)
    extra = sum(float(o.get("price") or 0) for o in pick.values())
    say = "Most people go with " + _join(words, "and") + " - want that, or would you like to change anything?"
    return {"set": {g: o["name"] for g, o in pick.items()}, "say": say, "extra": round(extra, 2)}
