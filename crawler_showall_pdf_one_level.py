import html
import json
import os
import re
from html.parser import HTMLParser
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from playwright.sync_api import TimeoutError as PwTimeout
from playwright.sync_api import sync_playwright

BASE_URL = "https://asc.fasb.org"
HOME_URL = f"{BASE_URL}/Home"
DOWNLOAD_DIR = Path(__file__).parent / "fasb_showall_pdfs"
DEBUG_HTML_DIR = Path(__file__).parent / "fasb_showall_html"
DEBUG_JSON_DIR = Path(__file__).parent / "fasb_showall_json"
CHROME_USER_DATA = Path(__file__).parent / "chrome_profile"
PROGRESS_FILE = Path(__file__).parent / "progress_showall.json"
DEBUG_PORT = 9222

# Optional debug artifacts.
SAVE_HTML_DEBUG = False
SAVE_JSON_DEBUG = False

# If you only want to test a few PDFs first, set this to an int like 3.
MAX_SHOWALL_PDFS: Optional[int] = None

TARGET_TABS = {
    "General Principles",
    "Presentation",
    "Assets",
    "Liabilities",
    "Equity",
    "Revenue",
    "Expenses",
    "Broad Transactions",
    "Industry",
}


def find_chrome() -> Optional[str]:
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def launch_chrome() -> subprocess.Popen:
    chrome_path = find_chrome()
    if not chrome_path:
        print("ERROR: Could not find Chrome.")
        sys.exit(1)

    CHROME_USER_DATA.mkdir(exist_ok=True)
    print(f"Launching Chrome: {chrome_path}")
    proc = subprocess.Popen(
        [
            chrome_path,
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={CHROME_USER_DATA}",
            "--no-first-run",
            "--no-default-browser-check",
            HOME_URL,
        ]
    )
    time.sleep(3)
    return proc


def load_progress() -> set:
    if PROGRESS_FILE.exists():
        try:
            data = PROGRESS_FILE.read_text(encoding="utf-8").strip()
            if data:
                return set(json.loads(data))
        except (json.JSONDecodeError, ValueError):
            pass
    return set()


def save_progress(visited: set) -> None:
    PROGRESS_FILE.write_text(json.dumps(sorted(visited), indent=2), encoding="utf-8")


def sanitize_folder_name(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", name).strip("_ .")
    name = re.sub(r"_+", "_", name)
    return name[:120] or "unknown"


def safe_filename(*parts: str, ext: str = ".pdf") -> str:
    name = " - ".join(p for p in parts if p)
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', "_", name).strip("_ .")
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = "download"
    if not name.lower().endswith(ext.lower()):
        name += ext
    return name[:180]


def extract_topic_number_from_nav_path(nav_path: str) -> Optional[str]:
    match = re.match(r"(\d{3,4})", nav_path or "")
    return match.group(1) if match else None


def breadcrumb_to_folder(breadcrumb: Sequence[str], nav_path: str) -> Path:
    folder = DOWNLOAD_DIR
    if breadcrumb:
        folder = folder / sanitize_folder_name(breadcrumb[0])

    topic = extract_topic_number_from_nav_path(nav_path)
    if topic:
        folder = folder / topic
    elif len(breadcrumb) > 1:
        folder = folder / sanitize_folder_name(breadcrumb[1])

    folder.mkdir(parents=True, exist_ok=True)
    return folder


def go_home(page) -> None:
    try:
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
    except PwTimeout:
        try:
            page.goto(HOME_URL, wait_until="commit", timeout=15000)
        except PwTimeout:
            pass
    page.wait_for_timeout(3000)


def wait_for_left_navigation(page, timeout_ms: int = 60000) -> List[dict]:
    start = time.time()
    while (time.time() - start) * 1000 < timeout_ms:
        raw = page.evaluate("() => localStorage.getItem('leftNavigationMenu')")
        if raw:
            try:
                data = json.loads(raw)
                if isinstance(data, list) and data:
                    return data
            except json.JSONDecodeError:
                pass
        page.wait_for_timeout(1000)
    raise RuntimeError("leftNavigationMenu was not available in localStorage. Make sure the FASB home page fully loaded after login/verification.")


def parse_item_link(item_link: str) -> Tuple[str, str]:
    parts = (item_link or "").strip("/").split("/", 1)
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"Unexpected itemLink format: {item_link!r}")
    return parts[0], parts[1]


