"""
Enrichment can't be allowed to end a run, and can't over-report quota.

Three things pinned here, all of which used to be wrong:

1. **Network errors were fatal.** None of the five YouTube call sites caught
   `requests.RequestException`, so one ConnectionError/ReadTimeout unwound
   through `get_channel_stats()` -> `process_candidate()` ->
   `push_until_full()` -> `run_niche()` — none of which catch it — and killed
   the whole pipeline over a single unreachable channel. CLAUDE.md's
   "enrichment returns None with a logged warning for inaccessible channels"
   convention was true for HTTP statuses and false for the network.
2. **`record_spend()` ran before the status check**, so a 403 `quotaExceeded`
   — which costs Google 0 units — was billed at full price in
   `quota_log.json`, over-counting our own spend and shrinking the
   `QUOTA_CEILING` headroom left for the rest of the day.
3. **`int(stats.get("subscriberCount", 0))` assumed the key is either absent
   or numeric.** The default only applies when the key is MISSING; YouTube
   also sends it present-but-null, `.get()` hands back None, and `int(None)`
   raises TypeError with nothing to catch it.

Plus the standing rule from `http_client.py`: the API key travels as the
`X-goog-api-key` header and must never reappear in a `params` dict, because
`requests` prints the full URL in its exception messages and CI keeps those
logs for 90 days.

No network: every YouTube call is monkeypatched (and so is `record_spend`,
which otherwise writes to the real `quota_log.json`).
"""
import pytest
import requests


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _channel_payload(**statistics):
    stats = {"subscriberCount": "1000", "videoCount": "50", "viewCount": "9999"}
    stats.update(statistics)
    return {
        "items": [{
            "snippet": {
                "title": "A Channel",
                "country": "US",
                "publishedAt": "2020-01-01T00:00:00Z",
                "description": "",
                "customUrl": "@achannel",
            },
            "statistics": stats,
            "contentDetails": {"relatedPlaylists": {"uploads": "PL1"}},
        }]
    }


def _playlist_payload(count=3):
    return {
        "items": [
            {"contentDetails": {"videoId": f"v{i}", "videoPublishedAt": "2026-01-01T00:00:00Z"}}
            for i in range(count)
        ]
    }


def _videos_payload(count=3, **statistics):
    stats = {"viewCount": "500", "likeCount": "10", "commentCount": "2"}
    stats.update(statistics)
    return {
        "items": [
            {
                "id": f"v{i}",
                "snippet": {"description": ""},
                "statistics": dict(stats),
                "contentDetails": {"duration": "PT10M"},
            }
            for i in range(count)
        ]
    }


class _Router:
    """
    Stands in for HTTP.get, routing on the URL and recording every `params`
    dict it was handed (which is how the "no key in params" test reads them).

    A canned value may be a _Resp or an Exception instance — an exception is
    raised instead of returned, which is how a dead network is simulated. A
    call site left as None is one the test asserts must not be reached.
    """

    def __init__(self, channels=None, playlist=None, videos=None):
        self._channels = channels
        self._playlist = playlist
        self._videos = videos
        self.seen_params = []

    def get(self, url, params=None, timeout=None):
        self.seen_params.append(params or {})
        if "/channels" in url:
            return self._resolve(self._channels)
        if "/playlistItems" in url:
            return self._resolve(self._playlist)
        if "/videos" in url:
            return self._resolve(self._videos)
        raise AssertionError(f"unexpected URL {url}")

    @staticmethod
    def _resolve(canned):
        if canned is None:
            raise AssertionError("this call site was not expected to be reached")
        if isinstance(canned, Exception):
            raise canned
        return canned


