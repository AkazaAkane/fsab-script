import os
import re
import sys
import time
import json
import random
import base64
import subprocess
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

BASE_URL = "https://asc.fasb.org"
HOME_URL = f"{BASE_URL}/Home"
DOWNLOAD_DIR = Path(__file__).parent / "fasb_page_pdfs"
CHROME_USER_DATA = Path(__file__).parent / "chrome_profile"
PROGRESS_FILE = Path(__file__).parent / "progress_pages.json"
DEBUG_PORT = 9222

MIN_DELAY = 1.5
MAX_DELAY = 3.5

SKIP_ITEMS = {"Search", "Logout", "Tools"}
SKIP_PATTERNS = ["filter_none"]

TARGET_TABS = {
    "General Principles", "Presentation", "Assets", "Liabilities",
    "Equity", "Revenue", "Expenses", "Broad Transactions", "Industry",
}


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
        text = PROGRESS_FILE.read_text().strip()
        if text:
            try:
                return set(json.loads(text))
            except json.JSONDecodeError:
                pass
    return set()


def save_progress(visited):
    PROGRESS_FILE.write_text(json.dumps(sorted(visited)))


def sanitize_folder_name(name):
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", name).strip("_ .")
    name = re.sub(r"_+", "_", name)
    return name[:120] or "unknown"


def extract_topic_number(url):
    match = re.search(r"/(\d{3,4})(?:/|$)", url)
    return match.group(1) if match else None


def breadcrumb_to_folder(breadcrumb, page_url=""):
    folder = DOWNLOAD_DIR
    if not breadcrumb:
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    folder = folder / sanitize_folder_name(breadcrumb[0])

    topic = extract_topic_number(page_url) if page_url else None
    if topic:
        folder = folder / topic
    elif len(breadcrumb) > 1:
        topic_match = re.match(r"(\d{3,4})\s", breadcrumb[1])
        if topic_match:
            folder = folder / topic_match.group(1)
        else:
            folder = folder / sanitize_folder_name(breadcrumb[1])

    folder.mkdir(parents=True, exist_ok=True)
    return folder


def safe_pdf_filename(breadcrumb, page_url):
    topic = extract_topic_number(page_url)
    if topic:
        name = f"Topic_{topic}"
    elif breadcrumb:
        name = sanitize_folder_name(breadcrumb[-1])
    else:
        name = "page"

    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def clean_text(raw):
    return raw.strip().replace("\n", " ").replace("keyboard_arrow_right", "").strip()


def save_using_site_print(page, dest_path, indent=""):
    """Triggers the site's built-in ngxprint button and captures the
    styled result via CDP. The site's Angular code preps the DOM with
    customPrint.css, then calls window.print() which we intercept."""
    print(f"{indent}Hooking into the browser's print function...")

    page.evaluate("""() => {
        window.__printIntercepted = false;
        window.__origPrint = window.print;
        window.print = function() {
            window.__printIntercepted = true;
        };
    }""")

    print_btn = page.locator("button[ngxprint]").filter(has_text="Print").first

    if print_btn.count() == 0:
        print(f"{indent}Could not find the site's ngxprint button, trying span fallback...")
        print_btn = page.locator("span.close-icon-image", has_text="Print").first

    if print_btn.count() == 0:
        print(f"{indent}ERROR: No Print button found at all.")
        return False

    print(f"{indent}Clicking the site's Print button...")
    print_btn.click(force=True)

    try:
        page.wait_for_function("window.__printIntercepted === true", timeout=15000)
        print(f"{indent}Site prepared the print view! Capturing PDF...")
    except Exception:
        print(f"{indent}Timed out waiting for print prep, capturing page as-is...")

    try:
        cdp = page.context.new_cdp_session(page)
        result = cdp.send("Page.printToPDF", {
            "landscape": False,
            "printBackground": True,
            "preferCSSPageSize": True,
        })

        pdf_data = base64.b64decode(result["data"])
        dest_path.write_bytes(pdf_data)
        cdp.detach()

        print(f"{indent}Saved PDF ({len(pdf_data):,} bytes): {dest_path.name}")

        page.evaluate("window.print = window.__origPrint;")
        return True

    except Exception as exc:
        print(f"{indent}Error saving PDF: {exc}")
        return False