def iter_showall_nodes(nodes: Sequence[dict], breadcrumb: Optional[List[str]] = None) -> Iterable[Tuple[dict, List[str]]]:
    """
    Yield only the first codification topic node under each top-level tab.

    Example:
      keep   -> 105/showallinonepage
      skip   -> 105/10/showallinonepage

    The previous logic relied on afterShowAllInOnePageRequired, but that flag can
    appear deeper in the tree, which causes traversal to keep descending into
    subtopics. Here we stop at the first numeric navPath segment like ``105``.
    """
    breadcrumb = breadcrumb or []
    for node in nodes:
        item = (node.get("item") or "").strip()
        if not item:
            continue

        # Root level: only enter the target top tabs.
        if not breadcrumb:
            if item not in TARGET_TABS:
                continue
            if node.get("itemData"):
                yield from iter_showall_nodes(node["itemData"], [item])
            continue

        new_breadcrumb = breadcrumb + [item]
        has_children = bool(node.get("itemData"))
        nav_path = (node.get("navPath") or "").strip("/")

        # Stop at the first ASC topic node (e.g. 105, 205, 3100).
        # Do not recurse into 105/10, 105/20, etc.
        if has_children and re.fullmatch(r"\d{3,4}", nav_path):
            yield node, new_breadcrumb
            continue

        if has_children:
            yield from iter_showall_nodes(node["itemData"], new_breadcrumb)


def collect_showall_headers(node: dict) -> List[dict]:
    headers: List[dict] = []

    def walk(children: Sequence[dict]) -> None:
        for child in children or []:
            child_has_children = bool(child.get("itemData"))

            if child_has_children:
                walk(child["itemData"])
                continue

            item_link = child.get("itemLink")
            if not item_link:
                continue

            publication_id, page_ids = parse_item_link(item_link)
            headers.append(
                {
                    "publicationId": publication_id,
                    "pageIds": page_ids,
                    "navPath": child.get("navPath", ""),
                    "pageName": child.get("item", ""),
                }
            )

    walk(node.get("itemData") or [])

    # Fallback for unexpected menu variants.
    if not headers:
        def walk_fallback(children: Sequence[dict]) -> None:
            for child in children or []:
                if child.get("itemData"):
                    walk_fallback(child["itemData"])
                    continue
                if child.get("itemLink"):
                    publication_id, page_ids = parse_item_link(child["itemLink"])
                    headers.append(
                        {
                            "publicationId": publication_id,
                            "pageIds": page_ids,
                            "navPath": child.get("navPath", ""),
                            "pageName": child.get("item", ""),
                        }
                    )
        walk_fallback(node.get("itemData") or [])

    return headers


