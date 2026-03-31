# Diagnostic: connect to an already-running Chrome and inspect the FASB page.
# Launch Chrome with --remote-debugging-port=9222 first, solve verification,
# then run this script.
import time
from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as pw:
        print("Connecting to Chrome on port 9222 ...")
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = context.pages[0]

        print(f"URL: {page.url}")
        print(f"Title: {page.title()}")
        page.wait_for_timeout(2000)

        print("\n=== All menu/nav/overlay divs (BEFORE click) ===")
        results = page.evaluate("""() => {
            const all = document.querySelectorAll('div[class]');
            const found = [];
            for (const el of all) {
                const cls = el.className;
                if (typeof cls === 'string' &&
                    (cls.includes('menu') || cls.includes('nav') || cls.includes('overlay') || cls.includes('Nav'))) {
                    const rect = el.getBoundingClientRect();
                    const text = el.innerText ? el.innerText.substring(0, 120).replace(/\\n/g, ' | ') : '';
                    found.push({
                        cls: cls.substring(0, 150),
                        w: Math.round(rect.width),
                        h: Math.round(rect.height),
                        childCount: el.children.length,
                        text: text
                    });
                }
            }
            return found;
        }""")
        for r in results:
            print(f"  [{r['w']}x{r['h']}] class='{r['cls']}' children={r['childCount']}")
            if r['text']:
                print(f"         text: {r['text'][:100]}")

        print("\n=== navElement structure (first 5) ===")
        structure = page.evaluate("""() => {
            const navEls = document.querySelectorAll('.navElement');
            return Array.from(navEls).slice(0, 5).map((el, idx) => ({
                idx: idx,
                outerHTML: el.outerHTML.substring(0, 400),
                rect: (() => { const r = el.getBoundingClientRect(); return {w: Math.round(r.width), h: Math.round(r.height)}; })()
            }));
        }""")
        for s in structure:
            print(f"\n  [{s['idx']}] {s['rect']['w']}x{s['rect']['h']}")
            print(f"  HTML: {s['outerHTML'][:300]}")

        print("\n=== Clicking 'General Principles' ===")
        gp = page.locator("text=General Principles").first
        print(f"  Found element, clicking...")
        gp.click(timeout=5000)
        page.wait_for_timeout(3000)
        print(f"  URL after click: {page.url}")

        print("\n=== All menu/nav/overlay divs (AFTER click) ===")
        results2 = page.evaluate("""() => {
            const all = document.querySelectorAll('div[class]');
            const found = [];
            for (const el of all) {
                const cls = el.className;
                if (typeof cls === 'string' &&
                    (cls.includes('menu') || cls.includes('nav') || cls.includes('overlay') || cls.includes('Nav'))) {
                    const rect = el.getBoundingClientRect();
                    const visible = rect.width > 0 && rect.height > 0;
                    const text = el.innerText ? el.innerText.substring(0, 120).replace(/\\n/g, ' | ') : '';
                    found.push({
                        cls: cls.substring(0, 150),
                        visible: visible,
                        w: Math.round(rect.width),
                        h: Math.round(rect.height),
                        text: text
                    });
                }
            }
            return found;
        }""")
        for r in results2:
            vis = "VISIBLE" if r['visible'] else "hidden"
            print(f"  [{r['w']}x{r['h']}] {vis} class='{r['cls']}'")
            if r['visible'] and r['text']:
                print(f"         text: {r['text'][:100]}")

        # Check specifically for overlay levels
        print("\n=== Overlay levels check ===")
        for lvl in range(10):
            sel = f"div.menu-overlay-level-{lvl}"
            c = page.locator(sel).count()
            if c > 0:
                vis = page.locator(sel).first.is_visible()
                text = page.locator(sel).first.inner_text()[:100].replace("\n", " | ")
                print(f"  {sel}: count={c} visible={vis} text='{text}'")

        # Also check for any new visible content
        print("\n=== New visible navName/navElement items ===")
        new_items = page.evaluate("""() => {
            const items = document.querySelectorAll('.navName, .navElement');
            return Array.from(items).map(el => {
                const rect = el.getBoundingClientRect();
                return {
                    cls: el.className.substring(0, 80),
                    text: el.innerText.trim().substring(0, 80).replace(/\\n/g, ' '),
                    visible: rect.width > 0 && rect.height > 0,
                    x: Math.round(rect.x),
                    y: Math.round(rect.y),
                    w: Math.round(rect.width),
                    h: Math.round(rect.height)
                };
            });
        }""")
        for it in new_items:
            vis = "VIS" if it['visible'] else "hid"
            print(f"  {vis} [{it['x']},{it['y']} {it['w']}x{it['h']}] class='{it['cls']}' text='{it['text']}'")

        browser.close()
        print("\nDone.")


if __name__ == "__main__":
    main()
