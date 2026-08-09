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
