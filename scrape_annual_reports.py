"""Screener.in Annual Report PDF Downloader.

Automates downloading the latest Annual Report PDFs from https://www.screener.in/
for a list of stock symbols read from data/volume_gainers.json.

Key Features:
- Playwright sync API with configurable headless mode (HEADLESS env var or --headed flag)
- Robust semantic/text-based DOM matching without brittle CSS classes
- Dynamically identifies the most recent Annual Report year (never hardcoded)
- Resumable: skips symbols whose reports are already downloaded (>5KB)
- Handles direct PDF streams, BSE/NSE attachment links, intermediate tabs, and embeds
- Detailed INFO logging, per-symbol error isolation, and end-of-run summary table
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, sync_playwright

# Ensure UTF-8 output encoding for console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("screener_scraper")

# ---------------------------------------------------------------------------
# Path & Configuration Setup
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DATA_FILE = Path(os.getenv("VOLUME_GAINERS_DATA_FILE", BASE_DIR / "data" / "volume_gainers.json"))
OUTPUT_DIR = Path(os.getenv("ANNUAL_REPORTS_DIR", BASE_DIR / "annual_reports"))

# Headless configuration: defaults to True, can be toggled via env or CLI
HEADLESS_DEFAULT = os.getenv("HEADLESS", "true").lower() in ("true", "1", "t")
if "--headed" in sys.argv:
    HEADLESS_DEFAULT = False
elif "--headless" in sys.argv:
    HEADLESS_DEFAULT = True

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
VIEWPORT = {"width": 1920, "height": 1080}
MIN_VALID_PDF_BYTES = 5 * 1024  # 5 KB minimum to ensure genuine PDF payload
DELAY_BETWEEN_SYMBOLS_SEC = 2.5


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------
def load_symbols_from_json(file_path: Path) -> List[str]:
    """Read the stock symbol list from volume_gainers.json."""
    if not file_path.exists():
        logger.error("Input JSON file does not exist: %s", file_path)
        return []

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        symbols = data.get("symbols", [])
        logger.info("Successfully loaded %d symbols from %s", len(symbols), file_path)
        return symbols
    except Exception as e:
        logger.error("Failed to parse JSON file %s: %s", file_path, e)
        return []


def locate_search_input(page: Page, logged_holder: List[bool]) -> Any:
    """Find the visible search input on screener.in using semantic selectors."""
    selectors = [
        "input[placeholder*='Search for a company' i]:visible",
        "input[aria-label*='Search for a company' i]:visible",
        "input[data-company-search='true']:visible",
        "input[type='search']:visible",
        "input.u-full-width:visible",
    ]

    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1500):
                if not logged_holder[0]:
                    logger.info("Matched search input selector: '%s'", sel)
                    logged_holder[0] = True
                return loc
        except Exception:
            continue

    # Fallback to any visible text input
    try:
        fallback = page.locator("input:visible").first
        if fallback.is_visible(timeout=2000):
            if not logged_holder[0]:
                logger.info("Matched fallback visible input selector.")
                logged_holder[0] = True
            return fallback
    except Exception:
        pass

    return None


def navigate_to_company_page(page: Page, symbol: str, logged_holder: List[bool]) -> bool:
    """Search for the company on screener.in and navigate to its company page."""
    logger.info("[%s] Step 1: Navigating to Screener homepage...", symbol)
    page.goto("https://www.screener.in/", wait_until="domcontentloaded", timeout=30000)

    search_input = locate_search_input(page, logged_holder)
    if search_input is None:
        logger.warning("[%s] Could not locate visible search input; trying direct URL fallback.", symbol)
        direct_url = f"https://www.screener.in/company/{symbol}/"
        page.goto(direct_url, wait_until="domcontentloaded", timeout=30000)
        return "/company/" in page.url

    logger.info("[%s] Step 2: Typing symbol into search box...", symbol)
    search_input.click()
    search_input.fill("")
    search_input.type(symbol, delay=70)
    time.sleep(1.8)

    # Inspect dropdown suggestions dynamically
    suggestions = page.evaluate("""(sym) => {
        const results = [];
        const items = Array.from(document.querySelectorAll(
            'ul.dropdown-menu li, .dropdown-content li, [role="listbox"] li, .search-results li, ul.has-search-results li, li:has(a[href*="/company/"])'
        ));
        for (const item of items) {
            const a = item.querySelector('a') || (item.tagName === 'A' ? item : null);
            const text = item.innerText.trim();
            const href = a ? a.href : '';
            if (href.includes('/company/')) {
                results.push({ text, href });
            }
        }
        return results;
    }""", symbol)

    matched_href = None
    if suggestions:
        logger.info("[%s] Found %d search suggestions.", symbol, len(suggestions))
        for s in suggestions:
            if symbol.upper() in s["text"].upper() or f"/{symbol.upper()}/" in s["href"].upper():
                matched_href = s["href"]
                logger.info("[%s] Matched suggestion in dropdown: '%s' -> %s", symbol, s["text"], s["href"])
                break
        if not matched_href and suggestions:
            # Pick first suggestion if specific match not differentiated
            matched_href = suggestions[0]["href"]
            logger.info("[%s] Selecting top dropdown suggestion: '%s' -> %s", symbol, suggestions[0]["text"], matched_href)

    if matched_href:
        logger.info("[%s] Navigating to matched company URL via suggestion link...", symbol)
        page.goto(matched_href, wait_until="domcontentloaded", timeout=30000)
    else:
        logger.info("[%s] No dropdown suggestion matched; pressing Enter on search input...", symbol)
        search_input.press("Enter")
        page.wait_for_timeout(2500)
        if "/company/" not in page.url:
            direct_url = f"https://www.screener.in/company/{symbol}/"
            logger.info("[%s] Direct navigation fallback: %s", symbol, direct_url)
            page.goto(direct_url, wait_until="domcontentloaded", timeout=30000)

    page_url = page.url
    if "/company/" in page_url:
        logger.info("[%s] Successfully landed on company page: %s", symbol, page_url)
        return True
    else:
        logger.error("[%s] Failed to land on company page (current URL: %s)", symbol, page_url)
        return False


def extract_latest_annual_report_link(page: Page, symbol: str) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """Scan the company page for the Documents -> Annual Reports subsection and select the latest report.

    Returns:
        Tuple of (latest_year, link_text, pdf_url)
    """
    logger.info("[%s] Step 3: Scanning page for 'Documents' -> 'Annual Reports' sub-section...", symbol)

    reports_data = page.evaluate("""() => {
        const results = [];
        // Locate Annual Reports heading
        const allHeadings = Array.from(document.querySelectorAll('h1, h2, h3, h4, h5, h6, .sub-heading, div.title, p.sub'));
        const arHeading = allHeadings.find(h => {
            const t = h.innerText.trim();
            return /^\\s*annual\\s*reports?\\s*$/i.test(t) || (/annual\\s*reports?/i.test(t) && !/announcements/i.test(t));
        });

        let targetContainer = null;
        if (arHeading) {
            targetContainer = arHeading.closest('div, section, li');
            if (targetContainer && targetContainer.querySelectorAll('a').length === 0) {
                targetContainer = arHeading.nextElementSibling || targetContainer.parentElement;
            }
        }

        // Scope to target container if found, else fall back to #documents or whole page
        const searchScope = targetContainer || document.querySelector('#documents') || document.body;
        const links = Array.from(searchScope.querySelectorAll('a'));

        for (const a of links) {
            const text = a.innerText.trim();
            const href = a.href || '';
            // Ensure we avoid announcement links, rating links, concall transcripts
            if (targetContainer || /annual\\s*report|financial\\s*year|fy\\s*\\d{2,4}/i.test(text) || /bseplus\\/AnnualReport/i.test(href)) {
                if (!/announcement|transcript|credit\\s*rating|concall/i.test(text)) {
                    results.push({
                        text: text,
                        href: href,
                    });
                }
            }
        }
        return {
            headingFound: !!arHeading,
            headingText: arHeading ? arHeading.innerText.trim() : null,
            links: results
        };
    }""")

    heading_found = reports_data.get("headingFound", False)
    raw_links = reports_data.get("links", [])
    logger.info("[%s] Annual Reports subsection located: %s | Links count: %d", symbol, heading_found, len(raw_links))

    if not raw_links:
        logger.warning("[%s] No annual report links detected in DOM.", symbol)
        return None, None, None

    # Parse years dynamically using regular expressions
    parsed_candidates = []
    for item in raw_links:
        text = item["text"]
        href = item["href"]

        # Search for 4-digit year in text first, then href
        year_match = re.search(r'\b(20\d{2}|19\d{2})\b', text)
        if not year_match:
            year_match = re.search(r'\b(20\d{2}|19\d{2})\b', href)

        if year_match:
            year_val = int(year_match.group(1))
            parsed_candidates.append({
                "year": year_val,
                "text": text,
                "href": href,
            })

    if not parsed_candidates:
        logger.warning("[%s] Found links but could not parse a valid 4-digit year from any link.", symbol)
        return None, None, None

    # Sort descending to dynamically pick the latest available year
    parsed_candidates.sort(key=lambda x: x["year"], reverse=True)
    latest = parsed_candidates[0]
    logger.info(
        "[%s] Dynamically selected latest Annual Report: Year %d | Text: '%s' | URL: %s",
        symbol, latest["year"], latest["text"].replace("\n", " "), latest["href"]
    )
    return latest["year"], latest["text"], latest["href"]


def download_pdf_file(
    context: BrowserContext,
    page: Page,
    symbol: str,
    pdf_url: str,
    target_path: Path,
) -> bool:
    """Download the PDF payload using direct requests or browser tab capture."""
    logger.info("[%s] Step 4: Initiating download for URL: %s", symbol, pdf_url)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Strategy A: Direct HTTP request with current session cookies and browser headers
    try:
        cookies = {c["name"]: c["value"] for c in context.cookies()}
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": page.url,
            "Accept": "application/pdf,application/octet-stream,*/*",
        }
        resp = requests.get(pdf_url, headers=headers, cookies=cookies, timeout=40, stream=True)
        content_type = resp.headers.get("Content-Type", "").lower()

        if resp.status_code == 200:
            content_bytes = resp.content
            # Check for genuine PDF magic header or non-trivial size
            if content_bytes.startswith(b"%PDF") or "pdf" in content_type or len(content_bytes) > MIN_VALID_PDF_BYTES:
                with open(target_path, "wb") as f:
                    f.write(content_bytes)
                file_size_kb = target_path.stat().st_size / 1024.0
                logger.info(
                    "[%s] Successfully downloaded PDF via HTTP stream: %.2f KB -> %s",
                    symbol, file_size_kb, target_path.name
                )
                return True
            else:
                logger.warning(
                    "[%s] HTTP response status 200 but content is not a verified PDF (size: %d bytes, Content-Type: %s)",
                    symbol, len(content_bytes), content_type
                )
    except Exception as e:
        logger.warning("[%s] Direct HTTP download failed: %s (falling back to browser tab capture)", symbol, e)

    # Strategy B: Open link in browser tab, handle intermediate page / embeds / download event
    try:
        logger.info("[%s] Opening PDF link via Playwright new tab capture...", symbol)
        link_locator = page.locator(f"a[href='{pdf_url}']").first

        new_tab = None
        try:
            with context.expect_page(timeout=15000) as new_page_info:
                link_locator.click(force=True)
            new_tab = new_page_info.value
            new_tab.wait_for_load_state("domcontentloaded", timeout=20000)
            resolved_url = new_tab.url
            logger.info("[%s] New tab opened with resolved URL: %s", symbol, resolved_url)

            # Check if resolved URL is a direct PDF link
            if resolved_url.lower().endswith(".pdf") or "attachhis" in resolved_url.lower() or "annpdfopen" in resolved_url.lower():
                resp = requests.get(resolved_url, headers={"User-Agent": USER_AGENT, "Referer": page.url}, timeout=40)
                if resp.status_code == 200 and len(resp.content) > MIN_VALID_PDF_BYTES:
                    with open(target_path, "wb") as f:
                        f.write(resp.content)
                    logger.info("[%s] Saved PDF from tab URL: %.2f KB", symbol, target_path.stat().st_size / 1024.0)
                    return True

            # If intermediate page: scan for embed, iframe, or pdf link in new tab
            embed_pdf_url = new_tab.evaluate("""() => {
                const embed = document.querySelector('embed[type*="pdf"], iframe[src*=".pdf"], object[data*=".pdf"]');
                if (embed) return embed.src || embed.data;
                const pdfLink = Array.from(document.querySelectorAll('a[href*=".pdf"]')).find(a => a.href);
                return pdfLink ? pdfLink.href : null;
            }""")

            if embed_pdf_url:
                logger.info("[%s] Found embedded PDF URL inside intermediate page: %s", symbol, embed_pdf_url)
                resp = requests.get(embed_pdf_url, headers={"User-Agent": USER_AGENT, "Referer": resolved_url}, timeout=40)
                if resp.status_code == 200 and len(resp.content) > MIN_VALID_PDF_BYTES:
                    with open(target_path, "wb") as f:
                        f.write(resp.content)
                    logger.info("[%s] Saved embedded PDF: %.2f KB", symbol, target_path.stat().st_size / 1024.0)
                    return True

        finally:
            if new_tab and not new_tab.is_closed():
                new_tab.close()

    except Exception as e:
        logger.error("[%s] Browser tab capture encountered error: %s", symbol, e)

    # Final file integrity verification
    if target_path.exists() and target_path.stat().st_size > MIN_VALID_PDF_BYTES:
        return True
    return False


def find_latest_annual_report_pdf(symbol: str, output_dir: Optional[Path] = None) -> Optional[Path]:
    """Find the most recent valid annual report PDF for a given symbol.
    
    Matches patterns:
    - <SYMBOL>_annual_report_<YYYYMMDD_HHMMSS>.pdf
    - <SYMBOL>_annual_report.pdf
    
    Returns:
        Path to the newest valid PDF file (> MIN_VALID_PDF_BYTES), or None.
    """
    dir_path = output_dir or OUTPUT_DIR
    clean_symbol = symbol.strip().upper()
    if not dir_path.exists():
        return None

    candidates: List[Path] = []
    candidates.extend(dir_path.glob(f"{clean_symbol}_annual_report_*.pdf"))
    legacy_file = dir_path / f"{clean_symbol}_annual_report.pdf"
    if legacy_file.exists():
        candidates.append(legacy_file)

    valid_candidates = [p for p in candidates if p.is_file() and p.stat().st_size > MIN_VALID_PDF_BYTES]
    if not valid_candidates:
        return None

    def sort_key(p: Path) -> float:
        match = re.search(r"_annual_report_(\d{8}_\d{6})\.pdf$", p.name)
        if match:
            try:
                dt = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
                return dt.timestamp()
            except ValueError:
                pass
        return p.stat().st_mtime

    valid_candidates.sort(key=sort_key, reverse=True)
    return valid_candidates[0]


def get_pdf_downloaded_at(pdf_path: Path) -> str:
    """Extract or infer the download timestamp of an annual report PDF as ISO 8601 string."""
    if not pdf_path.exists():
        return datetime.now(timezone.utc).isoformat()

    match = re.search(r"_annual_report_(\d{8}_\d{6})\.pdf$", pdf_path.name)
    if match:
        try:
            dt = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except ValueError:
            pass

    mtime = pdf_path.stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Main Batch Processing Orchestrator
# ---------------------------------------------------------------------------
def run_annual_reports_scraper() -> None:
    """Execute the full batch Annual Report PDF downloading workflow."""
    start_time = datetime.now(timezone.utc)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 78)
    print("  SCREENER.IN ANNUAL REPORTS SCRAPER & PDF DOWNLOADER")
    print(f"  Input File : {DATA_FILE.resolve()}")
    print(f"  Output Dir : {OUTPUT_DIR.resolve()}")
    print(f"  Headless   : {HEADLESS_DEFAULT}")
    print("=" * 78 + "\n")

    symbols = load_symbols_from_json(DATA_FILE)
    if not symbols:
        logger.error("No symbols found in %s. Exiting.", DATA_FILE)
        return

    logger.info("Found %d symbols to process: %s", len(symbols), ", ".join(symbols))

    succeeded: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    playwright_instance = None
    browser = None
    context = None
    page = None
    logged_search_selector = [False]

    try:
        playwright_instance = sync_playwright().start()
        browser = playwright_instance.chromium.launch(
            headless=HEADLESS_DEFAULT,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars",
            ],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport=VIEWPORT,
            locale="en-US",
            timezone_id="Asia/Kolkata",
            accept_downloads=True,
        )
        page = context.new_page()

        for idx, symbol in enumerate(symbols, 1):
            clean_symbol = symbol.strip().upper()

            print(f"\n[{idx}/{len(symbols)}] Processing Symbol: {clean_symbol}")
            print("-" * 60)

            # Check resumability using glob matching
            existing_pdf = find_latest_annual_report_pdf(clean_symbol, OUTPUT_DIR)
            if existing_pdf:
                size_kb = existing_pdf.stat().st_size / 1024.0
                logger.info("[%s] Already downloaded (%s, %.2f KB), skipping.", clean_symbol, existing_pdf.name, size_kb)
                skipped.append({
                    "symbol": clean_symbol,
                    "path": str(existing_pdf),
                    "size_kb": round(size_kb, 2),
                })
                continue

            # Generate timestamped filename for new download
            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            target_pdf_path = OUTPUT_DIR / f"{clean_symbol}_annual_report_{timestamp_str}.pdf"

            try:
                # 1. Navigate to company page
                if not navigate_to_company_page(page, clean_symbol, logged_search_selector):
                    raise RuntimeError(f"Could not land on Screener company page for '{clean_symbol}'.")

                # 2. Extract latest Annual Report link and year
                year, report_text, pdf_url = extract_latest_annual_report_link(page, clean_symbol)
                if not pdf_url:
                    raise RuntimeError(f"No valid Annual Report links found on company page for '{clean_symbol}'.")

                # 3. Download and verify PDF
                download_ok = download_pdf_file(context, page, clean_symbol, pdf_url, target_pdf_path)
                if not download_ok:
                    raise RuntimeError(f"Failed to capture valid PDF bytes from '{pdf_url}' (file missing or < 5KB).")

                # Validate saved file size
                final_size = target_pdf_path.stat().st_size
                final_size_kb = final_size / 1024.0
                logger.info(
                    "[%s] SUCCESS: Saved %d Annual Report (%.2f KB) -> %s",
                    clean_symbol, year, final_size_kb, target_pdf_path.name
                )
                succeeded.append({
                    "symbol": clean_symbol,
                    "year": year,
                    "path": str(target_pdf_path),
                    "size_kb": round(final_size_kb, 2),
                })

            except Exception as e:
                logger.error("[%s] FAILED: %s", clean_symbol, e)
                failed.append({
                    "symbol": clean_symbol,
                    "error": str(e),
                })

            # Courtesy delay between requests
            time.sleep(DELAY_BETWEEN_SYMBOLS_SEC)

    except KeyboardInterrupt:
        logger.warning("\n[INTERRUPTED] Script execution interrupted by user (Ctrl+C). Generating summary so far...")

    finally:
        # Resource cleanup
        logger.info("Cleaning up browser resources...")
        if page and not page.is_closed():
            try:
                page.close()
            except Exception:
                pass
        if context:
            try:
                context.close()
            except Exception:
                pass
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        if playwright_instance:
            try:
                playwright_instance.stop()
            except Exception:
                pass

        # Print Final Summary Report
        duration_sec = round((datetime.now(timezone.utc) - start_time).total_seconds(), 1)
        total_count = len(symbols)
        success_count = len(succeeded)
        skip_count = len(skipped)
        fail_count = len(failed)

        print("\n" + "=" * 78)
        print("  SCREENER.IN ANNUAL REPORTS SCRAPER - EXECUTION SUMMARY")
        print("=" * 78)
        print(f"  Total Symbols in Input  : {total_count}")
        print(f"  Successfully Downloaded : {success_count}")
        print(f"  Skipped (Already Exists): {skip_count}")
        print(f"  Failed                  : {fail_count}")
        print(f"  Total Time Elapsed      : {duration_sec}s")
        print("-" * 78)

        if succeeded:
            print("\n  [SUCCEEDED DOWNLOADS]:")
            for item in succeeded:
                print(f"    * {item['symbol']:<12} | Year {item['year']} | {item['size_kb']:>8.2f} KB | {item['path']}")

        if skipped:
            print("\n  [SKIPPED (ALREADY PRESENT)]:")
            for item in skipped:
                print(f"    * {item['symbol']:<12} | {item['size_kb']:>8.2f} KB | {item['path']}")

        if failed:
            print("\n  [FAILED SYMBOLS]:")
            for item in failed:
                print(f"    * {item['symbol']:<12} | Reason: {item['error']}")

        print("=" * 78 + "\n")


if __name__ == "__main__":
    run_annual_reports_scraper()
