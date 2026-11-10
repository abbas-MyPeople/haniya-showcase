"""The eval harness: the scorer agrees with the human labels, and the replay check catches change."""
import copy

from evals import rubric, run, scorer


def test_scorer_matches_every_human_label():
    calls = run.load_calls()
    assert len(calls) >= 5
    scores = [scorer.score(c) for c in calls]
    assert run.agreement(calls, scores) == []


def test_every_score_fills_the_whole_rubric():
    for c in run.load_calls():
        s = scorer.score(c)
        assert set(rubric.KEYS) <= set(s)


def test_replay_check_flags_a_regression():
    calls = run.load_calls()
    base = {c["id"]: scorer.score(c) for c in calls}
    # simulate a release where the agent stops texting the pay link on the clean call
    broken = copy.deepcopy(next(c for c in calls if c["id"] == "01_clean_order"))
    broken["turns"] = [t for t in broken["turns"] if t.get("tool") != "send_pay_link"]
    after = dict(base, **{"01_clean_order": scorer.score(broken)})
    assert ("01_clean_order", "pay_link_sent", "true", "false") in run.diff(base, after)
    assert run.diff(base, base) == []


def test_llm_judge_without_a_key_degrades_instead_of_crashing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = scorer.score(run.load_calls()[0], judge="llm")
    assert s["_error"]
    assert run.aggregate([s])["graded"] == 0


def test_tool_schema_requires_every_dimension():
    schema = rubric.tool_schema()
    assert schema["input_schema"]["required"] == rubric.KEYS
