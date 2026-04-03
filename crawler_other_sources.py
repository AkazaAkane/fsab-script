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
DOWNLOAD_DIR = Path(__file__).parent / "fasb_other_sources"
CHROME_USER_DATA = Path(__file__).parent / "chrome_profile"
PROGRESS_FILE = Path(__file__).parent / "progress_other_sources.json"
DEBUG_PORT = 9222

MIN_DELAY = 1.5
MAX_DELAY = 3.5

SKIP_ITEMS = {"Search", "Logout", "Tools"}
SKIP_PATTERNS = ["filter_none", "Show All in One Page"]
SKIP_TABS = {"Pre-Codification Standards", "Concept Statements"}


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


def sanitize_folder_name(name):
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", name).strip("_ .")
    name = re.sub(r"_+", "_", name)
    return name[:120] or "unknown"


def safe_filename(pdf_text, pdf_id):
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", pdf_text)[:100].strip("_ ")
    if not name:
        name = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", pdf_id)[:60].strip("_ ")
    if not name:
        name = "download"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


def clean_text(raw):
    return raw.strip().replace("\n", " ").replace("keyboard_arrow_right", "").strip()


def find_pdf_elements(page):
    """Find all div.pdf-link elements on the current page."""
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


def find_all_pdf_links(page):
    """Find all <a> tags that point to PDFs."""
    return page.evaluate("""() => {
        const results = [];
        const seen = new Set();
        for (const a of document.querySelectorAll('a[href]')) {
            const href = a.getAttribute('href') || '';
            const text = (a.innerText || '').trim();
            if (!href || seen.has(href)) continue;
            if (href.toLowerCase().endsWith('.pdf') || href.includes('getPdf')) {
                seen.add(href);
                results.push({href: href, text: text.substring(0, 120)});
            }
        }
        return results;
    }""")


def find_all_links(page):
    """Find all navigable <a> links on the page (non-PDF, non-anchor)."""
    return page.evaluate("""() => {
        const results = [];
        const seen = new Set();
        for (const a of document.querySelectorAll('a[href]')) {
            const href = a.getAttribute('href') || '';
            const text = (a.innerText || '').trim();
            if (!href || !text || seen.has(href)) continue;
            if (href.startsWith('#') || href.startsWith('javascript:')) continue;
            if (href.toLowerCase().endsWith('.pdf') || href.includes('getPdf')) continue;
            seen.add(href);
            results.push({href: href, text: text.substring(0, 120)});
        }
        return results;
    }""")


def download_pdf_by_click(page, pdf_div_index, dest_path):
    """Click a div.pdf-link element and capture the PDF from the new tab."""
    pages_before = set(id(p) for p in page.context.pages)
    try:
        pdf_div = page.locator("div.pdf-link").nth(pdf_div_index)
        with page.context.expect_page(timeout=15000) as new_page_info:
            pdf_div.click(timeout=5000)
        new_page = new_page_info.value
        new_page.wait_for_load_state("domcontentloaded", timeout=30000)
        pdf_url = new_page.url
        if "getPdf" in pdf_url or ".pdf" in pdf_url.lower():
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
            new_page.close()
            return True
        new_page.close()
    except Exception as exc:
        print(f"        Download error: {exc}")
        for p in page.context.pages:
            if id(p) not in pages_before:
                p.close()
    return False


def download_pdf_from_url(page, url, dest_path):
    """Download a PDF from a direct URL using in-browser fetch."""
    try:
        full_url = url if url.startswith("http") else urljoin(BASE_URL, url)
        js_result = page.evaluate("""async (url) => {
            const resp = await fetch(url);
            if (!resp.ok) return null;
            const buf = await resp.arrayBuffer();
            const bytes = new Uint8Array(buf);
            let binary = '';
            for (let i = 0; i < bytes.length; i++) {
                binary += String.fromCharCode(bytes[i]);
            }
            return btoa(binary);
        }""", full_url)
        if js_result:
            dest_path.write_bytes(base64.b64decode(js_result))
            return True
    except Exception as exc:
        print(f"        PDF URL download error: {exc}")
    return False


def download_all_pdfs(page, folder, visited, indent=""):
    """Download every PDF on the current page (div.pdf-link + <a> PDF links).

    Returns the number of PDFs successfully downloaded.
    """
    count = 0

    pdf_els = find_pdf_elements(page)
    if pdf_els:
        print(f"{indent}Found {len(pdf_els)} div.pdf-link PDF(s)")
        for pdf in pdf_els:
            link_key = f"pdf-div:{pdf['id']}"
            if link_key in visited:
                continue
            fname = safe_filename(pdf["text"], pdf["id"])
            dest = folder / fname
            if dest.exists():
                print(f"{indent}  Already have: {fname}")
                visited.add(link_key)
                continue
            print(f"{indent}  Downloading: {pdf['text']}")
            ok = download_pdf_by_click(page, pdf["index"], dest)
            print(f"{indent}  {'OK' if ok else 'FAILED'}: {fname}")
            if ok:
                count += 1
            visited.add(link_key)
            time.sleep(1)

    pdf_links = find_all_pdf_links(page)
    if pdf_links:
        print(f"{indent}Found {len(pdf_links)} <a> PDF link(s)")
        for pl in pdf_links:
            link_key = f"pdf-url:{pl['href']}"
            if link_key in visited:
                continue
            fname = safe_filename(pl["text"], pl["href"].split("/")[-1])
            dest = folder / fname
            if dest.exists():
                print(f"{indent}  Already have: {fname}")
                visited.add(link_key)
                continue
            print(f"{indent}  Downloading: {pl['text'][:80]}")
            ok = download_pdf_from_url(page, pl["href"], dest)
            print(f"{indent}  {'OK' if ok else 'FAILED'}: {fname}")
            if ok:
                count += 1
            visited.add(link_key)
            time.sleep(1)

    if not pdf_els and not pdf_links:
        print(f"{indent}No PDFs on this page")

    return count


