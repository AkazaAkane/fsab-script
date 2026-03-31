# Check an Accounting Standards Update page for PDFs
from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        while len(context.pages) > 1:
            context.pages[-1].close()
        page = context.pages[0]

        url = "https://asc.fasb.org/1943274/1856122/fasb-asc-publication/accounting-standards-update-no-2018-09%E2%80%94codification-improvements"
        print(f"Visiting: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        print(f"URL: {page.url}")
        print(f"Title: {page.title()}")

        # pdf-link divs
        pdf_divs = page.locator("div.pdf-link")
        print(f"\npdf-link divs: {pdf_divs.count()}")
        for i in range(pdf_divs.count()):
            pid = pdf_divs.nth(i).get_attribute("id") or ""
            text = pdf_divs.nth(i).inner_text().strip()[:60]
            print(f"  [{i}] id={pid} text={text}")

        # any pdf/download elements
        pdf_els = page.evaluate("""() => {
            const all = document.querySelectorAll('[class*="pdf"], [class*="download"], [id*=".pdf"]');
            return Array.from(all).slice(0, 20).map(el => ({
                tag: el.tagName,
                cls: (el.className || '').substring(0, 80),
                id: el.id || '',
                text: (el.innerText || '').substring(0, 60)
            }));
        }""")
        print(f"\nPDF/download elements: {len(pdf_els)}")
        for el in pdf_els:
            print(f"  <{el['tag']}> class='{el['cls']}' id='{el['id']}' text='{el['text']}'")

        # xref links on this page (do they chain further?)
        xrefs = page.evaluate("""() => {
            return Array.from(document.querySelectorAll('a[href*="fasb-asc-publication"]')).slice(0, 10).map(a => ({
                href: a.getAttribute('href'),
                text: (a.innerText || '').trim().substring(0, 60)
            }));
        }""")
        print(f"\nfasb-asc-publication links: {len(xrefs)}")
        for x in xrefs:
            print(f"  {x['text']}: {x['href'][:80]}")

        browser.close()
        print("\nDone.")


if __name__ == "__main__":
    main()
