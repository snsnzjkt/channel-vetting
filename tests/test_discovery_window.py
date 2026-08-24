"""The search window is configurable and defaults to a recent rolling window."""


def test_default_window_is_recent_not_ninety_days():
    import config

    assert config.DISCOVERY_DAYS_BACK == 7


def test_the_daily_caps_are_the_operator_s_chosen_throughput():
    """
    Was `test_caps_sum_to_forty`, asserting 30 + 10 against the original brief's
    "~30-40 new rows per table per day".

    RAISED to 60 + 10 on 2026-08-25 by operator decision, on measured evidence
    that the cap and not the gates was refusing rows. From the 18:40 run:

        'Lifestyle Sofa': 30/30 qualified and 0/10 flagged already added today.
        Discovery request: got 50 new candidate(s) (50 backlogged)
        'Lifestyle Sofa' so far: 0/0 qualified

    Fifty candidates in hand, 0.50 credits already spent fetching them, zero
    headroom to push any. Home Theater was at 28/30 the same run.

    The operator's instruction was explicit: more volume, same process, and
    "it will still be manually reviewed before approval" — so the cap is a
    throughput knob, not a quality one, and human review is the gate.

    This test is kept as a POLICY assertion rather than deleted. The number is a
    deliberate choice with a credit and a reviewer-attention cost behind it (see
    config.py), so it should fail loudly if someone changes it by accident.
    """
    import config

    assert config.DAILY_QUALIFIED_CAP == 60
    assert config.DAILY_FLAGGED_CAP == 10, (
        "the flagged budget is a separate ceiling so a weak discovery day cannot "
        "crowd out real prospects — raising it is a different decision"
    )


def test_run_passes_configured_window_to_discovery(monkeypatch):
    """run() must not hardcode 90 any more."""
    import main

    seen = {}

    def fake_run_niche(niche_name, table_name, keywords, max_results, days_back, *a, **k):
        seen["days_back"] = days_back
        return 0, 0, set(), True

    monkeypatch.setattr(main, "run_niche", fake_run_niche)
    monkeypatch.setattr(main, "fetch_blocklist", lambda: object())
    monkeypatch.setattr(main, "get_existing_channel_ids", lambda t: set())
    monkeypatch.setattr(main, "fetch_external_handles", lambda: {})
    monkeypatch.setattr(main, "get_today_spend", lambda: 0)

    main.run(niches=main.NICHES, max_results_per_keyword=50, days_back=7)
    assert seen["days_back"] == 7


def test_days_back_cli_override(monkeypatch):
    """--days-back lets a one-off wide sweep reach the backlog.

    Uses 43 rather than 90 deliberately: 90 was the old hardcoded value, so
    a test asserting 90 would still pass even if the full-run branch ignored
    args.days_back and kept hardcoding 90 — 90 == 90 either way. 43 matches
    no default anywhere (not 7, not 90), so it can only reach `run()` if the
    CLI value is genuinely plumbed through.
    """
    import sys

    import main

    captured = {}
    monkeypatch.setattr(main, "run", lambda **kw: captured.update(kw))
    monkeypatch.setattr(sys, "argv", ["main.py", "--days-back", "43"])

    main.main()
    assert captured["days_back"] == 43


def test_days_back_cli_override_in_test_mode(monkeypatch):
    """--test must also forward the resolved --days-back, not hardcode 90.

    Same rationale as above (43, not 90) — and this covers the --test
    branch specifically, which had zero coverage before: reverting
    `run(niches=test_niches, max_results_per_keyword=5, days_back=90)` to
    ignore args.days_back left the previous suite fully green.
    """
    import sys

    import main

    captured = {}
    monkeypatch.setattr(main, "run", lambda **kw: captured.update(kw))
    # --test now also bounds the daily caps (so a test run can't discover
    # toward a full 30-row day of real credits/quota). main() reassigns these
    # module globals, which would otherwise LEAK into later tests — snapshot
    # them through monkeypatch so they're restored when this test ends.
    monkeypatch.setattr(main, "DAILY_QUALIFIED_CAP", main.DAILY_QUALIFIED_CAP)
    monkeypatch.setattr(main, "DAILY_FLAGGED_CAP", main.DAILY_FLAGGED_CAP)
    monkeypatch.setattr(sys, "argv", ["main.py", "--test", "--days-back", "43"])

    main.main()
    assert captured["days_back"] == 43
    # --test with no explicit --daily-cap bounds the run so discovery stays cheap.
    assert main.DAILY_QUALIFIED_CAP == 2
    assert main.DAILY_FLAGGED_CAP == 1


def test_days_back_defaults_to_discovery_days_back_when_omitted(monkeypatch):
    """With no --days-back flag, the resolved value must track config.DISCOVERY_DAYS_BACK.

    Pins the argparse default to the config constant (rather than a bare
    literal), so a future change to config.DISCOVERY_DAYS_BACK is reflected
    here without a separate edit, and a regression to a hardcoded default
    is caught.
    """
    import sys

    import main
    import config

    captured = {}
    monkeypatch.setattr(main, "run", lambda **kw: captured.update(kw))
    monkeypatch.setattr(sys, "argv", ["main.py"])

    main.main()
    assert captured["days_back"] == config.DISCOVERY_DAYS_BACK
