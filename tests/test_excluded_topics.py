"""
Off-brand topic exclusion: the pipeline must discard political, ASMR, and
firearm/gun-review channels regardless of niche fit — and must NOT wrongly
drop legitimate home-theater / lifestyle channels whose text happens to
contain a landmine word (a shotgun MICROPHONE, a nail/glue gun, a
"conservative palette", "liberal use" of something).

`excluded_topic_reason` reads the channel's own title + About description;
`process_candidate` calls it before spending the performance-fetch quota.
"""
import pytest

from channel_vetting import pipeline
from channel_vetting.discovery import niches
from channel_vetting.discovery.search_zones import ZONE_CORE


class _NullBlocklist:
    handles: set = set()

    def match(self, handle="", email="", name=""):
        return ""


# --- classifier: excluded categories are caught ---------------------------

@pytest.mark.parametrize("text, expected", [
    ("Daily Politics with real election coverage", "political"),
    ("Breaking down the 2024 election", "political"),
    ("MAGA news and commentary", "political"),
    ("Debating socialism vs communism", "political"),
    ("Left-wing takes on Congress", "political"),
    ("Relaxing ASMR whispers for sleep", "asmr"),
    ("Tingles, mouth sounds, and more", "asmr"),
    ("Handgun and rifle reviews", "firearms"),
    ("AR-15 build guide", "firearms"),
    ("Glock ballistics and ammo tests", "firearms"),
    ("Concealed carry tips", "firearms"),
    ("Everything about the second amendment", "firearms"),
])
def test_excluded_categories_are_flagged(text, expected):
    assert pipeline.excluded_topic_reason(text) == expected


def test_matching_is_case_insensitive():
    assert pipeline.excluded_topic_reason("asmr") == "asmr"
    assert pipeline.excluded_topic_reason("ASMR") == "asmr"
    assert pipeline.excluded_topic_reason("Asmr Relaxation") == "asmr"


def test_checks_all_the_texts_passed():
    # title clean, description dirty -> still caught (process_candidate passes both)
    assert pipeline.excluded_topic_reason("My Channel", "we cover firearms and pistols") == "firearms"


# --- classifier: the niche-specific false positives are NOT flagged --------

@pytest.mark.parametrize("text", [
    "Home theater setup with a shotgun microphone for dialogue",   # shotgun MIC
    "DIY furniture builds with a nail gun and a glue gun",          # tool guns
    "A conservative color palette and liberal use of throw pillows",  # adjectives
    "Cozy living room decor and full house tours",
    "Home cinema, surround sound, and projector reviews",
    "Movie magazine reviews and film retrospectives",              # 'magazine' != MAGA
    "Interior design, fashion, and travel vlogs",
    "Man cave AV setup and media room builds",
    "I test speakers with everything from the Sex Pistols to Revolver",  # band + album, not firearms
    "I rifle through vintage thrift finds every week",             # 'rifle through' verb, not a firearm
    "Breaking in new speakers with Parliament-Funkadelic",         # funk band, not politics
    "Logging my progress building a media room",                   # 'logging' verb, not forestry
    "Daily vlogging from my home cinema",                          # 'vlogging' contains 'logging'
    "Car and truck reviews — a deliberate Home Theater keyword",   # 'racing' is NOT a term
    "Solid timber furniture restoration and styling",              # 'timber' deliberately omitted
    "Power tools review: the best chainsaw for the money",         # 'chainsaw' deliberately omitted
    "Building a sim racing cockpit in my man cave",                # a rig build IS on-niche
])
def test_legitimate_channels_are_not_flagged(text):
    assert pipeline.excluded_topic_reason(text) is None


# --- wrong vertical (2026-08-15) -------------------------------------------
# Two channels reached the Home Theater table that no gate could stop, because
# no gate asks about relevance. Both are caught by their own bios. See
# EXCLUDED_TOPIC_TERMS for the general relevance gate that was measured and
# rejected before falling back to this blocklist.


def test_a_racing_game_channel_is_flagged():
    """UCZY-IgNxiP2KUM1Ac8knQfg, verbatim from its About text."""
    assert pipeline.excluded_topic_reason(
        "Dwight Kovich",
        "Hey, I'm Dwight - a full-time content creator bringing high-octane "
        "racing action to life every single day! I specialize in BeamNG.drive "
        "and Assetto Corsa, streaming daily on Twitch and TikTok",
    ) == "sim_racing"


def test_a_forestry_channel_is_flagged():
    """UCGpOEUlhFipK0hTeu2AHCMQ, verbatim from its About text."""
    assert pipeline.excluded_topic_reason(
        "Timber Time",
        "If you love the raw power of logging trucks, daring tree-cutting "
        "skills, and epic battles against mud and rugged terrains, you're in "
        "the right place! We bring you the most breathtaking moments in "
        "forestry, from incredible timber transport to cutting-edge machines",
    ) == "forestry"


def test_the_game_title_matches_despite_the_trailing_dot():
    """'BeamNG.drive' — the word boundary falls at the dot, so the term hits."""
    assert pipeline.excluded_topic_reason("I play BeamNG.drive") == "sim_racing"


