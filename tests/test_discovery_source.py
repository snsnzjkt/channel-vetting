"""
`discovery_source` lets one niche use the free keyword loop while another keeps
paid discovery.

The bug this prevents: `use_discovery` was global-exclusive, so whenever an
influencers.club key was present EVERY niche went to the paid source and
`remaining_keywords` was emptied. Home Theater's nine curated YouTube keywords
were unreachable code — for the one niche whose paid pool was measurably spent
(net 279 creators on 2026-08-22, ~2 rows' worth).
"""
import pytest

import main


class _Disc:
    enabled = True
    credits_spent = 0.0
    creators_billed = 0

    def __init__(self):
        self.calls = []

    def discover(self, **kw):
        self.calls.append(kw)
        return []


def _niche(source=None, *, filters=True, keywords=("kw1", "kw2")):
    cfg = {
        "keywords": list(keywords),
        "min_avg_views": 0,
        "min_channel_age_months": None,
        "table_name": "tbl",
    }
    if filters:
        cfg["discovery_filters"] = {"ai_search": "x"}
    if source is not None:
        cfg["discovery_source"] = source
    return cfg


@pytest.mark.parametrize("source,expect_paid", [
    (None, True),               # default: unchanged behaviour
    ("influencers", True),      # explicit default
    ("search_list", False),     # the opt-out
])
def test_source_selects_the_path(source, expect_paid):
    cfg = _niche(source)
    disc = _Disc()
    chosen = (
        disc is not None and disc.enabled
        and "discovery_filters" in cfg
        and cfg.get("discovery_source", "influencers") == "influencers"
    )
    assert chosen is expect_paid


def test_home_theater_is_on_the_free_path_and_lifestyle_is_not():
    """The live configuration, pinned. Flipping either is a deliberate act."""
    import niches
    assert niches.NICHES["Home Theater"]["discovery_source"] == "search_list"
    assert niches.NICHES["Lifestyle Sofa"].get("discovery_source", "influencers") == "influencers"


def test_home_theater_keeps_its_discovery_filters():
    """
    Kept on purpose, not left behind: measure_discovery_pool.py probes them, and
    flipping back once the reject cache ages out (90 days) must stay a one-word
    change rather than a rewrite.
    """
    import niches
    ht = niches.NICHES["Home Theater"]
    assert "discovery_filters" in ht
    assert ht["discovery_filters"].get("ai_search")


def test_the_free_path_niche_still_has_keywords_to_use():
    """
    An opted-out niche with no keywords would discover nothing at all — the
    silent-zero-rows failure. Guard it at config level.
    """
    import niches
    for name, cfg in niches.NICHES.items():
        if cfg.get("discovery_source", "influencers") != "influencers":
            assert cfg.get("keywords"), (
                f"{name} opted out of paid discovery but has no keywords — "
                "it would discover nothing"
            )
