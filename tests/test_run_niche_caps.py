"""Caps count successful pushes only, and the two budgets are separate."""
import pytest


def test_qualified_cap_stops_the_loop(monkeypatch):
    import main

    pushed = []
    monkeypatch.setattr(main, "push_record", lambda t, r: pushed.append(r) or True)

    remaining = main.push_until_full(
        candidates=[{"channel_id": f"UC{i}"} for i in range(10)],
        build_record=lambda c: ({"Channel ID": c["channel_id"], "Qualification": "Qualified"}, "Qualified"),
        table_name="tbl",
        qualified_headroom=3,
        flagged_headroom=0,
    )
    assert len(pushed) == 3
    assert remaining["qualified"] == 3


def test_flagged_have_their_own_budget(monkeypatch):
    import main

    pushed = []
    monkeypatch.setattr(main, "push_record", lambda t, r: pushed.append(r) or True)

    result = main.push_until_full(
        candidates=[{"channel_id": f"UC{i}"} for i in range(10)],
        build_record=lambda c: (
            {"Channel ID": c["channel_id"], "Qualification": "Below View Minimum"},
            "Below View Minimum",
        ),
        table_name="tbl",
        qualified_headroom=5,
        flagged_headroom=2,
    )
    assert len(pushed) == 2
    assert result["flagged"] == 2
    assert result["qualified"] == 0


def test_failed_push_does_not_consume_budget(monkeypatch):
    """Regression: the old loop counted attempts, not successes."""
    import main

    attempts = {"n": 0}

    def flaky_push(table, record):
        attempts["n"] += 1
        return attempts["n"] > 2  # first two fail

    monkeypatch.setattr(main, "push_record", flaky_push)

    result = main.push_until_full(
        candidates=[{"channel_id": f"UC{i}"} for i in range(10)],
        build_record=lambda c: ({"Channel ID": c["channel_id"], "Qualification": "Qualified"}, "Qualified"),
        table_name="tbl",
        qualified_headroom=3,
    )
    assert result["qualified"] == 3
    assert attempts["n"] == 5  # two failures + three successes


def test_a_fruitless_flagged_hunt_stops_enriching(monkeypatch):
    """
    Once the qualified budget is full, only a flagged row can still be written,
    and that needs qualify() to return something other than "Qualified" — which
    for a niche with min_channel_age_months=None is impossible. build_record IS
    the enrichment (3-13 YouTube units, charged before qualification is known),
    so the loop must not run the whole batch hunting a row that cannot exist.
    """
    import main

    built = []
    monkeypatch.setattr(main, "push_record", lambda t, r: True)

    def build_record(candidate):
        built.append(candidate["channel_id"])
        return {"Channel ID": candidate["channel_id"], "Qualification": "Qualified"}, "Qualified"

    result = main.push_until_full(
        candidates=[{"channel_id": f"UC{i}"} for i in range(500)],
        build_record=build_record,
        table_name="tbl",
        qualified_headroom=1,
        flagged_headroom=5,          # can never fill: everything is "Qualified"
    )

    assert result["qualified"] == 1
    assert result["flagged"] == 0
    # 1 to fill the qualified budget, then at most FLAGGED_ONLY_PATIENCE more.
    assert len(built) <= 1 + main.FLAGGED_ONLY_PATIENCE, (
        f"enriched {len(built)} candidates chasing an unfillable flagged budget"
    )


def test_no_hunt_at_all_when_flagged_is_impossible(monkeypatch):
    """
    A niche with min_channel_age_months=None (Lifestyle Sofa) can NEVER produce
    a flagged row — scoring.qualify() returns QUALIFIED unconditionally without
    an age requirement. So once the qualified budget fills there is nothing to
    hunt for, and spending even FLAGGED_ONLY_PATIENCE enrichments to rediscover
    that is pure waste. Exact, not probabilistic: zero extra enrichments.
    """
    import main

    built = []
    monkeypatch.setattr(main, "push_record", lambda t, r: True)

    def build_record(candidate):
        built.append(candidate["channel_id"])
        return {"Channel ID": candidate["channel_id"], "Qualification": "Qualified"}, "Qualified"

    result = main.push_until_full(
        candidates=[{"channel_id": f"UC{i}"} for i in range(500)],
        build_record=build_record,
        table_name="tbl",
        qualified_headroom=1,
        flagged_headroom=5,
        flagged_possible=False,     # the niche sets no min_channel_age_months
    )

    assert result["qualified"] == 1
    assert len(built) == 1, (
        f"enriched {len(built)} candidates hunting a row that cannot exist"
    )


def test_the_flagged_hunt_continues_while_it_is_working(monkeypatch):
    """The brake must not cut off a niche that IS producing flagged rows."""
    import main

    monkeypatch.setattr(main, "push_record", lambda t, r: True)

    result = main.push_until_full(
        candidates=[{"channel_id": f"UC{i}"} for i in range(60)],
        build_record=lambda c: (
            {"Channel ID": c["channel_id"], "Qualification": "New Channel"},
            "New Channel",
        ),
        table_name="tbl",
        qualified_headroom=0,        # already full, so every row is flagged
        flagged_headroom=20,
    )
    assert result["flagged"] == 20, "the patience brake cut off a productive hunt"


def test_zero_headroom_pushes_nothing(monkeypatch):
    import main

    monkeypatch.setattr(main, "push_record", lambda t, r: pytest.fail("should not push"))

    result = main.push_until_full(
        candidates=[{"channel_id": "UC1"}],
        build_record=lambda c: ({"Channel ID": "UC1", "Qualification": "Qualified"}, "Qualified"),
        table_name="tbl",
        qualified_headroom=0,
        flagged_headroom=0,
    )
    assert result == {"qualified": 0, "flagged": 0, "skipped": 0, "pushed_ids": set()}