def test_the_new_terms_reach_the_server_side_negation_filter():
    """
    Each term also goes to the vendor as keywords_not_in_description, so an
    off-vertical creator is never RETURNED and never billed the 0.01. A term
    added to the local list but missing from the flattened set would leave the
    credit leak open while the local gate looked like it was working.
    """
    for term in ("beamng", "assetto corsa", "forestry", "logging truck"):
        assert term in niches.EXCLUDED_TOPIC_KEYWORDS


def test_empty_and_none_texts_are_safe():
    assert pipeline.excluded_topic_reason() is None
    assert pipeline.excluded_topic_reason("", None, "") is None


# --- wiring: process_candidate drops before spending quota -----------------

def test_process_candidate_drops_excluded_topic_before_performance(monkeypatch):
    calls = {"perf": 0}

    monkeypatch.setattr(
        pipeline, "get_channel_stats",
        lambda channel_id=None, *, handle=None: {
            "channel_id": "UC1", "channel_title": "Daily Politics",
            "handle": "h1", "description": "political commentary and election analysis",
        },
    )

    def fake_perf(*a, **k):
        calls["perf"] += 1
        return {"avg_views": 50000}

    monkeypatch.setattr(pipeline, "get_recent_video_performance", fake_perf)
    monkeypatch.setattr(pipeline.time, "sleep", lambda *a, **k: None)

    record, reason = pipeline.process_candidate(
        {"channel_id": "UC1", "channel_title": "Daily Politics"}, {}, _NullBlocklist(),
        {"min_avg_views": 10_000, "min_channel_age_months": 12, "allowed_country_codes": ZONE_CORE}, None,
    )
    assert record is None
    assert reason == pipeline.DROP_EXCLUDED_TOPIC
    assert calls["perf"] == 0, "excluded channel must be dropped before the performance-fetch quota is spent"


# --- wiring: the same terms are negated SERVER-SIDE in discovery ------------
# The local gate above is a post-response BACKSTOP — on the discovery path the
# 0.01 discovery credit is already billed by the time it runs. These assert the
# credit-saving tier: the off-brand terms are handed to influencers.club as
# `keywords_not_in_description`, so the categories are never returned (or
# billed) in the first place, the way exclude_handles already avoids paying for
# already-known creators.

def test_both_niches_carry_the_discovery_negation_filter():
    # The wired list is the UNION of two bio-level negation sets (widened
    # 2026-08-21 to add the gaming / generic-tech terms the brief asked for).
    # Asserted as a union rather than as either half, so neither can be dropped
    # silently: losing EXCLUDED_TOPIC_KEYWORDS would re-open the off-brand
    # topics, and losing GAMING_AND_TECH_BIO_NEGATIONS would start paying 0.01
    # again for creators the local gate then throws away.
    expected = sorted(
        set(niches.EXCLUDED_TOPIC_KEYWORDS) | set(niches.GAMING_AND_TECH_BIO_NEGATIONS)
    )
    for niche_name, cfg in pipeline.NICHES.items():
        filters = cfg["discovery_filters"]
        wired = filters.get("keywords_not_in_description")
        assert wired == expected, niche_name
        # Both halves present, stated explicitly so a future edit that replaces
        # rather than merges fails here with an obvious message.
        assert set(niches.EXCLUDED_TOPIC_KEYWORDS) <= set(wired), niche_name
        assert set(niches.GAMING_AND_TECH_BIO_NEGATIONS) <= set(wired), niche_name


def test_the_vendor_negations_never_include_an_on_niche_word():
    """
    Every term here shrinks the discovery pool and a false negation is
    unrecoverable — the creator is never shown to us at all. So no word that a
    legitimate prospect would put in their own bio may appear.
    """
    wired = set(pipeline.NICHES["Home Theater"]["discovery_filters"]["keywords_not_in_description"])
    for on_niche in ("speaker", "projector", "soundbar", "atmos", "surround",
                     "hi-fi", "home theater", "man cave", "nvidia", "home audio",
                     "decor", "interior", "furniture", "diy"):
        assert on_niche not in wired, f"{on_niche!r} is on-niche and must not be negated"


def test_discovery_negation_reuses_the_gate_terms_verbatim():
    """Derived FROM EXCLUDED_TOPIC_TERMS, not a hand-kept copy, so the server
    pre-filter and the local backstop can't drift — every wired term is one the
    local gate also recognises."""
    assert niches.EXCLUDED_TOPIC_KEYWORDS == sorted(
        {t for terms in niches.EXCLUDED_TOPIC_TERMS.values() for t in terms}
    )
    for term in niches.EXCLUDED_TOPIC_KEYWORDS:
        assert pipeline.excluded_topic_reason(term) is not None, term


def test_discovery_negation_omits_the_same_landmines_the_gate_omits():
    """The vendor field matches case-insensitive whole words/phrases, exactly
    like the gate — so the words the gate leaves out to protect the two niches
    (a shotgun MIC, a nail gun, a 'conservative palette') must not sneak into
    the discovery list either."""
    for landmine in ("gun", "shotgun", "rifle", "pistol", "conservative", "liberal", "parliament"):
        assert landmine not in niches.EXCLUDED_TOPIC_KEYWORDS
