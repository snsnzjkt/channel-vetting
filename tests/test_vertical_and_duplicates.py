"""
Two ways a channel's own upload habits corrupted its performance sample, both
found on UCZY-IgNxiP2KUM1Ac8knQfg ("Dwight Kovich") on 2026-08-15.

That channel posts every video TWICE — a vertical cut and a landscape cut of
the same recording — and only the vertical one is watched:

    MYSTERY DOORS CHALLANGE IN BEAMNG (Portrait)   3h52m54s   720x1280   8,370
    MYSTERY DOORS CHALLANGE IN BEAMNG              3h52m53s   720x405      105

Both cuts are hours long, so the duration-only Shorts test called both
long-form, and the sample came out as five videos over 10k and five under —
exactly the 50% the per-video floor allows. It was written as a prospect on a
16,686-view "average" that no viewer experiences.

The fixes are independent and both are needed:
  - VERTICAL is short-form at any duration (is_short_form).
  - A re-upload of the same video is ONE video (drop_duplicate_uploads).
"""
import pytest

import enrichment
from enrichment import (
    DUPLICATE_DURATION_TOLERANCE_SECONDS,
    drop_duplicate_uploads,
    is_short_form,
    is_shorts_only,
    is_vertical,
    count_longform,
    normalize_video_title,
    video_shape,
)

LANDSCAPE = ("PT3H52M53S", "720", "405")
PORTRAIT = ("PT3H52M54S", "720", "1280")
SQUARE = ("PT20M", "720", "720")
UNKNOWN_ORIENTATION = ("PT20M", None, None)


# --- orientation ------------------------------------------------------------


def test_a_vertical_upload_is_short_form_however_long_it_is():
    """The live case: a 3h52m vertical video is not long-form content."""
    assert is_short_form(PORTRAIT) is True


def test_a_long_landscape_upload_is_still_long_form():
    assert is_short_form(LANDSCAPE) is False


def test_a_square_upload_is_not_vertical():
    """1:1 sits above MAX_VERTICAL_ASPECT_RATIO, so it is judged on duration."""
    assert is_vertical("720", "720") is False
    assert is_short_form(SQUARE) is False


@pytest.mark.parametrize("width,height", [(None, None), ("", ""), ("0", "1280"), ("abc", "x")])
def test_unreadable_dimensions_are_unknown_not_vertical(width, height):
    """
    Absent data must not classify. Reading a missing embed size as vertical
    would discard channels on the basis of a parameter we forgot to send.
    """
    assert is_vertical(width, height) is None


def test_orientation_unknown_falls_back_to_duration():
    """A bare duration string carries no orientation — the pre-2026-08-15
    behaviour, preserved exactly for callers that only hold durations."""
    assert is_short_form("PT20M") is False
    assert is_short_form("PT30S") is True
    assert is_short_form(UNKNOWN_ORIENTATION) is False


def test_a_vertical_upload_with_an_unreadable_duration_is_still_short_form():
    """Orientation alone settles it — no need to also know the length."""
    assert is_short_form(("garbage", "720", "1280")) is True


def test_an_unreadable_duration_with_no_orientation_is_unknown():
    assert is_short_form(("garbage", None, None)) is None


def test_vertical_uploads_do_not_count_toward_the_longform_floor():
    """count_longform feeds a MINIMUM, so a vertical video must not satisfy it."""
    assert count_longform([PORTRAIT] * 30) == 0
    assert count_longform([LANDSCAPE] * 30) == 30


def test_a_vertical_only_channel_reads_as_shorts_only():
    assert is_shorts_only([PORTRAIT] * 10) is True
    assert is_shorts_only([LANDSCAPE] * 10) is False


def test_one_unjudgeable_video_still_blocks_a_shorts_only_verdict():
    """Unchanged asymmetry: that verdict discards the channel outright."""
    assert is_shorts_only([PORTRAIT, ("garbage", None, None)]) is False


def test_video_shape_reads_the_player_block():
    item = {
        "contentDetails": {"duration": "PT3H52M54S"},
        "player": {"embedWidth": "720", "embedHeight": "1280"},
    }
    assert video_shape(item) == ("PT3H52M54S", "720", "1280")


def test_video_shape_tolerates_a_missing_player_block():
    """The part was requested but a given item came back without it — unknown,
    not vertical."""
    item = {"contentDetails": {"duration": "PT20M"}}
    assert video_shape(item) == ("PT20M", None, None)
    assert is_short_form(video_shape(item)) is False


