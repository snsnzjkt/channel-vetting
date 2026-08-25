"""
Step 3 of the email chain: scan OLDER uploads for a contact email.

The newest-50 window (EMAIL_SCAN_SAMPLE_SIZE) misses any channel that
stopped printing its contact address in recent descriptions — common on
channels that pivoted to Shorts, where descriptions are terse. Paging
deeper into the uploads playlist costs 2 quota units per extra page
(playlistItems.list + videos.list) and only runs when the two free steps
found nothing, so a channel whose email is already known costs nothing.

No network: every YouTube call is monkeypatched.
"""


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = ""

    def json(self):
        return self._payload


def _playlist_payload(count, next_page_token=None):
    payload = {
        "items": [
            {"contentDetails": {"videoId": f"v{i}", "videoPublishedAt": "2026-01-01T00:00:00Z"}}
            for i in range(count)
        ]
    }
    if next_page_token:
        payload["nextPageToken"] = next_page_token
    return payload


def _videos_payload(descriptions):
    return {
        "items": [
            {"id": f"v{i}", "snippet": {"description": d}, "statistics": {}}
            for i, d in enumerate(descriptions)
        ]
    }


class _FakeApi:
    """
    Routes playlistItems.list / videos.list to canned pages.

    `pages` is a list of (playlist_payload, videos_payload) tuples, one per
    extra page the scan is allowed to fetch.
    """

    def __init__(self, pages):
        self._pages = list(pages)
        self.playlist_calls = 0
        self.video_calls = 0
        self.page_tokens = []

    def get(self, url, params=None, timeout=None):
        params = params or {}
        if "/playlistItems" in url:
            self.page_tokens.append(params.get("pageToken"))
            if self.playlist_calls >= len(self._pages):
                raise AssertionError("scan fetched more pages than the test allowed")
            payload = self._pages[self.playlist_calls][0]
            self.playlist_calls += 1
            return _Resp(200, payload)
        if "/videos" in url:
            payload = self._pages[self.video_calls][1]
            self.video_calls += 1
            return _Resp(200, payload)
        raise AssertionError(f"unexpected URL {url}")


