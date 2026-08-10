from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    page.goto("https://www.example.com")
    print(page.title())
    browser.close()

    page.wait_for_timeout(5000)  # Wait for 5 seconds before closing the browser
    print(page.title())  # Print the title of the page after waiting
    browser.close()  # Close the browser after waiting