def process_show_all_page(page, visited, breadcrumb, total_pdfs, indent=""):
    """Navigate to the showallinonepage, trigger the site's print, and save as PDF."""
    show_all_url = page.url
    print(f"{indent}Saving Show All in One Page: {show_all_url}")

    folder = breadcrumb_to_folder(breadcrumb, page.url)
    fname = safe_pdf_filename(breadcrumb, page.url)
    dest = folder / fname

    if dest.exists():
        print(f"{indent}  Already have: {fname}")
        return total_pdfs

    page.wait_for_timeout(3000)

    ok = save_using_site_print(page, dest, indent + "  ")
    if ok:
        total_pdfs += 1

    return total_pdfs


def get_level_items(page, level):
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
        if not text or text in SKIP_ITEMS or any(p in text for p in SKIP_PATTERNS):
            continue
        if level == 0 and text not in TARGET_TABS:
            continue
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
    if depth == 0:
        return 0
    return depth + 1


def find_and_click_by_text(page, level, target_text):
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


def has_show_all_in_one_page(page, level):
    if level == 0:
        container = page.locator("div.left-menu-container").first
        item_sel = "div.navElement"
    else:
        container = page.locator(f"div.nav-level-overflow.nav-level-{level}").first
        item_sel = "div.subNavElement"

    if container.count() == 0:
        return False

    items = container.locator(item_sel)
    for i in range(items.count()):
        text = clean_text(items.nth(i).inner_text())
        if "Show All in One Page" in text:
            return True
    return False


def click_show_all_in_one_page(page, level):
    if level == 0:
        container = page.locator("div.left-menu-container").first
        item_sel = "div.navElement"
    else:
        container = page.locator(f"div.nav-level-overflow.nav-level-{level}").first
        item_sel = "div.subNavElement"

    if container.count() == 0:
        return False

    items = container.locator(item_sel)
    for i in range(items.count()):
        text = clean_text(items.nth(i).inner_text())
        if "Show All in One Page" in text:
            items.nth(i).click(timeout=10000)
            return True
    return False


def explore(page, context, visited, breadcrumb, depth, total_pdfs):
    """Recursively explore the menu tree until we find 'Show All in One Page',
    then save that page as a PDF."""
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
            need_nav_reset = False

            if "showallinonepage" in current_url.lower():
                print(f"{indent}      -> Show All in One Page: {current_url}")
                total_pdfs = process_show_all_page(
                    page, visited, breadcrumb + [text], total_pdfs, indent + "      "
                )
                visited.add(path_key)
                save_progress(visited)
                need_nav_reset = True

            elif current_url != HOME_URL and "/Home" not in current_url:
                print(f"{indent}      -> Page: {current_url}")
                visited.add(path_key)
                save_progress(visited)
                need_nav_reset = True

            else:
                next_overlay = depth_to_overlay_level(depth + 1)

                if has_show_all_in_one_page(page, next_overlay):
                    print(f"{indent}      -> Found 'Show All in One Page' in submenu, clicking it")
                    click_show_all_in_one_page(page, next_overlay)
                    page.wait_for_timeout(3000)

                    if "showallinonepage" in page.url.lower():
                        total_pdfs = process_show_all_page(
                            page, visited, breadcrumb + [text], total_pdfs, indent + "      "
                        )
                    else:
                        print(f"{indent}        Unexpected URL after clicking Show All: {page.url}")

                    visited.add(path_key)
                    save_progress(visited)
                    need_nav_reset = True
                else:
                    next_items = get_level_items(page, next_overlay)
                    if next_items:
                        print(f"{indent}      -> Submenu opened ({len(next_items)} items), drilling deeper")
                        total_pdfs = explore(
                            page, context, visited,
                            breadcrumb + [text], depth + 1, total_pdfs
                        )
                    else:
                        print(f"{indent}      -> No submenu and no 'Show All in One Page', skip")
                        visited.add(path_key)
                        save_progress(visited)

            if need_nav_reset:
                go_home(page)
                if breadcrumb:
                    if not click_nav_path(page, breadcrumb):
                        print(f"{indent}      Nav replay failed, going home to retry...")
                        go_home(page)
                        if breadcrumb:
                            click_nav_path(page, breadcrumb)

        except PwTimeout:
            print(f"{indent}      Timeout on '{text}'")
        except Exception as exc:
            print(f"{indent}      Error: {exc}")

        delay = random.uniform(MIN_DELAY, MAX_DELAY)
        time.sleep(delay)

    return total_pdfs


def main():
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    print("=" * 60)
    print("FASB ASC — Save 'Show All in One Page' to PDF")
    print("=" * 60)
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
            print(f"Saved {total_pdfs} page PDFs to {DOWNLOAD_DIR}")
            browser.close()
            chrome_proc.terminate()


if __name__ == "__main__":
    main()
