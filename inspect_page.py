# Inspect the content page to find PDF links
import re
from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = context.pages[0]

        if "/1943274" not in page.url:
            page.goto("https://asc.fasb.org/1943274/2147479298",
                       wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)

        print(f"URL: {page.url}")
        print(f"Title: {page.title()}")

        anchors = page.locator("a[href]")
        print(f"\nTotal anchors: {anchors.count()}")
        for i in range(anchors.count()):
            href = anchors.nth(i).get_attribute("href") or ""
            text = anchors.nth(i).inner_text().strip()[:80]
            print(f'  [{i}] href="{href}" text="{text}"')

        buttons = page.locator("button")
        print(f"\nButtons: {buttons.count()}")
        for i in range(buttons.count()):
            text = buttons.nth(i).inner_text().strip()[:80]
            cls = buttons.nth(i).get_attribute("class") or ""
            print(f'  [{i}] class="{cls[:60]}" text="{text}"')

        body = page.eval_on_selector("body", "e => e.innerText")
        pdf_lines = [
            line.strip() for line in body.split("\n")
            if "pdf" in line.lower() or "download" in line.lower()
               or "document" in line.lower()
        ]
        print(f"\nPDF/Download/Document mentions ({len(pdf_lines)}):")
        for m in pdf_lines[:20]:
            print(f"  {m[:120]}")

        # Check for any element with "pdf" or "download" in its attributes
        pdf_els = page.evaluate("""() => {
            const all = document.querySelectorAll('*');
            const found = [];
            for (const el of all) {
                const attrs = Array.from(el.attributes || []).map(a => a.name + '=' + a.value).join(' ');
                const text = (el.innerText || '').substring(0, 60);
                if (attrs.toLowerCase().includes('pdf') || attrs.toLowerCase().includes('download')) {
                    found.push({tag: el.tagName, attrs: attrs.substring(0, 200), text: text});
                }
            }
            return found.slice(0, 30);
        }""")
        print(f"\nElements with 'pdf'/'download' in attributes ({len(pdf_els)}):")
        for el in pdf_els:
            print(f"  <{el['tag']}> {el['attrs'][:150]}")
            if el["text"]:
                print(f"    text: {el['text'][:80]}")

        browser.close()
        print("\nDone.")


if __name__ == "__main__":
    main()
