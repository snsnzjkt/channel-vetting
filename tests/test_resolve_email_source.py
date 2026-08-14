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
    def __init__(self, email="", has_links=None):
        self._email = email
        # An email implies the link list wasn't empty; default the flag to
        # match, so a stub configured with just an address behaves like the
        # real scraper (which returns True alongside a found address).
        self._has_links = has_links if has_links is not None else bool(email) or None
        self.calls = 0

    def find_contact(self, channel_id, need_email=True):
        self.calls += 1
        # need_email=False is the link-list-only mode the chain uses once an
        # earlier step has the address: the real scraper returns no email in
        # that mode, and mirroring it here is what stops a stub from making an
        # earlier-step hit look like a step-5 hit.
        self.need_email_calls = getattr(self, "need_email_calls", []) + [need_email]
        return (self._email if need_email else ""), self._has_links

    def find_email(self, channel_id):
        return self.find_contact(channel_id)[0]


def test_repeated_recent_videos_is_labelled(monkeypatch):
    import main

    email, source, _ = main.resolve_email_with_source(
        _stats(), _performance(repeated_email="a@b.com"), None,
    )
    assert (email, source) == ("a@b.com", main.EMAIL_SOURCE_REPEATED)


def test_about_description_is_labelled(monkeypatch):
    import main

    email, source, _ = main.resolve_email_with_source(
        _stats(business_email="a@b.com"), _performance(), None,
    )
    assert (email, source) == ("a@b.com", main.EMAIL_SOURCE_ABOUT)


def test_older_uploads_scan_is_labelled(monkeypatch):
    """The case the old comparison-based inference got wrong."""
    import main

    monkeypatch.setattr(main, "scan_older_videos_for_email", lambda *a, **k: "a@b.com")

    email, source, _ = main.resolve_email_with_source(
        _stats(), _performance(next_page_token="T2"), _Browser("browser@b.com"),
    )
    assert (email, source) == ("a@b.com", main.EMAIL_SOURCE_OLDER)


def test_browser_is_labelled(monkeypatch):
    import main

    monkeypatch.setattr(main, "scan_older_videos_for_email", lambda *a, **k: "")

    email, source, has_links = main.resolve_email_with_source(
        _stats(), _performance(), _Browser("browser@b.com"),
    )
    assert (email, source) == ("browser@b.com", main.EMAIL_SOURCE_BROWSER)
    # A browser hit implies the link list was non-empty.
    assert has_links is True


def test_nothing_found_reports_no_source(monkeypatch):
    import main

    monkeypatch.setattr(main, "scan_older_videos_for_email", lambda *a, **k: "")

    # _Browser("") models a scraper that read the link list but found no
    # email; with no has_links override it reports None (unknown presence).
    assert main.resolve_email_with_source(_stats(), _performance(), _Browser("")) == ("", "", None)


def test_empty_link_list_surfaces_as_false(monkeypatch):
    """
    A browser that read the About panel and saw NO links reports
    has_external_links=False — the signal process_candidate turns into the
    no-social drop. Distinct from None (list never read).
    """
    import main

    monkeypatch.setattr(main, "scan_older_videos_for_email", lambda *a, **k: "")

    result = main.resolve_email_with_source(
        _stats(), _performance(), _Browser("", has_links=False),
    )
    assert result == ("", "", False)


def test_every_source_label_is_distinct():
    """Two steps sharing a label would silently merge in the summary."""
    import main

    labels = [
        main.EMAIL_SOURCE_REPEATED,
        main.EMAIL_SOURCE_ABOUT,
        main.EMAIL_SOURCE_OLDER,
        main.EMAIL_SOURCE_INFLUENCERS,
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
