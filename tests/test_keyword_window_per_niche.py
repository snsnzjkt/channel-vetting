"""
Each niche can set its own keyword-search window and depth
(`keyword_days_back`, `keyword_max_results`), and an explicit --days-back
still wins over both.

Lifestyle Sofa relies on this: it searches 90 days x 2 pages while Home
Theater stays on the 7-day, 1-page default. Without these tests, a refactor
that dropped the per-niche lookup would quietly send Lifestyle back to 2-5
rows a day with nothing failing. No network: discovery, Airtable and
enrichment are all monkeypatched.
"""
from channel_vetting import pipeline
from channel_vetting.config import DEFAULT_MAX_RESULTS_PER_KEYWORD, DISCOVERY_DAYS_BACK
from channel_vetting.discovery.niches import NICHES
from channel_vetting.discovery.search_zones import ZONE_CORE


class _NullBlocklist:
    def match(self, handle="", email="", name=""):
        return ""


def _run(monkeypatch, niche_extra, days_back, max_results):
    calls = []

    def fake_run_discovery(keywords, max_results_per_keyword=50, days_back=90,
                           exclude_ids=None, target_fresh=None):
        calls.append((max_results_per_keyword, days_back))
        return []

    monkeypatch.setattr(pipeline, "run_discovery", fake_run_discovery)
    monkeypatch.setattr(pipeline, "push_record", lambda t, r: True)
    monkeypatch.setattr(pipeline, "count_added_today", lambda table, qualification=None: 0)

    pipeline.run_niche(
        niche_name="Test",
        table_name="tbl",
        keywords=["kw"],
        max_results_per_keyword=max_results,
        days_back=days_back,
        globally_tracked_ids=set(),
        external_handles={},
        blocklist=_NullBlocklist(),
        niche_config={"min_avg_views": 10_000, "min_channel_age_months": None,
                      "allowed_country_codes": ZONE_CORE, **niche_extra},
        scraper=None,
    )
    return calls


def test_unset_niche_uses_the_global_defaults(monkeypatch):
    assert _run(monkeypatch, {}, None, None) == [
        (DEFAULT_MAX_RESULTS_PER_KEYWORD, DISCOVERY_DAYS_BACK)
    ]


def test_niche_override_applies_when_nothing_explicit_was_passed(monkeypatch):
    extra = {"keyword_days_back": 90, "keyword_max_results": 100}
    assert _run(monkeypatch, extra, None, None) == [(100, 90)]


def test_explicit_values_beat_the_niche_override(monkeypatch):
    # --days-back 30 on the command line, and --test's max_results=5: both are
    # a human's choice for this run and must not be widened by the niche.
    extra = {"keyword_days_back": 90, "keyword_max_results": 100}
    assert _run(monkeypatch, extra, 30, 5) == [(5, 30)]


def test_lifestyle_is_wide_and_home_theater_is_not():
    assert NICHES["Lifestyle Sofa"]["keyword_days_back"] == 90
    assert NICHES["Lifestyle Sofa"]["keyword_max_results"] == 100
    assert "keyword_days_back" not in NICHES["Home Theater"]
    assert "keyword_max_results" not in NICHES["Home Theater"]