class VisibleHtmlFilter(HTMLParser):
    HIDDEN_CLASSES = {"hide-content", "sfragment-data", "print-content-hide", "annotationWithinContent"}
    HIDDEN_TAGS = {"script", "style", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: List[str] = []
        self.skip_stack: List[str] = []

    def _is_hidden(self, tag: str, attrs: Sequence[Tuple[str, Optional[str]]]) -> bool:
        if tag.lower() in self.HIDDEN_TAGS:
            return True
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        classes = set(attr_map.get("class", "").split())
        if classes & self.HIDDEN_CLASSES:
            return True
        style = attr_map.get("style", "").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            return True
        aria_hidden = attr_map.get("aria-hidden", "").lower()
        if aria_hidden == "true":
            return True
        return False

    def _render_attrs(self, attrs: Sequence[Tuple[str, Optional[str]]]) -> str:
        rendered = []
        for key, value in attrs:
            if value is None:
                rendered.append(key)
            else:
                rendered.append(f'{key}="{html.escape(value, quote=True)}"')
        return (" " + " ".join(rendered)) if rendered else ""

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if self.skip_stack:
            self.skip_stack.append(tag)
            return
        if self._is_hidden(tag, attrs):
            self.skip_stack.append(tag)
            return
        self.parts.append(f"<{tag}{self._render_attrs(attrs)}>")

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if self.skip_stack or self._is_hidden(tag, attrs):
            return
        self.parts.append(f"<{tag}{self._render_attrs(attrs)}/>")

    def handle_endtag(self, tag: str) -> None:
        if self.skip_stack:
            if self.skip_stack[-1] == tag:
                self.skip_stack.pop()
            elif tag in self.skip_stack:
                while self.skip_stack:
                    popped = self.skip_stack.pop()
                    if popped == tag:
                        break
            return
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.skip_stack:
            self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        if not self.skip_stack:
            self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if not self.skip_stack:
            self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        if not self.skip_stack:
            self.parts.append(f"<!--{data}-->")

    def get_html(self) -> str:
        return "".join(self.parts)


def remove_hidden_elements(fragment: str) -> str:
    parser = VisibleHtmlFilter()
    parser.feed(fragment or "")
    parser.close()
    return parser.get_html()


def normalize_visible_text_line(line: str) -> str:
    line = html.unescape(line or "").replace("\xa0", " ")
    line = re.sub(r"\s+", " ", line)
    return line.strip()


def html_fragment_to_lines(fragment: str) -> List[str]:
    text = fragment or ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|section|article|li|tr|table|h1|h2|h3|h4|h5|h6|blockquote)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text.splitlines()


def collapse_repeated_date_lines(lines: Sequence[str]) -> Tuple[List[str], bool]:
    cleaned: List[str] = []
    changed = False
    i = 0
    date_re = re.compile(r"\d{4}-\d{2}-\d{2}$")

    while i < len(lines):
        current = normalize_visible_text_line(lines[i])
        if not current:
            i += 1
            continue

        if date_re.fullmatch(current):
            j = i + 1
            while j < len(lines) and normalize_visible_text_line(lines[j]) == current:
                j += 1
            cleaned.append(current)
            if j - i > 1:
                changed = True
            i = j
            continue

        cleaned.append(current)
        i += 1

    return cleaned, changed


def simplify_fragment_if_needed(fragment: str) -> str:
    lines = html_fragment_to_lines(fragment)
    cleaned_lines, changed = collapse_repeated_date_lines(lines)
    if not changed:
        return fragment

    return '<div class="cleaned-fragment">' + '<br/>'.join(
        html.escape(line) for line in cleaned_lines if line
    ) + '</div>'


def absoluteize_fragment(fragment: str) -> str:
    if not fragment:
        return ""

    frag = re.sub(r"<script\b.*?</script>", "", fragment, flags=re.I | re.S)
    frag = re.sub(r"\s_ngcontent-[^=\s>]+(?:=\"[^\"]*\")?", "", frag)
    frag = re.sub(r"\s_nghost-[^=\s>]+(?:=\"[^\"]*\")?", "", frag)
    frag = remove_hidden_elements(frag)

    # Convert relative URLs to absolute URLs.
    frag = re.sub(
        r'(?i)(href|src)=("|\')/(?!/)',
        lambda m: f'{m.group(1)}={m.group(2)}{BASE_URL}/',
        frag,
    )
    frag = re.sub(
        r'(?i)(href|src)=("|\')(assets/)',
        lambda m: f'{m.group(1)}={m.group(2)}{BASE_URL}/assets/',
        frag,
    )
    frag = simplify_fragment_if_needed(frag)
    return frag


def extract_heading_text(raw_heading) -> str:
    if not raw_heading:
        return ""
    if isinstance(raw_heading, str):
        return raw_heading.strip()
    if isinstance(raw_heading, dict):
        for key in ("topicTitleName", "headingName", "title", "name"):
            value = raw_heading.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    return str(raw_heading).strip()


def page_titles_html(para_title: Sequence[Optional[str]]) -> str:
    parts: List[str] = []
    seen_titles = set()
    for item in para_title or []:
        title_text = extract_heading_text(item)
        if not title_text or title_text in seen_titles:
            continue
        seen_titles.add(title_text)
        parts.append(f'<div class="para-title">{html.escape(title_text)}</div>')
    return "".join(parts)


