"""
Small CloakBrowser smoke test for checking a target page with a stealth
Chromium binary.

This script is intentionally separate from the YouTube vetting pipeline:
it lets you test a site with CloakBrowser without changing the data
collection code.

Examples:
    python cloakbrowser_test.py --url https://example.com
    python cloakbrowser_test.py --url https://bot.incolumitas.com --headed --humanize

If you have a CloakBrowser license key, set CLOAKBROWSER_LICENSE_KEY in
your environment. If you need a proxy, pass --proxy or set it via the
command line.
"""
import argparse
import os
import sys

from cloakbrowser import launch


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a simple CloakBrowser page smoke test")
    parser.add_argument("--url", required=True, help="Page to open in CloakBrowser")
    parser.add_argument("--headed", action="store_true", help="Show the browser window")
    parser.add_argument("--humanize", action="store_true", help="Enable human-like mouse and keyboard behavior")
    parser.add_argument("--proxy", default=None, help="Proxy URL, for example http://user:pass@host:port")
    parser.add_argument("--timeout-ms", type=int, default=30000, help="Navigation timeout in milliseconds")
    args = parser.parse_args()

    launch_kwargs = {
        "headless": not args.headed,
    }
    if args.humanize:
        launch_kwargs["humanize"] = True
    if args.proxy:
        launch_kwargs["proxy"] = args.proxy

    license_key = os.getenv("CLOAKBROWSER_LICENSE_KEY")
    if license_key:
        launch_kwargs["license_key"] = license_key

    browser = launch(**launch_kwargs)
    try:
        page = browser.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout_ms)
        page.wait_for_load_state("networkidle", timeout=args.timeout_ms)
        print(f"Title: {page.title()}")
        print(f"URL: {page.url}")
    finally:
        browser.close()


if __name__ == "__main__":
    sys.exit(main())