"""
Queue ordering from the reviewer's own keyword history.

The contract every test here defends is that this is an ORDERING, never a filter:
`rank` returns exactly what it was given, reordered. That bound is why it may
ship on a signal measured at AUC 0.602 with a CI spanning 0.5, when the AI screen
at 0.432 could not ship even as a gate (YIELD_OPTIMIZATION_PLAN.md 14.16).
"""
import ranking


def _judged(pairs):
    """[(keyword, label), ...] -> rows shaped like Airtable's."""
    return [{"source": f"YouTube search ({k})", "label": l} for k, l in pairs]


# --- parsing the Source field that already exists on ~280 live rows ---

def test_keywords_come_off_the_parenthesised_tail():
    assert ranking.source_keywords("YouTube search (man cave tour)") == ["man cave tour"]
    assert ranking.source_keywords("x (a, b,  c )") == ["a", "b", "c"]


def test_a_source_with_no_keyword_yields_none():
    """The paid vendor path writes a constant string with no parentheses."""
    for src in ("influencers.club discovery", "", None, "no parens here"):
        assert ranking.source_keywords(src) == []


def test_only_the_trailing_parenthesis_group_is_read():
    """A channel name containing parentheses must not be mistaken for keywords."""
    assert ranking.source_keywords("Search (US) (man cave tour)") == ["man cave tour"]


# --- learning rates ---

def test_a_rate_is_approved_over_total():
    rates = ranking.approval_rates(_judged(
        [("kw", "Approved")] * 3 + [("kw", "Rejected")] * 1))
    assert rates["kw"]["rate"] == 0.75
    assert rates["kw"]["approved"] == 3 and rates["kw"]["rejected"] == 1


def test_a_keyword_with_too_little_history_gets_no_rate():
    """
    One verdict must not set a 0% or 100% rate that then orders every future
    candidate on a single reviewer decision.
    """
    rates = ranking.approval_rates(_judged([("thin", "Approved")]))
    assert rates["thin"]["rate"] is None
    assert rates["thin"]["approved"] == 1, "the count is still reported"


def test_unjudged_rows_are_ignored_entirely():
    """A pending row is absence of evidence, not evidence."""
    rows = _judged([("kw", "Approved")] * 3)
    rows += [{"source": "x (kw)", "label": "New"},
             {"source": "x (kw)", "label": None}]
    assert ranking.approval_rates(rows)["kw"]["rate"] == 1.0


def test_a_row_matched_by_two_keywords_counts_for_both():
    rates = ranking.approval_rates(
        [{"source": "x (a, b)", "label": "Approved"}] * 3)
    assert rates["a"]["approved"] == 3 and rates["b"]["approved"] == 3


# --- scoring one candidate ---

def test_the_best_keyword_wins_not_the_average():
    """
    A candidate found by a strong and a weak keyword is one the strong keyword
    found; averaging would punish it for having matched twice.
    """
    rates = ranking.approval_rates(
        _judged([("good", "Approved")] * 4 + [("bad", "Rejected")] * 4))
    score, why = ranking.priority_score("x (bad, good)", rates)
    assert score == 1.0
    assert "good" in why


def test_a_candidate_with_no_usable_keyword_gets_the_NEUTRAL_PRIOR():
    """
    Not zero. A zero would bury every candidate whose keyword is simply new,
    which is absent data disqualifying it.
    """
    score, why = ranking.priority_score("influencers.club discovery", {})
    assert score == ranking.NEUTRAL_PRIOR
    assert "no keyword" in why


def test_a_thin_keyword_is_named_in_the_explanation():
    rates = ranking.approval_rates(_judged([("thin", "Approved")]))
    score, why = ranking.priority_score("x (thin)", rates)
    assert score == ranking.NEUTRAL_PRIOR
    assert "thin" in why, "a reviewer must see WHY it could not be ranked"


def test_the_explanation_always_shows_the_record_behind_the_score():
    rates = ranking.approval_rates(
        _judged([("kw", "Approved")] * 2 + [("kw", "Rejected")] * 2))
    _, why = ranking.priority_score("x (kw)", rates)
    assert "50%" in why and "2/4" in why


# --- the ordering contract ---

def test_rank_returns_EVERY_candidate_and_drops_none():
    """The whole safety argument: this reorders, it never filters."""
    rates = ranking.approval_rates(_judged([("bad", "Rejected")] * 5))
    cands = [{"name": n, "source": f"x ({k})"} for n, k in
             (("A", "bad"), ("B", "unknown"), ("C", "bad"))]
    out = ranking.rank(cands, rates)
    assert len(out) == 3
    assert {c["name"] for c in out} == {"A", "B", "C"}


def test_high_rate_sorts_before_low_rate():
    rates = ranking.approval_rates(
        _judged([("good", "Approved")] * 4 + [("bad", "Rejected")] * 4))
    out = ranking.rank([{"source": "x (bad)"}, {"source": "x (good)"}], rates)
    assert out[0]["priority"] == 1.0 and out[1]["priority"] == 0.0


def test_ties_keep_arrival_order_so_the_output_is_stable():
    rates = ranking.approval_rates(_judged([("kw", "Approved")] * 3))
    cands = [{"name": str(i), "source": "x (kw)"} for i in range(6)]
    once = [c["name"] for c in ranking.rank(cands, rates)]
    assert once == [str(i) for i in range(6)]
    assert once == [c["name"] for c in ranking.rank(cands, rates)]


def test_the_original_fields_survive_ranking():
    out = ranking.rank([{"name": "A", "source": "x (k)", "subs": 1234}], {})
    assert out[0]["subs"] == 1234 and out[0]["name"] == "A"
    assert "priority" in out[0] and "priority_reason" in out[0]


def test_empty_input_is_empty_output():
    assert ranking.rank([], {}) == []
    assert ranking.rank(None, {}) == []
    assert ranking.approval_rates([]) == {}
    assert ranking.approval_rates(None) == {}


def test_no_internal_bookkeeping_leaks_into_the_result():
    out = ranking.rank([{"name": "A", "source": "x (k)"}], {})
    assert "_i" not in out[0]