def render_showall_html(breadcrumb: Sequence[str], node: dict, headers: Sequence[dict], response_data: Dict[str, dict]) -> str:
    doc_title = " > ".join(breadcrumb)
    nav_path = node.get("navPath") or ""
    body_parts: List[str] = []
    missing_navpaths: List[str] = []

    SKIP_TITLES = {"GAAP Taxonomy Elements"}

    for header in headers:
        nav = header["navPath"]
        page_data = response_data.get(nav)
        if not page_data:
            missing_navpaths.append(nav)
            continue

        page_title = page_data.get("contentPageTitle") or header.get("pageName") or nav

        if any(skip in page_title for skip in SKIP_TITLES):
            continue
        section_parts = [
            '<section class="page-section">',
            f'<h2 class="page-title">{html.escape(page_title)}</h2>',
        ]

        formatted = page_data.get("formattedResponse") or []
        if formatted:
            for group in formatted:
                heading_name = extract_heading_text(group.get("headingName"))
                if heading_name:
                    section_parts.append(f'<h3 class="heading-name">{html.escape(heading_name)}</h3>')

                for para in group.get("paragraphContent") or []:
                    para_num = (para.get("paraNum") or "").strip()
                    title_html = page_titles_html(para.get("paraTitle") or [])
                    para_content = absoluteize_fragment(para.get("paraContent") or "")
                    section_parts.append(
                        "<div class=\"para-row\">"
                        f'<div class="para-num">{html.escape(para_num)}</div>'
                        f'<div class="para-body">{title_html}{para_content}</div>'
                        "</div>"
                    )
        else:
            fallback_bits = []
            if page_data.get("topicBodyContent"):
                fallback_bits.append(absoluteize_fragment(str(page_data["topicBodyContent"])))
            if page_data.get("otherSourcePara"):
                fallback_bits.append(absoluteize_fragment(str(page_data["otherSourcePara"])))
            if fallback_bits:
                section_parts.append('<div class="page-fallback">' + "\n".join(fallback_bits) + "</div>")
            else:
                section_parts.append('<div class="page-fallback empty">No formatted content returned for this section.</div>')

        section_parts.append("</section>")
        body_parts.append("\n".join(section_parts))

    missing_html = ""
    if missing_navpaths:
        items = "".join(f"<li>{html.escape(x)}</li>" for x in missing_navpaths)
        missing_html = (
            '<section class="diagnostic">'
            '<h2>Missing sections in response</h2>'
            f'<ul>{items}</ul>'
            '</section>'
        )

    style = f"""
    <style>
      @page {{ size: Letter; margin: 0.55in; }}
      html, body {{ margin: 0; padding: 0; }}
      body {{
        font-family: Arial, Helvetica, sans-serif;
        font-size: 11px;
        line-height: 1.42;
        color: #111;
        -webkit-print-color-adjust: exact;
        print-color-adjust: exact;
      }}
      .doc-header {{ margin-bottom: 20px; border-bottom: 2px solid #222; padding-bottom: 10px; }}
      .doc-kicker {{ font-size: 10px; color: #555; text-transform: uppercase; letter-spacing: 0.06em; }}
      .doc-title {{ font-size: 22px; line-height: 1.2; margin: 6px 0; }}
      .doc-subtitle {{ font-size: 12px; color: #444; }}
      .page-section {{ page-break-inside: avoid; margin-bottom: 20px; }}
      .page-title {{ font-size: 16px; margin: 18px 0 8px 0; border-top: 1px solid #bbb; padding-top: 10px; }}
      .heading-name {{ font-size: 13px; margin: 12px 0 6px 0; color: #222; }}
      .para-row {{ display: grid; grid-template-columns: 108px 1fr; column-gap: 14px; margin: 6px 0 10px 0; break-inside: avoid; }}
      .para-num {{ font-weight: 700; color: #222; }}
      .para-body {{ min-width: 0; }}
      .para-title {{ font-weight: 700; margin-bottom: 4px; }}
      .page-fallback.empty {{ color: #900; }}
      .diagnostic {{ margin-top: 24px; padding-top: 12px; border-top: 1px dashed #999; }}
      p {{ margin: 0 0 0.7em 0; }}
      table {{ border-collapse: collapse; width: 100%; margin: 8px 0; table-layout: auto; }}
      th, td {{ border: 1px solid #111; padding: 4px 6px; vertical-align: top; }}
      ul, ol {{ margin: 0.35em 0 0.8em 1.3em; }}
      ol.ol-norm {{ list-style: none; padding-left: 1.6em; }}
      .li-norm {{ position: relative; }}
      .linum {{ position: absolute; left: -1.6em; min-width: 1.4em; }}
      .linum::after {{ content: "."; }}
      img, svg {{ max-width: 100%; height: auto; }}
      a {{ color: #000; text-decoration: none; }}
      .term, .xref-range, .displayInline {{ display: inline; }}
      .topic-table {{ width: 100%; }}
      .printShow, .contentShow {{ display: block !important; }}
      .annotationWithinContent, .print-content-hide, .hide-content, .sfragment-data, button, .btn, .whatsNew-image {{ display: none !important; }}
    </style>
    """

    return f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <title>{html.escape(nav_path or doc_title)}</title>
  {style}
