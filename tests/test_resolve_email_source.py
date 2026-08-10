"""
Which step of the email chain produced an address.

backfill_missing_emails.py reports coverage by step, and it used to infer
the step by comparing the resolved email back against stats/performance —
"not repeated_email and not business_email, therefore the browser". That
inference silently became wrong the moment a third source (the older-
uploads scan) was added between them: both it and the browser land in the
same else branch. The chain reports its own source instead.
"""


def _stats(**overrides):
    stats = {"channel_id": "UC1", "business_email": "", "uploads_playlist_id": "PL1"}
    stats.update(overrides)
    return stats


def _performance(**overrides):
    performance = {"repeated_email": "", "next_page_token": "", "video_descriptions": []}
    performance.update(overrides)
    return performance


class _Browser:
    def __init__(self, email=""):
        self._email = email
        self.calls = 0

    def find_email(self, channel_id):
        self.calls += 1
        return self._email


def test_repeated_recent_videos_is_labelled(monkeypatch):
    import main

    email, source = main.resolve_email_with_source(
        _stats(), _performance(repeated_email="a@b.com"), None,
    )
    assert (email, source) == ("a@b.com", main.EMAIL_SOURCE_REPEATED)


def test_about_description_is_labelled(monkeypatch):
    import main

    email, source = main.resolve_email_with_source(
        _stats(business_email="a@b.com"), _performance(), None,
    )
    assert (email, source) == ("a@b.com", main.EMAIL_SOURCE_ABOUT)


def test_older_uploads_scan_is_labelled(monkeypatch):
    """The case the old comparison-based inference got wrong."""
    import main

    monkeypatch.setattr(main, "scan_older_videos_for_email", lambda *a, **k: "a@b.com")

    email, source = main.resolve_email_with_source(
        _stats(), _performance(next_page_token="T2"), _Browser("browser@b.com"),
    )
    assert (email, source) == ("a@b.com", main.EMAIL_SOURCE_OLDER)


def test_browser_is_labelled(monkeypatch):
    import main

    monkeypatch.setattr(main, "scan_older_videos_for_email", lambda *a, **k: "")

    email, source = main.resolve_email_with_source(
        _stats(), _performance(), _Browser("browser@b.com"),
    )
    assert (email, source) == ("browser@b.com", main.EMAIL_SOURCE_BROWSER)


def test_nothing_found_reports_no_source(monkeypatch):
    import main

    monkeypatch.setattr(main, "scan_older_videos_for_email", lambda *a, **k: "")

    assert main.resolve_email_with_source(_stats(), _performance(), _Browser("")) == ("", "")


def test_every_source_label_is_distinct():
    """Two steps sharing a label would silently merge in the summary."""
    import main

    labels = [
        main.EMAIL_SOURCE_REPEATED,
        main.EMAIL_SOURCE_ABOUT,
        main.EMAIL_SOURCE_OLDER,
        main.EMAIL_SOURCE_BROWSER,
    ]
    assert len(set(labels)) == len(labels)
    assert all(labels)


def test_resolve_email_returns_the_same_address(monkeypatch):
    """resolve_email stays the plain-string entry point the pipeline uses."""
    import main

    monkeypatch.setattr(main, "scan_older_videos_for_email", lambda *a, **k: "older@b.com")

    stats, performance = _stats(), _performance(next_page_token="T2")
    assert main.resolve_email(stats, performance, None) == "older@b.com"
    assert main.resolve_email(stats, performance, None) == \
        main.resolve_email_with_source(stats, performance, None)[0]
