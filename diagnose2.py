# Connect to running Chrome, click General Principles, and dump ALL elements
# inside menu-overlay-level-2 to see what's there.
import time
from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as pw:
        print("Connecting to Chrome on port 9222 ...")
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = context.pages[0]

        print(f"URL: {page.url}")

        # Make sure we're on home
        if "/Home" not in page.url:
            page.goto("https://asc.fasb.org/Home", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)

        # Click General Principles
        container = page.locator("div.left-menu-container").first
        nav_els = container.locator("div.navElement")
        print(f"navElement count: {nav_els.count()}")

        # Find General Principles
        for i in range(nav_els.count()):
            t = nav_els.nth(i).inner_text().strip().replace("\n", " ")
            if "General Principles" in t:
                print(f"Clicking navElement[{i}]: '{t}'")
                nav_els.nth(i).click(timeout=5000)
                break

        # Wait various amounts of time and check
        for wait in [1, 2, 3, 5]:
            page.wait_for_timeout(wait * 1000)
            print(f"\n--- After {wait}s total ---")
            print(f"URL: {page.url}")

            # Check overlay level 2
            ol2 = page.locator("div.menu-overlay-level-2")
            print(f"menu-overlay-level-2 count: {ol2.count()}")
            if ol2.count() > 0:
                print(f"  visible: {ol2.first.is_visible()}")
                print(f"  children: {ol2.first.locator('> *').count()}")
                inner = ol2.first.inner_text().strip()[:200]
                print(f"  text: {inner}")

            # Check nav-level-overflow
            for lvl in range(6):
                nlo = page.locator(f"div.nav-level-overflow.nav-level-{lvl}")
                if nlo.count() > 0:
                    print(f"  nav-level-overflow.nav-level-{lvl}: count={nlo.count()} visible={nlo.first.is_visible()}")
                    sub = nlo.first.locator("div.subNavElement")
                    print(f"    subNavElement count: {sub.count()}")
                    for j in range(min(sub.count(), 5)):
                        print(f"      [{j}] {sub.nth(j).inner_text().strip()[:60]}")

            # Also check: any subNavElement anywhere
            all_sub = page.locator("div.subNavElement")
            print(f"  ALL subNavElement on page: {all_sub.count()}")

            # Check what classes exist inside overlay-2
            if ol2.count() > 0:
                inner_classes = page.evaluate("""() => {
                    const ol = document.querySelector('.menu-overlay-level-2');
                    if (!ol) return [];
                    return Array.from(ol.querySelectorAll('*')).slice(0, 30).map(e => ({
                        tag: e.tagName,
                        cls: (e.className || '').toString().substring(0, 100),
                        text: (e.innerText || '').substring(0, 60).replace(/\\n/g, ' ')
                    }));
                }""")
                print(f"  Elements inside overlay-2:")
                for ic in inner_classes:
                    print(f"    <{ic['tag']}> class='{ic['cls']}' text='{ic['text']}'")

        browser.close()
        print("\nDone.")


if __name__ == "__main__":
    main()
