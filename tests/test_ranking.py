"""
Queue ordering from the reviewer's own keyword history.

The contract every test here defends is that this is an ORDERING, never a filter:
`rank` returns exactly what it was given, reordered. That bound is why it may
ship on a signal measured at AUC 0.602 with a CI spanning 0.5, when the AI screen
at 0.432 could not ship even as a gate (YIELD_OPTIMIZATION_PLAN.md 14.16).
"""
from channel_vetting.ranking import keyword_history


def _judged(pairs):
    """[(keyword, label), ...] -> rows shaped like Airtable's."""
    return [{"source": f"YouTube search ({k})", "label": l} for k, l in pairs]


# --- parsing the Source field that already exists on ~280 live rows ---

def test_keywords_come_off_the_parenthesised_tail():
    assert keyword_history.source_keywords("YouTube search (man cave tour)") == ["man cave tour"]
    assert keyword_history.source_keywords("x (a, b,  c )") == ["a", "b", "c"]


def test_a_source_with_no_keyword_yields_none():
    """The paid vendor path writes a constant string with no parentheses."""
    for src in ("influencers.club discovery", "", None, "no parens here"):
        assert keyword_history.source_keywords(src) == []


def test_only_the_trailing_parenthesis_group_is_read():
    """Sibling groups: the LAST balanced one is the provenance."""
    assert keyword_history.source_keywords("Search (US) (man cave tour)") == ["man cave tour"]


def test_the_NESTED_vendor_label_is_read_and_its_niche_stripped():
    """
    REGRESSION, and it silently disabled this signal for 176 rows.

    pipeline.run_niche passes source_label=f"influencers.club discovery ({niche})",
    so the paid path's rows end in "))". The original tail pattern here required
    a group containing no ")", so it matched nothing at all and returned [] for
    the ENTIRE paid discovery path — 64 Home Theater rows and 112 Lifestyle rows.
    That is what produced "0 of 31 pending rows are rankable" and was misread as
    the pipeline not recording a keyword. It records it; this function dropped it.

    The niche qualifier is stripped so the vendor path is ONE bucket: the niche is
    already implied by the table, and splitting would halve every cell count.
    """
    for niche in ("Home Theater", "Lifestyle Sofa"):
        src = f"YouTube Discovery Pipeline (influencers.club discovery ({niche}))"
        assert keyword_history.source_keywords(src) == ["influencers.club discovery"], src


def test_an_unbalanced_source_is_no_provenance_rather_than_a_guess():
    assert keyword_history.source_keywords("YouTube Discovery Pipeline (oops") == []


def test_the_vendor_path_gets_a_rate_like_any_other_source():
    """
    The point of the fix: the paid path becomes measurable. Measured live after
    it, Home Theater 33% (21/63) and Lifestyle 51% (41/81) — and for Lifestyle
    that beat every one of its free keywords.
    """
    rows = ([{"source": "P (influencers.club discovery (Lifestyle Sofa))",
              "label": "Approved"}] * 5
            + [{"source": "P (influencers.club discovery (Lifestyle Sofa))",
                "label": "Rejected"}] * 5)
    rates = keyword_history.approval_rates(rows)
    assert rates["influencers.club discovery"]["rate"] == 0.5
    assert "Lifestyle Sofa" not in rates, "the niche must not become its own bucket"


# --- learning rates ---

def test_a_rate_is_approved_over_total():
    rates = keyword_history.approval_rates(_judged(
        [("kw", "Approved")] * 3 + [("kw", "Rejected")] * 1))
    assert rates["kw"]["rate"] == 0.75
    assert rates["kw"]["approved"] == 3 and rates["kw"]["rejected"] == 1


def test_a_keyword_with_too_little_history_gets_no_rate():
    """
    One verdict must not set a 0% or 100% rate that then orders every future
    candidate on a single reviewer decision.
    """
    rates = keyword_history.approval_rates(_judged([("thin", "Approved")]))
    assert rates["thin"]["rate"] is None
    assert rates["thin"]["approved"] == 1, "the count is still reported"