def process_page(page, folder, visited, breadcrumb, total_pdfs, depth=0, max_depth=5, indent=""):
    """Process a page: download all PDFs, then follow every link and recurse.

    Creates sub-folders for each followed link, named after the link text.
    """
    current_url = page.url
    page_key = f"page:{current_url}"

    if page_key in visited:
        print(f"{indent}Already processed page: {current_url}")
        return total_pdfs

    print(f"{indent}Processing page: {current_url}")
    folder.mkdir(parents=True, exist_ok=True)

    total_pdfs += download_all_pdfs(page, folder, visited, indent + "  ")
    save_progress(visited)

    if depth >= max_depth:
        print(f"{indent}  Max depth reached, not following sub-links")
        visited.add(page_key)
        save_progress(visited)
        return total_pdfs

    sub_links = find_all_links(page)
    if sub_links:
        print(f"{indent}  Found {len(sub_links)} sub-link(s) to follow")
        for link in sub_links:
            href = link["href"]
            sub_key = f"sublink:{href}"
            if sub_key in visited:
                continue

            full_url = href if href.startswith("http") else urljoin(BASE_URL, href)
            if not full_url.startswith(BASE_URL):
                visited.add(sub_key)
                continue

            sub_folder_name = sanitize_folder_name(link["text"])
            sub_folder = folder / sub_folder_name
            print(f"{indent}  Following: {link['text'][:80]}")

            try:
                page.goto(full_url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(2000)

                total_pdfs = process_page(
                    page, sub_folder, visited,
                    breadcrumb + [link["text"][:60]],
                    total_pdfs, depth + 1, max_depth, indent + "    "
                )
            except Exception as exc:
                print(f"{indent}    Error: {exc}")

            visited.add(sub_key)
            save_progress(visited)
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    visited.add(page_key)
    save_progress(visited)
    return total_pdfs


def go_home(page):
    try:
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
    except PwTimeout:
        try:
            page.goto(HOME_URL, wait_until="commit", timeout=15000)
        except PwTimeout:
            pass
    page.wait_for_timeout(3000)


def depth_to_overlay_level(depth):
    if depth == 0:
        return 0
    return depth + 1


def get_all_menu_items(page, level):
    """Get all menu items at a level, only filtering out SKIP_ITEMS and SKIP_PATTERNS."""
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
        result.append({"index": i, "text": text})
    return result


def find_and_click_by_text(page, level, target_text):
    """Click a menu item by matching its text."""
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


def explore_other_sources(page, visited, breadcrumb, depth, total_pdfs):
    """Recursively explore menus under Other Sources, downloading all PDFs.

    At depth 1 (the tabs inside Other Sources), skips SKIP_TABS.
    At all other depths, iterates through everything.
    """
    level = depth_to_overlay_level(depth)
    indent = "  " * depth

    items = get_all_menu_items(page, level)
    if not items:
        print(f"{indent}No items at depth={depth} (level {level})")
        return total_pdfs

    if depth == 1:
        items = [i for i in items if i["text"] not in SKIP_TABS]

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
                print(f"{indent}      -> Navigated to: {current_url}")

                folder_name = sanitize_folder_name(text)
                page_folder = DOWNLOAD_DIR
                for bc in breadcrumb[1:]:
                    page_folder = page_folder / sanitize_folder_name(bc)
                page_folder = page_folder / folder_name
                page_folder.mkdir(parents=True, exist_ok=True)

                total_pdfs = process_page(
                    page, page_folder, visited,
                    breadcrumb + [text], total_pdfs,
                    depth=0, max_depth=5, indent=indent + "      "
                )

                visited.add(path_key)
                save_progress(visited)

                go_home(page)
                if breadcrumb:
                    click_nav_path(page, breadcrumb)
            else:
                next_overlay = depth_to_overlay_level(depth + 1)
                next_items = get_all_menu_items(page, next_overlay)
                if next_items:
                    print(f"{indent}      -> Submenu opened ({len(next_items)} items), drilling deeper")
                    total_pdfs = explore_other_sources(
                        page, visited,
                        breadcrumb + [text], depth + 1, total_pdfs
                    )
                else:
                    print(f"{indent}      -> No submenu, no navigation, skip")
                    visited.add(path_key)
                    save_progress(visited)

        except PwTimeout:
            print(f"{indent}      Timeout on '{text}'")
        except Exception as exc:
            print(f"{indent}      Error: {exc}")

        time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

    return total_pdfs


def main():
    DOWNLOAD_DIR.mkdir(exist_ok=True)

    print("=" * 60)
    print("FASB Other Sources PDF Crawler")
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

        print("\n--- Clicking 'Other Sources' ---\n")
        clicked = find_and_click_by_text(page, 0, "Other Sources")
        if not clicked:
            print("ERROR: Could not find 'Other Sources' in the left menu.")
            browser.close()
            chrome_proc.terminate()
            return

        page.wait_for_timeout(2000)

        total_pdfs = 0
        try:
            total_pdfs = explore_other_sources(
                page, visited, ["Other Sources"], 1, total_pdfs
            )
        except KeyboardInterrupt:
            print("\n\nInterrupted. Progress saved.")
        except Exception as exc:
            print(f"\nError: {exc}")
            import traceback
            traceback.print_exc()
        finally:
            save_progress(visited)
            print(f"\nVisited {len(visited)} items.")
            print(f"Downloaded {total_pdfs} PDFs to {DOWNLOAD_DIR}")
            browser.close()
            chrome_proc.terminate()


if __name__ == "__main__":
    main()