def _patch_enrichment(monkeypatch, api, spend=None):
    from channel_vetting.enrichment import channels

    # The shared session object, not the `requests` module: enrichment calls
    # HTTP.get() (http_client.YOUTUBE). Patching enrichment.requests would
    # leave the real network live and trip the block_real_http guard.
    monkeypatch.setattr(channels.HTTP, "get", api.get)
    monkeypatch.setattr(channels.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(
        channels, "record_spend",
        lambda units, call_name="": spend.append(units) if spend is not None else None,
    )


# --- the repeat threshold spans both windows ------------------------------

def test_repeats_split_across_pages_clear_the_threshold(monkeypatch):
    """Two recent mentions + one older mention = 3, which qualifies.

    The older descriptions are added to the ones already scanned rather
    than counted on their own, so a channel that mentions its address
    twice recently isn't thrown away when the third mention is older.
    """
    from channel_vetting.enrichment import channels

    api = _FakeApi([(_playlist_payload(2), _videos_payload(["reach me at hi@creator.com"]))])
    _patch_enrichment(monkeypatch, api)

    email = channels.scan_older_videos_for_email(
        "UC1", "PL1", "TOKEN2",
        known_descriptions=["email: hi@creator.com", "email: hi@creator.com"],
        max_pages=1,
    )
    assert email == "hi@creator.com"


def test_single_mention_in_older_videos_is_not_enough(monkeypatch):
    """One mention anywhere is still weak evidence — the same bar as step 1."""
    from channel_vetting.enrichment import channels

    api = _FakeApi([(_playlist_payload(2), _videos_payload(["one-off: hi@creator.com"]))])
    _patch_enrichment(monkeypatch, api)

    assert channels.scan_older_videos_for_email(
        "UC1", "PL1", "TOKEN2", known_descriptions=["nothing here"], max_pages=1,
    ) == ""


# --- quota discipline -----------------------------------------------------

def test_no_quota_spent_without_a_page_token(monkeypatch):
    """No nextPageToken means the first page was the whole playlist."""
    from channel_vetting.enrichment import channels

    api = _FakeApi([])
    spend = []
    _patch_enrichment(monkeypatch, api, spend)

    assert channels.scan_older_videos_for_email(
        "UC1", "PL1", "", known_descriptions=["x"], max_pages=2,
    ) == ""
    assert api.playlist_calls == 0
    assert spend == []


def test_each_extra_page_costs_exactly_two_units(monkeypatch):
    """playlistItems.list (1) + videos.list (1) per page, nothing more."""
    from channel_vetting.enrichment import channels

    api = _FakeApi([
        (_playlist_payload(2, next_page_token="TOKEN3"), _videos_payload(["no email"])),
        (_playlist_payload(2), _videos_payload(["still no email"])),
    ])
    spend = []
    _patch_enrichment(monkeypatch, api, spend)

    channels.scan_older_videos_for_email(
        "UC1", "PL1", "TOKEN2", known_descriptions=["nope"], max_pages=2,
    )
    assert sum(spend) == 4
    assert api.playlist_calls == 2


def test_stops_at_max_pages(monkeypatch):
    """A long back catalogue must not page forever."""
    from channel_vetting.enrichment import channels

    api = _FakeApi([
        (_playlist_payload(2, next_page_token="TOKEN3"), _videos_payload(["no email"])),
    ])
    _patch_enrichment(monkeypatch, api)

    # _FakeApi raises if a second page is requested.
    assert channels.scan_older_videos_for_email(
        "UC1", "PL1", "TOKEN2", known_descriptions=["nope"], max_pages=1,
    ) == ""


def test_stops_paging_once_an_email_is_found(monkeypatch):
    """Don't pay for page 3 when page 2 already answered the question."""
    from channel_vetting.enrichment import channels

    api = _FakeApi([
        (_playlist_payload(2, next_page_token="TOKEN3"),
         _videos_payload(["hi@creator.com", "hi@creator.com", "hi@creator.com"])),
    ])
    _patch_enrichment(monkeypatch, api)

    assert channels.scan_older_videos_for_email(
        "UC1", "PL1", "TOKEN2", known_descriptions=[], max_pages=3,
    ) == "hi@creator.com"
    assert api.playlist_calls == 1


def test_walks_forward_through_page_tokens(monkeypatch):
    """Page 3 must be requested with page 2's nextPageToken, not page 1's."""
    from channel_vetting.enrichment import channels

    api = _FakeApi([
        (_playlist_payload(2, next_page_token="TOKEN3"), _videos_payload(["no email"])),
        (_playlist_payload(2), _videos_payload(["no email"])),
    ])
    _patch_enrichment(monkeypatch, api)

    channels.scan_older_videos_for_email(
        "UC1", "PL1", "TOKEN2", known_descriptions=[], max_pages=2,
    )
    assert api.page_tokens == ["TOKEN2", "TOKEN3"]


# --- failure is soft ------------------------------------------------------

def test_api_error_returns_empty_rather_than_raising(monkeypatch):
    """A failed page must never break a pipeline run."""
    from channel_vetting.enrichment import channels

    monkeypatch.setattr(channels.HTTP, "get", lambda *a, **k: _Resp(500))
    monkeypatch.setattr(channels.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(channels, "record_spend", lambda *a, **k: None)

    assert channels.scan_older_videos_for_email(
        "UC1", "PL1", "TOKEN2", known_descriptions=[], max_pages=2,
    ) == ""


def test_network_error_returns_empty_rather_than_raising(monkeypatch):
    """A dead network must not end the run either.

    The retry adapter has already given up by the time RequestException
    reaches this function, so there is nothing left to do but skip the
    optional deep scan — this step is a bonus on top of two free ones.
    """
    import requests

    from channel_vetting.enrichment import channels

    def boom(*a, **k):
        raise requests.ConnectionError("no route to host")

    monkeypatch.setattr(channels.HTTP, "get", boom)
    monkeypatch.setattr(channels.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(channels, "record_spend", lambda *a, **k: None)

    assert channels.scan_older_videos_for_email(
        "UC1", "PL1", "TOKEN2", known_descriptions=[], max_pages=2,
    ) == ""


def test_missing_uploads_playlist_returns_empty(monkeypatch):
    from channel_vetting.enrichment import channels

    def boom(*a, **k):
        raise AssertionError("must not call the API without an uploads playlist")

    monkeypatch.setattr(channels.HTTP, "get", boom)

    assert channels.scan_older_videos_for_email(
        "UC1", "", "TOKEN2", known_descriptions=[], max_pages=2,
    ) == ""


# --- the token has to reach the scan at all ------------------------------

def test_performance_result_carries_the_next_page_token(monkeypatch):
    """get_recent_video_performance must hand the token forward.

    Without this the scan would have to re-fetch page 1 (2 wasted units)
    just to learn where the newest-50 window ended.
    """
    from channel_vetting.enrichment import channels

    api = _FakeApi([
        (_playlist_payload(3, next_page_token="TOKEN2"),
         _videos_payload(["a", "b", "c"])),
    ])
    _patch_enrichment(monkeypatch, api)

    result = channels.get_recent_video_performance("UC1", "PL1")
    assert result["next_page_token"] == "TOKEN2"


# --- position in the chain ------------------------------------------------

def test_resolve_email_deep_scans_before_the_browser(monkeypatch):
    """Two free-ish quota units beat launching a browser."""
    from channel_vetting import pipeline

    class _Browser:
        def __init__(self):
            self.email_calls = 0
            self.link_list_calls = 0

        def find_contact(self, channel_id, need_email=True):
            if need_email:
                self.email_calls += 1
                return "browser@found.com", True
            # Link-list-only mode: no address, just the presence flag.
            self.link_list_calls += 1
            return "", True

        def find_email(self, channel_id):
            return self.find_contact(channel_id)[0]

    browser = _Browser()
    monkeypatch.setattr(
        pipeline, "scan_older_videos_for_email",
        lambda *a, **k: "older@creator.com",
    )

    stats = {"channel_id": "UC1", "business_email": "", "uploads_playlist_id": "PL1"}
    performance = {"repeated_email": "", "next_page_token": "TOKEN2", "video_descriptions": []}

    assert pipeline.resolve_email(stats, performance, browser) == "older@creator.com"
    # The expensive part — following links for an address — must not run.
    assert browser.email_calls == 0
    # The cheap part — one About page for the link list — still does, because
    # the no-social drop needs it. See resolve_email_with_source.
    assert browser.link_list_calls == 1


def test_resolve_email_skips_deep_scan_when_a_free_step_answered(monkeypatch):
    """Steps 1-2 cost nothing; step 3 must not run when they succeed."""
    from channel_vetting import pipeline

    def boom(*a, **k):
        raise AssertionError("deep scan must not run when a free step found an email")

    monkeypatch.setattr(pipeline, "scan_older_videos_for_email", boom)

    stats = {"channel_id": "UC1", "business_email": "about@page.com", "uploads_playlist_id": "PL1"}
    performance = {"repeated_email": "", "next_page_token": "TOKEN2", "video_descriptions": []}

    assert pipeline.resolve_email(stats, performance, None) == "about@page.com"


def test_resolve_email_passes_the_already_scanned_descriptions(monkeypatch):
    """The scan needs page 1's descriptions to count repeats across windows."""
    from channel_vetting import pipeline

    seen = {}

    def fake_scan(channel_id, uploads_playlist_id, page_token, known_descriptions, **kwargs):
        seen.update(
            channel_id=channel_id,
            uploads_playlist_id=uploads_playlist_id,
            page_token=page_token,
            known=list(known_descriptions),
        )
        return ""

    monkeypatch.setattr(pipeline, "scan_older_videos_for_email", fake_scan)

    stats = {"channel_id": "UC1", "business_email": "", "uploads_playlist_id": "PL1"}
    performance = {
        "repeated_email": "",
        "next_page_token": "TOKEN2",
        "video_descriptions": ["d1", "d2"],
    }
    pipeline.resolve_email(stats, performance, None)

    assert seen == {
        "channel_id": "UC1",
        "uploads_playlist_id": "PL1",
        "page_token": "TOKEN2",
        "known": ["d1", "d2"],
    }
