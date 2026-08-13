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

import main


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
    assert main.excluded_topic_reason(text) == expected


def test_matching_is_case_insensitive():
    assert main.excluded_topic_reason("asmr") == "asmr"
    assert main.excluded_topic_reason("ASMR") == "asmr"
    assert main.excluded_topic_reason("Asmr Relaxation") == "asmr"


def test_checks_all_the_texts_passed():
    # title clean, description dirty -> still caught (process_candidate passes both)
    assert main.excluded_topic_reason("My Channel", "we cover firearms and pistols") == "firearms"


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
])
def test_legitimate_channels_are_not_flagged(text):
    assert main.excluded_topic_reason(text) is None


def test_empty_and_none_texts_are_safe():
    assert main.excluded_topic_reason() is None
    assert main.excluded_topic_reason("", None, "") is None


# --- wiring: process_candidate drops before spending quota -----------------

def test_process_candidate_drops_excluded_topic_before_performance(monkeypatch):
    calls = {"perf": 0}

    monkeypatch.setattr(
        main, "get_channel_stats",
        lambda channel_id=None, *, handle=None: {
            "channel_id": "UC1", "channel_title": "Daily Politics",
            "handle": "h1", "description": "political commentary and election analysis",
        },
    )

    def fake_perf(*a, **k):
        calls["perf"] += 1
        return {"avg_views": 50000}

    monkeypatch.setattr(main, "get_recent_video_performance", fake_perf)
    monkeypatch.setattr(main.time, "sleep", lambda *a, **k: None)

    record, reason = main.process_candidate(
        {"channel_id": "UC1", "channel_title": "Daily Politics"}, {}, _NullBlocklist(),
        {"min_avg_views": 10_000, "min_channel_age_months": 12}, None,
    )
    assert record is None
    assert reason == main.DROP_EXCLUDED_TOPIC
    assert calls["perf"] == 0, "excluded channel must be dropped before the performance-fetch quota is spent"