# --- duplicate uploads ------------------------------------------------------


def test_a_reupload_of_the_same_video_counts_once():
    kept = drop_duplicate_uploads([
        ("a", "MYSTERY DOORS CHALLANGE IN BEAMNG (Portrait)", 13974),
        ("b", "MYSTERY DOORS CHALLANGE IN BEAMNG", 13973),
    ])
    assert kept == ["a"], "the first occurrence survives"


def test_the_newest_copy_is_the_one_kept():
    """
    Caller feeds these newest-first and has already dropped short-form cuts, so
    first-seen is the newest long-form copy. Keeping the BEST-PERFORMING copy
    instead would restore exactly the flattery this removes.
    """
    kept = drop_duplicate_uploads([
        ("newest", "Same Video", 600),
        ("older", "Same Video", 600),
    ])
    assert kept == ["newest"]


def test_different_videos_of_the_same_length_are_not_merged():
    """Length alone must never merge two uploads — the titles differ."""
    kept = drop_duplicate_uploads([
        ("a", "Episode One", 600),
        ("b", "Episode Two", 600),
    ])
    assert kept == ["a", "b"]


def test_the_same_title_at_a_very_different_length_is_not_merged():
    """A short recap and the full video share a title but are distinct uploads."""
    kept = drop_duplicate_uploads([
        ("full", "Big Race", 3600),
        ("recap", "Big Race", 300),
    ])
    assert kept == ["full", "recap"]


def test_a_re_encode_within_the_tolerance_is_merged():
    """A re-encode is not frame-exact; the live pair differed by 1s and by 6s."""
    inside = DUPLICATE_DURATION_TOLERANCE_SECONDS
    kept = drop_duplicate_uploads([
        ("a", "Same Video", 1000),
        ("b", "Same Video", 1000 + inside),
        ("c", "Same Video", 1000 + inside + 1),
    ])
    assert kept == ["a", "c"]


def test_an_untitled_upload_is_kept_rather_than_guessed_at():
    """Unknown never excludes — the rule the rest of the module follows."""
    kept = drop_duplicate_uploads([
        ("a", "", 600),
        ("b", "", 600),
    ])
    assert kept == ["a", "b"]


@pytest.mark.parametrize("title,expected", [
    ("MYSTERY DOORS (Portrait)", "mystery doors"),
    ("MYSTERY DOORS [4K]", "mystery doors"),
    ("Mystery  Doors!!!", "mystery doors"),
    ("Mystery Doors 🔥", "mystery doors"),
])
def test_title_normalisation_strips_decoration_not_words(title, expected):
    assert normalize_video_title(title) == expected


def test_normalisation_strips_only_a_TRAILING_suffix():
    """A bracketed phrase mid-title is part of the name, not a cut marker."""
    assert normalize_video_title("Ep (Part 2) Finale") == "ep part 2 finale"


# --- the two working together, on the shape of the live channel -------------


def test_the_live_channel_shape_no_longer_produces_a_passing_sample(monkeypatch):
    """
    Ten uploads: five real videos, each posted twice. The vertical cuts carry
    the views. After both fixes the sample is the five LANDSCAPE cuts — which
    is what the audience actually watched — not a 50/50 mix that clears the
    per-video floor.
    """
    import main

    pairs = []
    for i in range(5):
        pairs.append((f"v{i}p", f"Race {i} (Portrait)", 3600 + i, 30_000, ("720", "1280")))
        pairs.append((f"v{i}l", f"Race {i}", 3600 + i, 200, ("720", "405")))

    longform = [
        (vid, title, secs, views) for vid, title, secs, views, (w, h) in pairs
        if is_short_form((f"PT{secs}S", w, h)) is False
    ]
    kept = drop_duplicate_uploads((v, t, s) for v, t, s, _ in longform)
    views = [v for vid, _, _, v in longform if vid in kept]

    assert len(views) == 5, "five distinct videos, not ten"
    assert views == [200] * 5, "the watched-by-nobody landscape cuts are the real ones"
    assert main.pre_push_drop_reason(
        subscriber_count=68_700, avg_views=sum(views) / len(views),
        min_avg_views=10_000, video_count=1134, content_language="en",
        settled_views=views,
    ) == "below_view_minimum"
