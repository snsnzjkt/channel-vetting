"""
The pre-push gate: candidates that never reach Airtable at all.

This is a deliberate exception to the project's "flag, never discard" rule
(see CLAUDE.md > Qualification). After the 2026-08 criteria change it holds
every hard requirement:

  - Below the niche's average-view floor (10,000 for both niches). This
    used to be a "Below View Minimum" row for a human to dismiss; it is now
    a discard, which is what retired that Qualification value.
  - Fewer than MIN_VIDEO_COUNT (30) public videos — not enough of a
    published track record to approach.
  - Outside the allowed search zones (US, Canada, UK, Europe, Australia;
    Ireland excluded). Checked separately in process_candidate rather than
    here, since resolving the country can cost a browser page load.
  - Dead channels, on BOTH measures, so a small-but-real channel survives.
  - Shorts-only channels, which aren't the format these briefs are buying.

Shorts are detected by duration, since the API has no Shorts flag.
"""
import pytest


# A catalogue and a view count that comfortably clear the new floors, so
# each test below is exercising the one gate it names.
PASSING_VIDEOS = 100
PASSING_VIEWS = 50_000
HOME_THEATER_MIN_VIEWS = 10_000


def drop_reason(**overrides):
    """pre_push_drop_reason with everything passing unless overridden."""
    from main import pre_push_drop_reason

    kwargs = {
        "subscriber_count": 25_000,
        "avg_views": PASSING_VIEWS,
        "shorts_only": False,
        "min_avg_views": HOME_THEATER_MIN_VIEWS,
        "video_count": PASSING_VIDEOS,
        # Explicit because the gate treats an unset language as a FAILURE —
        # see main.is_english(). Passing it here keeps each test below
        # exercising the one gate it names.
        "content_language": "en",
    }
    kwargs.update(overrides)
    return pre_push_drop_reason(**kwargs)


def test_a_channel_clearing_every_gate_is_kept():
    assert drop_reason() is None


# --- the view floor, now a discard rather than a flag ---------------------


def test_drops_a_channel_below_the_view_floor():
    assert drop_reason(avg_views=9_999) == "below_view_minimum"


def test_exactly_at_the_view_floor_is_kept():
    assert drop_reason(avg_views=10_000) is None


def test_the_floor_comes_from_the_niche_not_a_constant():
    """
    Both niches sit at 10,000 today, but the threshold is still passed in
    per-niche — a hardcoded 10,000 here would silently ignore a niche that
    is later given a different bar.
    """
    assert drop_reason(avg_views=5_000, min_avg_views=2_000) is None
    assert drop_reason(avg_views=5_000, min_avg_views=10_000) == "below_view_minimum"


def test_missing_avg_views_is_treated_as_zero():
    """None here means the performance window produced nothing, not 'lots'."""
    assert drop_reason(avg_views=None) == "below_view_minimum"


def test_a_channel_that_used_to_be_flagged_is_now_dropped():
    """
    Review Center: 281 subs / 38.5 avg views, a live row that survived the
    old gate and landed as "Below View Minimum" for a human to dismiss.
    The view floor is what removes that cost.
    """
    assert drop_reason(subscriber_count=281, avg_views=38.5) == "below_view_minimum"


# --- the video-count floor -----------------------------------------------


def test_drops_a_channel_with_too_few_videos():
    assert drop_reason(video_count=29) == "too_few_videos"


def test_exactly_at_the_video_floor_is_kept():
    from main import MIN_VIDEO_COUNT

    assert MIN_VIDEO_COUNT == 30
    assert drop_reason(video_count=30) is None


def test_the_video_floor_has_no_upper_bound():
    """
    "30-40 videos" is a minimum catalogue size, not a band. A prolific
    channel clears the bar rather than failing it.
    """
    assert drop_reason(video_count=40) is None
    assert drop_reason(video_count=41) is None
    assert drop_reason(video_count=4_000) is None


