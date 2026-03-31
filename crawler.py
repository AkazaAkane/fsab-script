import os
import re
import sys
import time
import json
import random
import base64
import subprocess
from pathlib import Path
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

BASE_URL = "https://asc.fasb.org"
HOME_URL = f"{BASE_URL}/Home"
DOWNLOAD_DIR = Path(__file__).parent / "fasb_pdfs"
CHROME_USER_DATA = Path(__file__).parent / "chrome_profile"
PROGRESS_FILE = Path(__file__).parent / "progress.json"
DEBUG_PORT = 9222

MIN_DELAY = 1.5
MAX_DELAY = 3.5

SKIP_ITEMS = {"Search", "Logout", "Tools"}
SKIP_PATTERNS = ["filter_none", "Show All in One Page"]


def find_chrome():
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def launch_chrome():
    chrome_path = find_chrome()
    if not chrome_path:
        print("ERROR: Could not find Chrome.")
        sys.exit(1)
    CHROME_USER_DATA.mkdir(exist_ok=True)
    print(f"Launching Chrome: {chrome_path}")
    proc = subprocess.Popen([
        chrome_path,
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={CHROME_USER_DATA}",
        "--no-first-run", "--no-default-browser-check",
        HOME_URL,
    ])
    time.sleep(3)
    return proc


def load_progress():
    if PROGRESS_FILE.exists():
        return set(json.loads(PROGRESS_FILE.read_text()))
    return set()


def save_progress(visited):
    PROGRESS_FILE.write_text(json.dumps(sorted(visited)))


def find_pdf_elements(page):
    """Find all PDF-link divs on the current page.

    The FASB site uses <div class="pdf-link" id="GUID-xxx.pdf"> elements
    that are clickable (not <a> tags). Each has a text label and a PDF icon.
    """
    page.wait_for_timeout(2000)
    pdf_divs = page.locator("div.pdf-link")
    count = pdf_divs.count()
    results = []
    seen_ids = set()
    for i in range(count):
        el = pdf_divs.nth(i)
        pdf_id = (el.get_attribute("id") or "").strip()
        text = clean_text(el.inner_text())
        if pdf_id and pdf_id not in seen_ids:
            seen_ids.add(pdf_id)
            results.append({"index": i, "id": pdf_id, "text": text})
    return results


def find_publication_links(page):
    """Find unique fasb-asc-publication links on the current page.

    These are <a> links pointing to other FASB pages that may contain PDFs.
    """
    links = page.evaluate("""() => {
        const anchors = document.querySelectorAll('a[href*="fasb-asc-publication"]');
        const seen = new Set();
        const results = [];
        for (const a of anchors) {
            const href = a.getAttribute('href').split('#')[0];
            if (!seen.has(href)) {
                seen.add(href);
                results.push({href: href, text: (a.innerText || '').trim().substring(0, 80)});
            }
        }
        return results;
    }""")
    return links


def download_pdf_by_click(page, pdf_div_index, dest_path):
    """Click a div.pdf-link element and save the PDF from the new tab.

    Clicking opens a new tab at getPdf?fileName=GUID-xxx.pdf which serves the PDF.
    We use CDP to capture the response body from the new tab.
    """
    pages_before = set(id(p) for p in page.context.pages)

    try:
        pdf_div = page.locator("div.pdf-link").nth(pdf_div_index)

        with page.context.expect_page(timeout=15000) as new_page_info:
            pdf_div.click(timeout=5000)

        new_page = new_page_info.value
        new_page.wait_for_load_state("domcontentloaded", timeout=30000)
        pdf_url = new_page.url

        if "getPdf" in pdf_url or ".pdf" in pdf_url.lower():
            cdp = page.context.new_cdp_session(new_page)
            js_result = new_page.evaluate("""async () => {
                const resp = await fetch(window.location.href);
                const buf = await resp.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let binary = '';
                for (let i = 0; i < bytes.length; i++) {
                    binary += String.fromCharCode(bytes[i]);
                }
                return btoa(binary);
            }""")
            dest_path.write_bytes(base64.b64decode(js_result))
            cdp.detach()
            new_page.close()
            return True

        new_page.close()

    except Exception as exc:
        print(f"        Download error: {exc}")
        for p in page.context.pages:
            if id(p) not in pages_before:
                p.close()

    return False