def _patch(monkeypatch, router, spend=None):
    """Patch the shared session, the pacing sleep, and the quota log."""
    import enrichment

    # enrichment.HTTP (http_client.YOUTUBE), NOT enrichment.requests — the
    # module keeps `requests` imported only for RequestException.
    monkeypatch.setattr(enrichment.HTTP, "get", router.get)
    monkeypatch.setattr(enrichment.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(
        enrichment, "record_spend",
        lambda units, call_name="": spend.append((units, call_name)) if spend is not None else None,
    )
    return enrichment


# --- a dead network skips the candidate, it doesn't end the run -----------

def test_get_channel_stats_returns_none_on_network_error(monkeypatch):
    """The bug this whole file exists for: return None, never raise."""
    enrichment = _patch(monkeypatch, _Router(channels=requests.ConnectionError("dns failure")))

    assert enrichment.get_channel_stats("UC1") is None


def test_get_channel_stats_network_error_charges_no_quota(monkeypatch):
    """A request that never reached Google can't have cost a unit."""
    spend = []
    enrichment = _patch(monkeypatch, _Router(channels=requests.ReadTimeout("timed out")), spend)

    assert enrichment.get_channel_stats("UC1") is None
    assert spend == []


def test_performance_returns_none_when_playlist_items_is_unreachable(monkeypatch):
    enrichment = _patch(monkeypatch, _Router(playlist=requests.ConnectionError("reset by peer")))

    assert enrichment.get_recent_video_performance("UC1", "PL1") is None


def test_performance_returns_none_when_videos_list_is_unreachable(monkeypatch):
    """The second of the two calls was just as fatal as the first.

    The playlistItems unit IS still charged here: that call returned data,
    so Google billed it — only the failed call goes unbilled.
    """
    spend = []
    router = _Router(
        playlist=_Resp(200, _playlist_payload()),
        videos=requests.ReadTimeout("slow"),
    )
    enrichment = _patch(monkeypatch, router, spend)

    assert enrichment.get_recent_video_performance("UC1", "PL1") is None
    assert [units for units, _ in spend] == [enrichment.QUOTA_COST_PLAYLIST_ITEMS_LIST]


# --- record_spend only bills calls that returned data --------------------

def test_non_200_records_no_spend(monkeypatch):
    """A 403 quotaExceeded costs 0 units on Google's side — don't self-bill.

    Over-counting is not harmless: quota_log.json is what can_afford_search()
    checks against QUOTA_CEILING, so phantom spend shrinks the day's real
    discovery budget.
    """
    spend = []
    router = _Router(channels=_Resp(403, {}, text="quotaExceeded"))
    enrichment = _patch(monkeypatch, router, spend)

    assert enrichment.get_channel_stats("UC1") is None
    assert spend == []


def test_non_200_on_playlist_items_records_no_spend(monkeypatch):
    spend = []
    router = _Router(playlist=_Resp(500, {}, text="backend error"))
    enrichment = _patch(monkeypatch, router, spend)

    assert enrichment.get_recent_video_performance("UC1", "PL1") is None
    assert spend == []


def test_successful_call_still_records_spend(monkeypatch):
    """The positive control: moving the call must not silence the accounting."""
    spend = []
    enrichment = _patch(monkeypatch, _Router(channels=_Resp(200, _channel_payload())), spend)

    assert enrichment.get_channel_stats("UC1")["subscriber_count"] == 1000
    assert [units for units, _ in spend] == [enrichment.QUOTA_COST_CHANNELS_LIST]


def test_a_200_with_no_items_is_still_charged(monkeypatch):
    """Spend moved behind the STATUS check, not behind the items check.

    A 200 that simply contains no channel (private/deleted/terminated) was
    served and billed; only an error response is free.
    """
    spend = []
    enrichment = _patch(monkeypatch, _Router(channels=_Resp(200, {"items": []})), spend)

    assert enrichment.get_channel_stats("UC1") is None
    assert [units for units, _ in spend] == [enrichment.QUOTA_COST_CHANNELS_LIST]


# --- the API key is a header, never a query parameter --------------------

def test_no_call_site_sends_the_api_key_as_a_param(monkeypatch):
    """See http_client.py: a `key=` param leaks into CI logs via exceptions."""
    router = _Router(
        channels=_Resp(200, _channel_payload()),
        playlist=_Resp(200, _playlist_payload()),
        videos=_Resp(200, _videos_payload()),
    )
    enrichment = _patch(monkeypatch, router)

    enrichment.get_channel_stats("UC1")
    enrichment.get_recent_video_performance("UC1", "PL1")
    enrichment.scan_older_videos_for_email(
        "UC1", "PL1", "TOKEN2", known_descriptions=[], max_pages=1,
    )

    # Sanity check that the fake really was exercised, so the assertion below
    # can't pass by capturing nothing at all.
    assert len(router.seen_params) == 5
    offenders = [p for p in router.seen_params if "key" in p]
    assert offenders == []


def test_enrichment_does_not_import_the_api_key():
    """Nothing in this module needs the key any more — the session holds it.

    Pinned as an absence (like tests/test_qualify.py does for
    BELOW_VIEW_MINIMUM): the import coming back is the first step toward the
    param coming back.
    """
    import enrichment

    assert not hasattr(enrichment, "YOUTUBE_API_KEY")


# --- null statistics are data, not a crash ------------------------------

@pytest.mark.parametrize("value,expected", [
    ("1234", 1234),      # what the API actually sends: digits as a string
    (1234, 1234),
    (None, 0),           # key present, value null — the TypeError case
    ("", 0),
    ("n/a", 0),
    ("12.5", 0),         # int() rejects it; a default beats an exception
])
def test_as_int_never_raises(value, expected):
    from enrichment import _as_int

    assert _as_int(value) == expected


def test_as_int_honours_a_custom_default():
    from enrichment import _as_int

    assert _as_int(None, -1) == -1


def test_null_channel_statistics_do_not_crash(monkeypatch):
    router = _Router(channels=_Resp(200, _channel_payload(
        subscriberCount=None, videoCount=None, viewCount=None,
    )))
    enrichment = _patch(monkeypatch, router)

    stats = enrichment.get_channel_stats("UC1")
    assert (stats["subscriber_count"], stats["video_count"], stats["view_count"]) == (0, 0, 0)


def test_null_video_statistics_do_not_crash(monkeypatch):
    """Likes and comments are routinely disabled, and can arrive as null."""
    router = _Router(
        playlist=_Resp(200, _playlist_payload(2)),
        videos=_Resp(200, _videos_payload(2, viewCount=None, likeCount=None, commentCount=None)),
    )
    enrichment = _patch(monkeypatch, router)

    result = enrichment.get_recent_video_performance("UC1", "PL1")
    assert result["avg_views"] == 0
    assert result["avg_engagement_rate"] == 0.0


# --- A 200 is not a promise of JSON -------------------------------------
#
# `resp.json()` used to sit OUTSIDE the try/except guarding each request, so
# it looked covered but wasn't. A corporate proxy, a captive portal, or a
# Google frontend error page all return HTML with a 200, and
# requests.exceptions.JSONDecodeError (a RequestException subclass) would
# propagate straight through process_candidate() -> push_until_full() ->
# run_niche() and kill the run. do_not_contact.fetch_blocklist() already
# treats a non-JSON 200 as a failure; these pin the same defence on the
# enrichment side, where the right answer is to skip the channel, not abort.


class _NonJsonResp:
    """A 200 whose body is an HTML interstitial rather than JSON."""

    status_code = 200
    text = "<html><body>Proxy authentication required</body></html>"

    def json(self):
        raise requests.exceptions.JSONDecodeError("Expecting value", self.text, 0)


def test_channels_list_non_json_200_returns_none(monkeypatch):
    enrichment = _patch(monkeypatch, _Router(channels=_NonJsonResp()))
    assert enrichment.get_channel_stats("UC1") is None


def test_playlist_items_non_json_200_returns_none(monkeypatch):
    enrichment = _patch(monkeypatch, _Router(playlist=_NonJsonResp()))
    assert enrichment.get_recent_video_performance("UC1", "PL1") is None


def test_videos_list_non_json_200_returns_none(monkeypatch):
    """The second call in the pair — its own separate guard."""
    router = _Router(playlist=_Resp(200, _playlist_payload(2)), videos=_NonJsonResp())
    enrichment = _patch(monkeypatch, router)
    assert enrichment.get_recent_video_performance("UC1", "PL1") is None


# --- the per-video minimum feeds the "each of the last 10 > 10k" gate -----
# get_recent_video_performance already sums views into avg_views; it must
# also surface the LOWEST video in the performance window so the caller can
# gate on "every recent video passed 10k", which the average can't answer.


# Old enough for its view count to have settled (well past PERFORMANCE_MATURITY_DAYS).
_SETTLED = "2020-01-01T00:00:00Z"


def _recent_iso(days_ago):
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _videos_payload_with_views(view_counts, published_ats=None):
    """view_counts entries may be None to omit viewCount entirely (an
    unreported count). published_ats defaults every video to a settled date."""
    if published_ats is None:
        published_ats = [_SETTLED] * len(view_counts)
    items = []
    for i, (vc, pa) in enumerate(zip(view_counts, published_ats)):
        stats = {"likeCount": "1", "commentCount": "1"}
        if vc is not None:
            stats["viewCount"] = str(vc)
        items.append({
            "id": f"v{i}",
            "snippet": {"description": "", "publishedAt": pa},
            "statistics": stats,
            "contentDetails": {"duration": "PT10M"},
        })
    return {"items": items}


def test_min_views_is_the_lowest_video_in_the_performance_window(monkeypatch):
    router = _Router(
        playlist=_Resp(200, _playlist_payload(3)),
        videos=_Resp(200, _videos_payload_with_views([50_000, 12_000, 30_000])),
    )
    enrichment = _patch(monkeypatch, router)

    result = enrichment.get_recent_video_performance("UC1", "PL1")
    assert result["min_views"] == 12_000


def test_min_views_exposes_a_weak_video_the_average_hides(monkeypatch):
    router = _Router(
        playlist=_Resp(200, _playlist_payload(3)),
        videos=_Resp(200, _videos_payload_with_views([90_000, 90_000, 900])),
    )
    enrichment = _patch(monkeypatch, router)

    result = enrichment.get_recent_video_performance("UC1", "PL1")
    assert result["avg_views"] > 10_000   # the mean clears the niche floor
    assert result["min_views"] == 900     # ...but one video is well under it


def test_min_views_ignores_a_too_new_video(monkeypatch):
    """A just-posted upload is still climbing toward 10k; it must not sink the
    per-video floor. The 900-view video is 3 days old, so min skips it."""
    router = _Router(
        playlist=_Resp(200, _playlist_payload(3)),
        videos=_Resp(200, _videos_payload_with_views(
            [50_000, 30_000, 900],
            published_ats=[_SETTLED, _SETTLED, _recent_iso(3)],
        )),
    )
    enrichment = _patch(monkeypatch, router)

    result = enrichment.get_recent_video_performance("UC1", "PL1")
    assert result["min_views"] == 30_000   # the fresh 900 is excluded


def test_min_views_ignores_a_video_with_no_reported_view_count(monkeypatch):
    """An absent viewCount is unknown, not 0 — it must not force min to 0 and
    drop the channel."""
    router = _Router(
        playlist=_Resp(200, _playlist_payload(3)),
        videos=_Resp(200, _videos_payload_with_views([50_000, None, 30_000])),
    )
    enrichment = _patch(monkeypatch, router)

    result = enrichment.get_recent_video_performance("UC1", "PL1")
    assert result["min_views"] == 30_000   # the None-view video is excluded


def test_min_views_is_none_when_no_window_video_has_settled(monkeypatch):
    """A channel that just posted its whole newest window has no judgeable
    per-video views yet — min_views is None (unknown), so the floor is skipped
    rather than the channel dropped."""
    router = _Router(
        playlist=_Resp(200, _playlist_payload(2)),
        videos=_Resp(200, _videos_payload_with_views(
            [900, 1_200], published_ats=[_recent_iso(2), _recent_iso(5)],
        )),
    )
    enrichment = _patch(monkeypatch, router)

    result = enrichment.get_recent_video_performance("UC1", "PL1")
    assert result["min_views"] is None
    assert result["avg_views"] > 0   # avg still computed over the window


def test_deep_scan_non_json_200_returns_empty_string(monkeypatch):
    """scan_older_videos_for_email() fails soft to "" rather than None."""
    enrichment = _patch(monkeypatch, _Router(playlist=_NonJsonResp()))
    assert enrichment.scan_older_videos_for_email("UC1", "PL1", "tok", [], max_pages=1) == ""


def test_non_json_200_is_still_charged_quota(monkeypatch):
    """
    Google served and billed the request — the body being unusable is our
    problem, not a reason to under-report spend. Contrast the non-200 and
    network-error cases above, which record nothing.
    """
    spend = []
    enrichment = _patch(monkeypatch, _Router(channels=_NonJsonResp()), spend=spend)
    enrichment.get_channel_stats("UC1")
    assert spend == [(1, "channels.list(UC1)")]


def test_json_guard_logs_which_call_failed(monkeypatch, caplog):
    enrichment = _patch(monkeypatch, _Router(channels=_NonJsonResp()))
    with caplog.at_level("WARNING"):
        enrichment.get_channel_stats("UC1")
    assert "channels.list(UC1)" in caplog.text
    assert "non-JSON" in caplog.text


def test_json_guard_returns_payload_on_a_healthy_response():
    """Positive control: the guard is transparent when the body is fine."""
    import enrichment

    resp = _Resp(200, {"items": [{"id": "x"}]})
    assert enrichment._json_or_none(resp, "channels.list(UC1)") == {"items": [{"id": "x"}]}