def test_an_unreported_video_count_does_not_disqualify():
    """
    None means channels.list didn't report one. Absent data is not
    evidence against a channel — same rule as an unknown channel age.
    """
    assert drop_reason(video_count=None) is None


def test_a_reported_zero_video_count_is_a_real_answer():
    assert drop_reason(video_count=0) == "too_few_videos"


# --- dead-channel gate ----------------------------------------------------


def test_drops_a_channel_dead_on_both_measures():
    assert drop_reason(subscriber_count=5, avg_views=5, min_avg_views=0) == "dead_channel"


def test_keeps_a_channel_with_few_subs_but_real_views():
    """A new channel with traction is a prospect, not junk."""
    assert drop_reason(subscriber_count=40, avg_views=80_000) is None


def test_keeps_a_channel_with_many_subs_but_few_views():
    """
    Low views on a big channel is a review signal, not a reason to hide it
    — but only while the view floor lets it through at all, which is why
    this asserts against the dead-channel gate specifically.
    """
    assert drop_reason(subscriber_count=469_000, avg_views=4_902, min_avg_views=0) is None


def test_both_dead_thresholds_are_exclusive_bounds():
    """100/100 is the floor: at exactly 100 the channel is kept."""
    assert drop_reason(subscriber_count=100, avg_views=100, min_avg_views=0) is None
    assert drop_reason(subscriber_count=99, avg_views=99, min_avg_views=0) == "dead_channel"


def test_the_dead_rows_from_the_live_table_are_dropped():
    """SmartFindsDaily, James Bhattrai and Easy Tech Reviews, live values."""
    for subs, views in [(5, 5), (5, 5), (0, 0)]:
        assert drop_reason(subscriber_count=subs, avg_views=views, min_avg_views=0) == "dead_channel"


def test_the_lowest_real_prospect_survives():
    """Retro Media Crypt — the lowest Qualified channel in the live table."""
    assert drop_reason(subscriber_count=2_400, avg_views=16_159.8) is None


# --- Shorts-only gate -----------------------------------------------------


def test_drops_a_shorts_only_channel():
    assert drop_reason(shorts_only=True) == "shorts_only"


def test_shorts_gate_applies_regardless_of_how_strong_the_channel_is():
    assert drop_reason(subscriber_count=2_000_000, avg_views=900_000, shorts_only=True) == "shorts_only"


# --- duration parsing -----------------------------------------------------


def test_parses_iso8601_durations():
    from enrichment import parse_iso8601_duration

    assert parse_iso8601_duration("PT45S") == 45
    assert parse_iso8601_duration("PT1M") == 60
    assert parse_iso8601_duration("PT1M1S") == 61
    assert parse_iso8601_duration("PT12M35S") == 755
    assert parse_iso8601_duration("PT1H2M3S") == 3723


def test_unparseable_duration_is_not_treated_as_a_short():
    """
    A missing or malformed duration must not make a channel look like
    Shorts — that would discard it with no row to review.
    """
    from enrichment import parse_iso8601_duration

    assert parse_iso8601_duration("") is None
    assert parse_iso8601_duration(None) is None
    assert parse_iso8601_duration("P0D") is None


def test_shorts_only_needs_every_sampled_video_under_the_cap():
    from enrichment import is_shorts_only

    assert is_shorts_only(["PT30S", "PT59S", "PT60S"]) is True
    assert is_shorts_only(["PT30S", "PT61S"]) is False


def test_shorts_only_is_false_when_nothing_is_known():
    """No durations means no evidence, and no evidence never discards."""
    from enrichment import is_shorts_only

    assert is_shorts_only([]) is False
    assert is_shorts_only(["", None]) is False


def test_an_unreadable_duration_among_shorts_does_not_force_a_drop():
    """One unparseable entry is enough doubt to keep the channel."""
    from enrichment import is_shorts_only

    assert is_shorts_only(["PT30S", "PT45S", ""]) is False


# --- the English content-language gate ------------------------------------


