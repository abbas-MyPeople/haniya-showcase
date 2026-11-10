"""Tests for the choice-question slice. Option names and upcharges are fictional."""
from evals.scorer import questions_in
from haniya_slice.choice_question import label, money_words, popular_set, question

RICE = [
    {"name": "Steamed Rice", "price": 0},
    {"name": "Brown Rice", "price": 0},
    {"name": "Garlic Rice", "price": 2},
]
SIDES = [{"name": f"Side {c}", "price": 0} for c in "ABCDEFG"] + [{"name": "Side Deluxe", "price": 3}]


def test_short_list_names_everything_and_prices_the_extra():
    q = question("Rice", RICE)
    assert q["say"] == ("For the rice: Steamed Rice or Brown Rice - included; "
                        "or Garlic Rice for two dollars more. Which would you like?")
    assert q["more"] is False


def test_all_included_short_list_is_one_plain_question():
    q = question("Sauce", [{"name": "Mild", "price": 0}, {"name": "Spicy", "price": 0}])
    assert q["say"] == "Which sauce - Mild or Spicy?"
    assert "included" not in q["say"]


def test_long_list_is_capped_and_offers_the_rest():
    q = question("Side 1", SIDES, counts={"Side E": 50, "Side B": 20})
    assert len(q["named"]) <= 4
    assert q["more"] is True
    assert q["say"].endswith("Want one of those, or shall I list the rest?")
    # most chosen first, and the one extra-cost option still says its price
    assert q["named"][0] == "Side E"
    assert "three dollars more" in q["say"]


def test_every_built_sentence_asks_exactly_one_question():
    for group, opts in [("Rice", RICE), ("Side", SIDES), ("Sauce", RICE[:2])]:
        assert questions_in(question(group, opts)["say"]) == 1


def test_labels_and_money_words():
    assert label("Carb 2") == "second carb"
    assert label("a Soup Large") == "soup"
    assert money_words(1) == "one dollar"
    assert money_words(2.5) == "$2.50"


def test_popular_set_is_one_question_and_never_upsells():
    groups = [
        {"group": "Side 1", "group_id": "g1", "options": SIDES},
        {"group": "Side 2", "group_id": "g2", "options": SIDES},
    ]
    counts = {"g1": {"Side A": 40, "Side Deluxe": 90}, "g2": {"Side A": 30, "Side C": 10}}
    s = popular_set(groups, counts)
    assert s["set"] == {"Side 1": "Side A", "Side 2": "Side C"}  # included beats a dearer favourite
    assert s["extra"] == 0
    assert questions_in(s["say"]) == 1


def test_popular_set_refuses_without_real_counts():
    groups = [{"group": "Side 1", "group_id": "g1", "options": SIDES}]
    assert popular_set(groups, {}) is None
