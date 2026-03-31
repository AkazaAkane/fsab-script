# Test: click pdf-link, capture new tab, fetch PDF via in-page JS
import base64
from pathlib import Path
from playwright.sync_api import sync_playwright

DOWNLOAD_DIR = Path(__file__).parent / "fasb_pdfs"
DOWNLOAD_DIR.mkdir(exist_ok=True)


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = context.pages[0]

        # Close extra tabs
        while len(context.pages) > 1:
            context.pages[-1].close()

        page = context.pages[0]

        if "/1943274" not in page.url:
            page.goto("https://asc.fasb.org/1943274/2147479298",
                       wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)

        print(f"URL: {page.url}")

        pdf_divs = page.locator("div.pdf-link")
        print(f"pdf-link count: {pdf_divs.count()}")

        print("\nClicking first pdf-link...")
        with context.expect_page(timeout=15000) as new_page_info:
            pdf_divs.nth(0).click(timeout=5000)

        new_page = new_page_info.value
        new_page.wait_for_load_state("domcontentloaded", timeout=30000)
        print(f"New tab URL: {new_page.url}")

        print("Fetching PDF via in-page fetch()...")
        b64_data = new_page.evaluate("""async () => {
            const resp = await fetch(window.location.href);
            const buf = await resp.arrayBuffer();
            const bytes = new Uint8Array(buf);
            let binary = '';
            for (let i = 0; i < bytes.length; i++) {
                binary += String.fromCharCode(bytes[i]);
            }
            return btoa(binary);
        }""")

        pdf_bytes = base64.b64decode(b64_data)
        print(f"Got {len(pdf_bytes)} bytes")
        print(f"Starts with: {pdf_bytes[:20]}")

        dest = DOWNLOAD_DIR / "test_maintenance_update_2014-20.pdf"
        dest.write_bytes(pdf_bytes)
        print(f"Saved: {dest}")

        new_page.close()
        browser.close()
        print("Done.")


if __name__ == "__main__":
    main()
