"""The browser step must fail soft and reuse one session."""


class _FakePage:
    def __init__(self, text):
        self._text = text

    def goto(self, url, **kwargs):
        self.url = url

    def locator(self, selector):
        return self

    def inner_text(self, timeout=None):
        return self._text

    def close(self):
        pass


class _FakeBrowser:
    def __init__(self, text="", fail=False):
        self._text = text
        self._fail = fail
        self.pages_created = 0
        self.closed = False

    def new_page(self):
        self.pages_created += 1
        if self._fail:
            raise RuntimeError("browser exploded")
        return _FakePage(self._text)

    def close(self):
        self.closed = True


def test_extracts_email_from_about_text():
    from browser_email import BrowserEmailScraper

    browser = _FakeBrowser("Business enquiries: hello@creator.com")
    scraper = BrowserEmailScraper(browser=browser)
    assert scraper.find_email("UC123") == "hello@creator.com"


def test_returns_empty_when_no_email_present():
    from browser_email import BrowserEmailScraper

    scraper = BrowserEmailScraper(browser=_FakeBrowser("no contact details here"))
    assert scraper.find_email("UC123") == ""


def test_browser_failure_is_soft():
    """A browser error must never break the pipeline."""
    from browser_email import BrowserEmailScraper

    scraper = BrowserEmailScraper(browser=_FakeBrowser(fail=True))
    assert scraper.find_email("UC123") == ""


def test_one_session_serves_many_channels():
    """Regression: the backfill launched a browser per channel."""
    from browser_email import BrowserEmailScraper

    browser = _FakeBrowser("a@b.com")
    scraper = BrowserEmailScraper(browser=browser)
    for i in range(5):
        scraper.find_email(f"UC{i}")

    assert browser.pages_created == 5  # five pages...
    assert not browser.closed          # ...but the browser stayed open


def test_null_scraper_is_inert():
    from browser_email import null_scraper

    assert null_scraper().find_email("UC123") == ""


def test_resolve_email_prefers_free_steps_over_browser():
    """The browser must only run when both free steps found nothing."""
    import main
    from browser_email import BrowserEmailScraper

    browser = _FakeBrowser("browser@found.com")
    scraper = BrowserEmailScraper(browser=browser)

    stats = {"business_email": "about@page.com", "channel_id": "UC1"}
    performance = {"repeated_email": ""}
    assert main.resolve_email(stats, performance, scraper) == "about@page.com"
    assert browser.pages_created == 0


def test_resolve_email_falls_through_to_browser():
    import main
    from browser_email import BrowserEmailScraper

    browser = _FakeBrowser("browser@found.com")
    scraper = BrowserEmailScraper(browser=browser)

    stats = {"business_email": "", "channel_id": "UC1"}
    performance = {"repeated_email": ""}
    assert main.resolve_email(stats, performance, scraper) == "browser@found.com"