def test_drops_a_channel_whose_content_is_not_english():
    assert drop_reason(content_language="de") == "not_english"
    assert drop_reason(content_language="pl") == "not_english"
    assert drop_reason(content_language="vi") == "not_english"


def test_regional_english_variants_all_pass():
    """
    en-GB/en-US/en-AU must pass, and must NOT be normalised to a bare "en":
    resolve_country() reads the region subtag to place channels that declare
    no country, and that is the only zone signal for ~15% of candidates.
    """
    for tag in ("en", "en-US", "en-GB", "en-AU", "en-CA", "EN-gb"):
        assert drop_reason(content_language=tag) is None, tag


def test_an_unset_language_is_dropped_not_kept():
    """
    The deliberate exception to "absent data never disqualifies": the table
    is specified to hold English channels, and a blank can't satisfy that.
    Measured cost when this was chosen: 0 of 47 otherwise-qualifying
    candidates had an unset language.
    """
    assert drop_reason(content_language="") == "not_english"
    assert drop_reason(content_language=None) == "not_english"
    assert drop_reason(content_language="   ") == "not_english"


def test_zxx_no_linguistic_content_is_not_english():
    """`zxx` is the ISO code for "no linguistic content" — a real value seen
    on a live candidate, and not a channel to approach in English."""
    assert drop_reason(content_language="zxx") == "not_english"


def test_a_language_merely_containing_en_is_not_english():
    """Prefix match, not substring: "ben" (Bengali) must not pass as English."""
    assert drop_reason(content_language="ben") == "not_english"
    assert drop_reason(content_language="hen") == "not_english"


# --- the long-form (non-Shorts) video floor -------------------------------


def test_counts_only_confirmed_long_form_videos():
    from enrichment import count_longform

    assert count_longform(["PT30S", "PT61S", "PT12M"]) == 2
    assert count_longform(["PT30S", "PT59S"]) == 0
    assert count_longform([]) == 0


def test_an_unreadable_duration_never_counts_as_long_form():
    """
    Opposite lean from is_shorts_only, and for the same reason: this number
    feeds a MINIMUM, so counting an unknown would let a channel clear the bar
    on missing data.
    """
    from enrichment import count_longform

    assert count_longform(["", None, "P0D", "garbage"]) == 0
    assert count_longform(["PT12M", ""]) == 1


def test_drops_a_shorts_factory_that_posts_the_occasional_long_video():
    """
    The gap this gate closes. is_shorts_only() only catches 100%-Shorts
    channels, and statistics.videoCount counts Shorts as videos — so a
    channel with 300 Shorts and 4 long-form uploads cleared both checks.
    """
    from main import longform_drop_reason

    assert longform_drop_reason(4) == "too_few_longform_videos"
    assert longform_drop_reason(29) == "too_few_longform_videos"


def test_exactly_thirty_long_form_videos_is_kept():
    from main import longform_drop_reason

    assert longform_drop_reason(30) is None
    assert longform_drop_reason(400) is None


# --- precedence between the gates ----------------------------------------


@pytest.mark.parametrize(
    "overrides,expected",
    [
        # Shorts wins over everything: it's the verdict about what the
        # channel IS, and the cheapest thing to report.
        ({"shorts_only": True, "video_count": 2, "avg_views": 1}, "shorts_only"),
        # Catalogue size before view count — a 5-video channel's average
        # views aren't a meaningful number to report against.
        ({"video_count": 2, "avg_views": 1}, "too_few_videos"),
        # View floor before the dead-channel gate, since it's the criterion
        # the reviewer actually set.
        ({"subscriber_count": 5, "avg_views": 5}, "below_view_minimum"),
    ],
)
def test_gate_precedence_is_stable(overrides, expected):
    """
    Only one reason is reported, and it goes in the log rather than into
    Airtable. Pinned anyway so the log stays readable and a reordering is a
    visible decision.
    """
    assert drop_reason(**overrides) == expected