</head>
<body>
  <header class=\"doc-header\">
    <div class=\"doc-kicker\">FASB ASC Show All Export</div>
    <h1 class=\"doc-title\">{html.escape(doc_title)}</h1>
    <div class=\"doc-subtitle\">{html.escape(nav_path)}</div>
  </header>
  {''.join(body_parts)}
</body>
</html>
"""


def fetch_json_via_page(page, url: str, payload) -> object:
    text = page.evaluate(
        """async ({ url, payload }) => {
            const resp = await fetch(url, {
                method: 'POST',
                credentials: 'include',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const txt = await resp.text();
            if (!resp.ok) {
                throw new Error(`HTTP ${resp.status} ${resp.statusText}: ${txt.slice(0, 500)}`);
            }
            return txt;
        }""",
        {"url": url, "payload": payload},
    )
    return json.loads(text)


def fetch_showall_payload(page, node: dict, headers: Sequence[dict]) -> Dict[str, dict]:
    payload = {
        "showAllPagePath": node.get("navPath", ""),
        "allPagesHeaderList": list(headers),
        "includeSharedSubtopic": False,
    }
    return fetch_json_via_page(page, f"{BASE_URL}/Publications/ShowAllPages?allPagesHeaders", payload)


def fetch_pending_merge_info(page, headers: Sequence[dict]):
    try:
        return fetch_json_via_page(
            page,
            f"{BASE_URL}/Publications/pendingContentMergeListforShowAllinOnePage?pageData=",
            list(headers),
        )
    except Exception:
        return None


def write_debug_artifacts(folder: Path, stem: str, html_text: str, response_data: dict) -> None:
    if SAVE_HTML_DEBUG:
        DEBUG_HTML_DIR.mkdir(parents=True, exist_ok=True)
        (DEBUG_HTML_DIR / safe_filename(stem, ext=".html")).write_text(html_text, encoding="utf-8")
    if SAVE_JSON_DEBUG:
        DEBUG_JSON_DIR.mkdir(parents=True, exist_ok=True)
        (DEBUG_JSON_DIR / safe_filename(stem, ext=".json")).write_text(
            json.dumps(response_data, indent=2), encoding="utf-8"
        )


def print_html_to_pdf(chrome_path: str, html_path: Path, pdf_path: Path) -> None:
    html_uri = html_path.resolve().as_uri()
    cmd = [
        chrome_path,
        "--headless=new",
        "--disable-gpu",
        "--allow-file-access-from-files",
        "--run-all-compositor-stages-before-draw",
        f"--print-to-pdf={str(pdf_path.resolve())}",
        "--print-to-pdf-no-header",
        html_uri,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            "Headless Chrome PDF generation failed.\n"
            f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
        )


def export_showall_node(page, chrome_path: str, node: dict, breadcrumb: Sequence[str]) -> Optional[Path]:
    nav_path = node.get("navPath") or ""
    headers = collect_showall_headers(node)
    if not headers:
        print(f"    No page headers found for {nav_path} ({' > '.join(breadcrumb)})")
        return None

    print(f"    Fetching show-all content for {nav_path} with {len(headers)} page(s)")
    showall_data = fetch_showall_payload(page, node, headers)
    pending_merge = fetch_pending_merge_info(page, headers)
    if pending_merge not in (None, False):
        print(f"    Note: pending merge response for {nav_path}: {str(pending_merge)[:200]}")

    html_text = render_showall_html(breadcrumb, node, headers, showall_data)
    folder = breadcrumb_to_folder(breadcrumb, nav_path)
    stem = safe_filename(nav_path, breadcrumb[-1], ext="")
    html_path = folder / safe_filename(nav_path, breadcrumb[-1], ext=".html")
    pdf_path = folder / safe_filename(nav_path, breadcrumb[-1], ext=".pdf")

    html_path.write_text(html_text, encoding="utf-8")
    write_debug_artifacts(folder, stem, html_text, showall_data)

    print(f"    Saved HTML -> {html_path.relative_to(DOWNLOAD_DIR)}")
    print(f"    Rendering PDF -> {pdf_path.relative_to(DOWNLOAD_DIR)}")
    print_html_to_pdf(chrome_path, html_path, pdf_path)

    return pdf_path


def main() -> None:
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    if SAVE_HTML_DEBUG:
        DEBUG_HTML_DIR.mkdir(exist_ok=True)
    if SAVE_JSON_DEBUG:
        DEBUG_JSON_DIR.mkdir(exist_ok=True)

    chrome_path = find_chrome()
    if not chrome_path:
        print("ERROR: Could not find Chrome.")
        sys.exit(1)

    print("=" * 60)
    print("FASB ASC Show-All PDF Crawler")
    print("=" * 60)
    print()
    print("STEP 1: Launch Chrome with debugging port.")
    print("STEP 2: Complete login / human verification in that Chrome window.")
    print("STEP 3: Once the FASB home page is fully loaded, press ENTER here.")
    print()

    chrome_proc = launch_chrome()
    input(">>> Press ENTER to begin... ")

    visited = load_progress()
    if visited:
        print(f"\nResuming — {len(visited)} show-all item(s) already exported.")

    created_count = 0

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()

        print(f"\nConnected. URL: {page.url}  Title: {page.title()}")
        if "/Home" not in page.url:
            print("Not on Home page, navigating...")
            go_home(page)

        try:
            left_nav = wait_for_left_navigation(page)
        except Exception:
            print("Home page loaded, but leftNavigationMenu was not in localStorage yet. Retrying once after a refresh...")
            go_home(page)
            left_nav = wait_for_left_navigation(page)

        nodes = list(iter_showall_nodes(left_nav))
        print(f"\nDiscovered {len(nodes)} show-all node(s) from leftNavigationMenu.")
        print()

        try:
            for idx, (node, breadcrumb) in enumerate(nodes, start=1):
                path_key = " > ".join(breadcrumb)
                if path_key in visited:
                    print(f"[{idx}/{len(nodes)}] Skip already exported: {path_key}")
                    continue

                print(f"[{idx}/{len(nodes)}] Exporting: {path_key}")
                try:
                    pdf_path = export_showall_node(page, chrome_path, node, breadcrumb)
                    if pdf_path:
                        created_count += 1
                        print(f"    OK: {pdf_path}")
                    else:
                        print("    Skipped: nothing to export")
                    visited.add(path_key)
                    save_progress(visited)
                except Exception as exc:
                    print(f"    ERROR: {exc}")

                if MAX_SHOWALL_PDFS is not None and created_count >= MAX_SHOWALL_PDFS:
                    print(f"\nReached MAX_SHOWALL_PDFS={MAX_SHOWALL_PDFS}; stopping early.")
                    break

        except KeyboardInterrupt:
            print("\n\nInterrupted. Progress saved.")
        finally:
            save_progress(visited)
            print(f"\nExported {created_count} new show-all PDF(s) + HTML(s) to {DOWNLOAD_DIR}")
            browser.close()
            chrome_proc.terminate()


if __name__ == "__main__":
    main()
