"""
Small Playwright + stealth smoke test for checking a target page with a
Chromium binary.

This script is intentionally separate from the YouTube vetting pipeline:
it lets you test a site with Playwright without changing the data
collection code.

Examples:
    python cloakbrowser_test.py --url https://example.com
    python cloakbrowser_test.py --url https://bot.incolumitas.com --headed

If you need a proxy, pass --proxy on the command line.
"""
import argparse
import sys

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a simple Playwright + stealth page smoke test")
    parser.add_argument("--url", required=True, help="Page to open in Playwright")
    parser.add_argument("--headed", action="store_true", help="Show the browser window")
    parser.add_argument("--proxy", default=None, help="Proxy URL, for example http://user:pass@host:port")
    parser.add_argument("--timeout-ms", type=int, default=30000, help="Navigation timeout in milliseconds")
    args = parser.parse_args()

    with sync_playwright() as playwright:
        launch_kwargs = {"headless": not args.headed}
        if args.proxy:
            launch_kwargs["proxy"] = {"server": args.proxy}

        browser = playwright.chromium.launch(**launch_kwargs)
        context = browser.new_context()
        Stealth().apply_stealth_sync(context)
        try:
            page = context.new_page()
            page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            page.wait_for_load_state("domcontentloaded", timeout=args.timeout_ms)
            print(f"Title: {page.title()}")
            print(f"URL: {page.url}")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    sys.exit(main())