def download_all_pdfs_on_page(page, breadcrumb_label, total_pdfs, indent=""):
    """Download all div.pdf-link PDFs on the current page."""
    pdf_els = find_pdf_elements(page)
    if pdf_els:
        print(f"{indent}Found {len(pdf_els)} PDF(s)")
        for pdf in pdf_els:
            fname = safe_filename(breadcrumb_label, pdf["text"], pdf["id"])
            dest = DOWNLOAD_DIR / fname
            if dest.exists():
                print(f"{indent}  Already have: {fname}")
                continue
            print(f"{indent}  Downloading: {pdf['text']} ({pdf['id']})")
            ok = download_pdf_by_click(page, pdf["index"], dest)
            print(f"{indent}  {'OK' if ok else 'FAILED'}: {fname}")
            if ok:
                total_pdfs += 1
            time.sleep(1)
    else:
        print(f"{indent}No PDFs on this page")
    return total_pdfs


def safe_filename(breadcrumb, pdf_text, pdf_url):
    parts = "_".join(breadcrumb)
    parts = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", parts)[:120].strip("_ ")
    pdf_name = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", pdf_text)[:40].strip("_ ")
    name = f"{parts}_{pdf_name}" if pdf_name else parts
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def clean_text(raw):
    return raw.strip().replace("\n", " ").replace("keyboard_arrow_right", "").strip()


def get_level_items(page, level):
    """Get clickable items for a given menu level.

    Level 0 = left-menu-container with navElement items.
    Level 2+ = menu-overlay-level-N with subNavElement items.
    """
    if level == 0:
        container = page.locator("div.left-menu-container").first
        item_sel = "div.navElement"
    else:
        container = page.locator(f"div.nav-level-overflow.nav-level-{level}").first
        item_sel = "div.subNavElement"

    if container.count() == 0:
        return []

    items = container.locator(item_sel)
    count = items.count()
    result = []
    for i in range(count):
        raw = items.nth(i).inner_text()
        text = clean_text(raw)
        if text and text not in SKIP_ITEMS and not any(p in text for p in SKIP_PATTERNS):
            result.append({"index": i, "text": text})
    return result


def go_home(page):
    try:
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
    except PwTimeout:
        try:
            page.goto(HOME_URL, wait_until="commit", timeout=15000)
        except PwTimeout:
            pass
    page.wait_for_timeout(3000)


def click_nav_path(page, path_texts):
    """Replay a sequence of nav clicks to re-open menus to a certain depth."""
    for depth, text in enumerate(path_texts):
        level = depth_to_overlay_level(depth)
        page.wait_for_timeout(1000)
        clicked = find_and_click_by_text(page, level, text)
        if not clicked:
            print(f"  Warning: could not replay click for '{text}' at level {level}")
            return False
        page.wait_for_timeout(1500)
    return True


def depth_to_overlay_level(depth):
    """Map recursion depth to the CSS overlay level number.

    depth 0 = left-menu-container (level 0, special)
    depth 1 = menu-overlay-level-2 / nav-level-2
    depth 2 = menu-overlay-level-3 / nav-level-3
    depth 3 = menu-overlay-level-4 / nav-level-4
    ...
    """
    if depth == 0:
        return 0
    return depth + 1


def find_and_click_by_text(page, level, target_text):
    """Re-query the DOM and click an item by matching its text. Returns True if clicked."""
    if level == 0:
        container = page.locator("div.left-menu-container").first
        item_sel = "div.navElement"
    else:
        container = page.locator(f"div.nav-level-overflow.nav-level-{level}").first
        item_sel = "div.subNavElement"

    if container.count() == 0:
        return False

    els = container.locator(item_sel)
    for i in range(els.count()):
        raw = els.nth(i).inner_text()
        t = clean_text(raw)
        if t == target_text:
            els.nth(i).click(timeout=10000)
            return True
    return False