def test_unjudged_rows_are_ignored_entirely():
    """A pending row is absence of evidence, not evidence."""
    rows = _judged([("kw", "Approved")] * 3)
    rows += [{"source": "x (kw)", "label": "New"},
             {"source": "x (kw)", "label": None}]
    assert keyword_history.approval_rates(rows)["kw"]["rate"] == 1.0


def test_a_row_matched_by_two_keywords_counts_for_both():
    rates = keyword_history.approval_rates(
        [{"source": "x (a, b)", "label": "Approved"}] * 3)
    assert rates["a"]["approved"] == 3 and rates["b"]["approved"] == 3


# --- scoring one candidate ---

def test_the_best_keyword_wins_not_the_average():
    """
    A candidate found by a strong and a weak keyword is one the strong keyword
    found; averaging would punish it for having matched twice.
    """
    rates = keyword_history.approval_rates(
        _judged([("good", "Approved")] * 4 + [("bad", "Rejected")] * 4))
    score, why = keyword_history.priority_score("x (bad, good)", rates)
    assert score == 1.0
    assert "good" in why


def test_a_candidate_with_no_usable_keyword_gets_the_NEUTRAL_PRIOR():
    """
    Not zero. A zero would bury every candidate whose keyword is simply new,
    which is absent data disqualifying it.
    """
    score, why = keyword_history.priority_score("influencers.club discovery", {})
    assert score == keyword_history.NEUTRAL_PRIOR
    assert "no keyword" in why


def test_a_thin_keyword_is_named_in_the_explanation():
    rates = keyword_history.approval_rates(_judged([("thin", "Approved")]))
    score, why = keyword_history.priority_score("x (thin)", rates)
    assert score == keyword_history.NEUTRAL_PRIOR
    assert "thin" in why, "a reviewer must see WHY it could not be ranked"


def test_the_explanation_always_shows_the_record_behind_the_score():
    rates = keyword_history.approval_rates(
        _judged([("kw", "Approved")] * 2 + [("kw", "Rejected")] * 2))
    _, why = keyword_history.priority_score("x (kw)", rates)
    assert "50%" in why and "2/4" in why


# --- the ordering contract ---

def test_rank_returns_EVERY_candidate_and_drops_none():
    """The whole safety argument: this reorders, it never filters."""
    rates = keyword_history.approval_rates(_judged([("bad", "Rejected")] * 5))
    cands = [{"name": n, "source": f"x ({k})"} for n, k in
             (("A", "bad"), ("B", "unknown"), ("C", "bad"))]
    out = keyword_history.rank(cands, rates)
    assert len(out) == 3
    assert {c["name"] for c in out} == {"A", "B", "C"}


def test_high_rate_sorts_before_low_rate():
    rates = keyword_history.approval_rates(
        _judged([("good", "Approved")] * 4 + [("bad", "Rejected")] * 4))
    out = keyword_history.rank([{"source": "x (bad)"}, {"source": "x (good)"}], rates)
    assert out[0]["priority"] == 1.0 and out[1]["priority"] == 0.0


def test_ties_keep_arrival_order_so_the_output_is_stable():
    rates = keyword_history.approval_rates(_judged([("kw", "Approved")] * 3))
    cands = [{"name": str(i), "source": "x (kw)"} for i in range(6)]
    once = [c["name"] for c in keyword_history.rank(cands, rates)]
    assert once == [str(i) for i in range(6)]
    assert once == [c["name"] for c in keyword_history.rank(cands, rates)]


def test_the_original_fields_survive_ranking():
    out = keyword_history.rank([{"name": "A", "source": "x (k)", "subs": 1234}], {})
    assert out[0]["subs"] == 1234 and out[0]["name"] == "A"
    assert "priority" in out[0] and "priority_reason" in out[0]


def test_empty_input_is_empty_output():
    assert keyword_history.rank([], {}) == []
    assert keyword_history.rank(None, {}) == []
    assert keyword_history.approval_rates([]) == {}
    assert keyword_history.approval_rates(None) == {}


def test_no_internal_bookkeeping_leaks_into_the_result():
    out = keyword_history.rank([{"name": "A", "source": "x (k)"}], {})
    assert "_i" not in out[0]
