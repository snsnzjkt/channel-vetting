"""The search window is configurable and defaults to a recent rolling window."""


def test_default_window_is_recent_not_ninety_days():
    import config

    assert config.DISCOVERY_DAYS_BACK == 7


def test_caps_sum_to_forty():
    """The requirement is ~30-40 new rows per table per day, total."""
    import config

    assert config.DAILY_QUALIFIED_CAP == 30
    assert config.DAILY_FLAGGED_CAP == 10
    assert config.DAILY_QUALIFIED_CAP + config.DAILY_FLAGGED_CAP == 40


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
    monkeypatch.setattr(sys, "argv", ["main.py", "--test", "--days-back", "43"])

    main.main()
    assert captured["days_back"] == 43


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