def explore(page, context, visited, breadcrumb, depth, total_pdfs):
    """Recursively explore the menu tree."""
    level = depth_to_overlay_level(depth)
    indent = "  " * depth

    items = get_level_items(page, level)
    if not items:
        print(f"{indent}No items at depth={depth} (overlay level {level})")
        return total_pdfs

    item_texts = [i["text"] for i in items]
    print(f"{indent}Depth {depth} (level {level}): {len(items)} items — {[t[:40] for t in item_texts]}")

    for text in item_texts:
        path_key = " > ".join(breadcrumb + [text])

        if path_key in visited:
            print(f"{indent}  {text} — already done, skip")
            continue

        print(f"{indent}  Clicking: {text}")

        try:
            clicked = find_and_click_by_text(page, level, text)
            if not clicked:
                print(f"{indent}      Could not find '{text}' in DOM, skip")
                continue

            page.wait_for_timeout(2500)

            current_url = page.url
            if current_url != HOME_URL and "/Home" not in current_url:
                print(f"{indent}      -> Page: {current_url}")

                total_pdfs = download_all_pdfs_on_page(
                    page, breadcrumb + [text], total_pdfs, indent + "      "
                )

                pub_links = find_publication_links(page)
                if pub_links:
                    print(f"{indent}      Also found {len(pub_links)} publication link(s)")
                    for pl in pub_links:
                        pl_key = pl["href"]
                        if pl_key in visited:
                            continue
                        full_url = urljoin(BASE_URL, pl["href"])
                        print(f"{indent}        -> Following: {pl['text']}")
                        try:
                            page.goto(full_url, wait_until="domcontentloaded", timeout=30000)
                            page.wait_for_timeout(2000)
                            total_pdfs = download_all_pdfs_on_page(
                                page, breadcrumb + [text, pl["text"]],
                                total_pdfs, indent + "          "
                            )
                            visited.add(pl_key)
                            save_progress(visited)
                        except Exception as exc:
                            print(f"{indent}          Error: {exc}")
                        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

                visited.add(path_key)
                save_progress(visited)

                go_home(page)
                if breadcrumb:
                    click_nav_path(page, breadcrumb)

            else:
                next_overlay = depth_to_overlay_level(depth + 1)
                next_items = get_level_items(page, next_overlay)
                if next_items:
                    print(f"{indent}      -> Submenu opened (overlay level {next_overlay}, {len(next_items)} items)")
                    total_pdfs = explore(
                        page, context, visited,
                        breadcrumb + [text], depth + 1, total_pdfs
                    )
                else:
                    print(f"{indent}      -> No submenu (checked overlay level {next_overlay}), no navigation")
                    visited.add(path_key)
                    save_progress(visited)

        except PwTimeout:
            print(f"{indent}      Timeout on '{text}'")
        except Exception as exc:
            print(f"{indent}      Error: {exc}")

        delay = random.uniform(MIN_DELAY, MAX_DELAY)
        time.sleep(delay)

    return total_pdfs


def main():
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    print("="*60)
    print("FASB ASC PDF Crawler")
    print("="*60)
    print()
    print("STEP 1: Launch Chrome with debugging port.")
    print("  If Chrome is not already open, launching now...")

    chrome_proc = launch_chrome()

    print()
    print("STEP 2: Solve verification in the Chrome window.")
    print("  Once the FASB home page loads normally,")
    print("  press ENTER here to start crawling.")
    print()
    input(">>> Press ENTER to begin... ")

    visited = load_progress()
    if visited:
        print(f"\nResuming — {len(visited)} items already visited.")

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()

        print(f"\nConnected. URL: {page.url}  Title: {page.title()}")
        page.wait_for_timeout(3000)

        if "/Home" not in page.url:
            print("Not on Home page, navigating...")
            go_home(page)

        total_pdfs = 0
        print("\n--- Starting menu exploration ---\n")
        try:
            total_pdfs = explore(page, context, visited, [], 0, total_pdfs)
        except KeyboardInterrupt:
            print("\n\nInterrupted. Progress saved.")
        except Exception as exc:
            print(f"\nError: {exc}")
            import traceback
            traceback.print_exc()
        finally:
            save_progress(visited)
            print(f"\nVisited {len(visited)} menu items.")
            print(f"Downloaded {total_pdfs} PDFs to {DOWNLOAD_DIR}")
            browser.close()
            chrome_proc.terminate()


if __name__ == "__main__":
    main()
