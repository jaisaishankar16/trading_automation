"""Standalone Python Debug/Visual Test Script for NSE Volume Gainers Scraper.

Visually verifies each step of scraping:
https://www.nseindia.com/market-data/volume-gainers-spurts

Features:
- Playwright sync API with visible Chromium window (headless=False, slow_mo=800)
- Saves numbered screenshots to ./debug_output/ at each step
- Pauses with input() after each step so a human can inspect the browser
- Highlights the 'Download CSV' button in red before clicking
- Prints raw CSV lines, pandas DataFrame columns, and first 10 rows
- Re-scrapes the live HTML table and prints a side-by-side diff of SYMBOL values
- Robust try/except error handling with error screenshot capture
"""

import os
import sys
import time
import traceback
from pathlib import Path
from typing import List, Optional, Tuple

# Ensure UTF-8 output encoding for console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd
from playwright.sync_api import Error as PlaywrightError, Page, sync_playwright

# ---------------------------------------------------------------------------
# Configuration & Constants
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DEBUG_OUTPUT_DIR = BASE_DIR / "debug_output"

NSE_HOME_URL = "https://www.nseindia.com"
NSE_VOLUME_GAINERS_URL = "https://www.nseindia.com/market-data/volume-gainers-spurts"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

VIEWPORT = {"width": 1920, "height": 1080}
TIMEOUT_MS = 45000
SLOW_MO_MS = 800

# Selectors for locating the "Download CSV" button on NSE pages
DOWNLOAD_BUTTON_SELECTORS = [
    "#volumeGainersTable-download",
    "#cr_volume_gainers_table_download",
    "a[id*='download']",
    "a:has-text('Download (.csv)')",
    "a:has-text('Download CSV')",
    "a:has-text('CSV')",
    "div.download-data-link a",
    "div.table-download a",
    "[title*='Download']",
]

# Selectors indicating live table data rows in the DOM
TABLE_CONTAINER_SELECTORS = [
    "table tbody tr",
    "#volumeGainersTable",
    "#cr_volume_gainers_table",
    "table.common_table",
    "table.table",
    "div.dataTables_wrapper table",
]


# ---------------------------------------------------------------------------
# Console Helper Functions
# ---------------------------------------------------------------------------
def print_banner(step_id: str, title: str) -> None:
    """Print a visually distinct banner for each step."""
    line = "=" * 78
    print(f"\n{line}")
    print(f"  [STEP {step_id.upper()}] : {title}")
    print(f"{line}\n")


def print_info(label: str, value: str) -> None:
    """Print labeled key-value information."""
    print(f"  * {label:<22}: {value}")


def print_success(message: str) -> None:
    """Print success message."""
    print(f"\n  [SUCCESS] {message}")


def print_warning(message: str) -> None:
    """Print warning message."""
    print(f"\n  [WARNING] {message}")


AUTO_MODE = "--auto" in sys.argv or os.getenv("AUTO_DEBUG", "0") == "1"


def prompt_user(step_id: str, next_action: str) -> None:
    """Pause execution and prompt human operator to inspect browser."""
    print(f"\n{'-' * 78}")
    print(f"  >> [PAUSE] Step {step_id.upper()} completed.")
    print(f"  >> Please inspect the browser window.")
    if AUTO_MODE:
        print(f"  >> [AUTO-MODE] Automatically proceeding to: {next_action} (1.5s delay)")
        print(f"{'-' * 78}")
        time.sleep(1.5)
        return

    print(f"  >> Press ENTER to proceed to: {next_action}")
    print(f"{'-' * 78}")
    try:
        input("  [Press Enter to continue...]")
    except (EOFError, KeyboardInterrupt):
        print("  [Non-interactive or piped input detected. Continuing...]")


def save_screenshot_safe(page: Optional[Page], filename: str) -> Optional[Path]:
    """Capture and save screenshot to debug_output directory."""
    if page is None or page.is_closed():
        print_warning("Cannot save screenshot: page is not open.")
        return None

    try:
        DEBUG_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        screenshot_path = DEBUG_OUTPUT_DIR / filename
        page.screenshot(path=str(screenshot_path), full_page=False)
        print_info("Screenshot saved", str(screenshot_path.relative_to(BASE_DIR)))
        return screenshot_path
    except Exception as e:
        print_warning(f"Failed to capture screenshot '{filename}': {e}")
        return None


# ---------------------------------------------------------------------------
# Live Table HTML Scraper Helper
# ---------------------------------------------------------------------------
def scrape_symbols_from_html_table(page: Page, max_rows: int = 10) -> List[str]:
    """Extract stock symbols directly from the rendered HTML table in the DOM."""
    try:
        symbols = page.evaluate("""() => {
            const tables = Array.from(document.querySelectorAll('table'));
            let targetTable = tables.find(t => {
                const rows = t.querySelectorAll('tbody tr');
                return rows.length > 0 && Array.from(rows).some(r => r.querySelectorAll('td').length > 0);
            });
            if (!targetTable && tables.length > 0) {
                targetTable = tables[0];
            }
            if (!targetTable) return [];

            // Determine symbol column index from first header row
            let symbolColIdx = 0;
            const headerRow = targetTable.querySelector('thead tr') || targetTable.querySelector('tr');
            if (headerRow) {
                const ths = Array.from(headerRow.querySelectorAll('th, td'));
                const foundIdx = ths.findIndex(th => /symbol/i.test(th.innerText.trim()));
                if (foundIdx !== -1) {
                    symbolColIdx = foundIdx;
                }
            }

            const rows = Array.from(targetTable.querySelectorAll('tbody tr'));
            const extracted = [];

            for (const row of rows) {
                const cells = Array.from(row.querySelectorAll('td'));
                if (cells.length === 0) continue;

                let symbolText = '';
                // Prefer cell at symbolColIdx, checking anchor link first
                if (cells.length > symbolColIdx) {
                    const targetCell = cells[symbolColIdx];
                    const link = targetCell.querySelector('a');
                    symbolText = (link ? link.innerText : targetCell.innerText) || targetCell.textContent || '';
                } else {
                    const link = row.querySelector('a');
                    symbolText = (link ? link.innerText : cells[0].innerText) || '';
                }

                const cleanSymbol = symbolText.trim().split('\\n')[0].split('\\t')[0].trim().toUpperCase();
                if (cleanSymbol && !['TOTAL', 'N/A', 'NAN', 'SYMBOL'].includes(cleanSymbol)) {
                    extracted.push(cleanSymbol);
                }
            }
            return extracted;
        }""")
        return [str(s).strip().upper() for s in symbols[:max_rows]]
    except Exception as e:
        print_warning(f"Failed to scrape symbols via DOM evaluate: {e}")
        return []


# ---------------------------------------------------------------------------
# Main Step-by-Step Debug Scraper Flow
# ---------------------------------------------------------------------------
def run_debug_scraper() -> None:
    """Execute the full interactive debug scraper workflow."""
    DEBUG_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print("\n" + "#" * 78)
    print("  NSE INDIA VOLUME GAINERS & SPURTS - VISUAL DEBUG / STEP VERIFIER")
    print(f"  Debug Output Directory: {DEBUG_OUTPUT_DIR.resolve()}")
    print("#" * 78)

    playwright_instance = None
    browser = None
    context = None
    page = None
    current_step = "Init"

    try:
        # ===================================================================
        # STEP A: Launch Browser
        # ===================================================================
        current_step = "Step A: Launch Browser"
        print_banner("A", "Launch Browser & Configure Playwright Sync")

        print_info("Headless Mode", "False (Visible Chromium Window)")
        print_info("Slow Motion Delay", f"{SLOW_MO_MS} ms")
        print_info("Target Initial URL", NSE_HOME_URL)
        print_info("Target Data URL", NSE_VOLUME_GAINERS_URL)

        playwright_instance = sync_playwright().start()
        browser = playwright_instance.chromium.launch(
            headless=False,
            slow_mo=SLOW_MO_MS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--start-maximized",
            ],
        )

        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport=VIEWPORT,
            locale="en-US",
            timezone_id="Asia/Kolkata",
            accept_downloads=True,
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"Windows"',
                "Upgrade-Insecure-Requests": "1",
            },
        )

        page = context.new_page()
        page.set_default_timeout(TIMEOUT_MS)

        print_success("Chromium launched and context initialized.")
        prompt_user("A", "Navigate to NSE Homepage (Step B)")

        # ===================================================================
        # STEP B: Navigate to NSE Homepage (Session Cookie Initialization)
        # ===================================================================
        current_step = "Step B: Navigate to NSE Homepage"
        print_banner("B", f"Navigate to NSE Homepage: {NSE_HOME_URL}")

        print_info("Action", f"page.goto('{NSE_HOME_URL}')")
        response = page.goto(NSE_HOME_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        time.sleep(2.0)  # Allow initial cookies and background scripts to settle

        actual_url = page.url
        http_status = response.status if response else "Unknown"

        print_info("HTTP Status", str(http_status))
        print_info("Actual Resulting URL", actual_url)

        if actual_url.rstrip("/") != NSE_HOME_URL.rstrip("/"):
            print_warning(f"URL redirected: {NSE_HOME_URL} -> {actual_url}")
        else:
            print_success(f"Successfully loaded {actual_url}")

        save_screenshot_safe(page, "01_homepage_loaded.png")
        prompt_user("B", "Navigate to Volume Gainers & Spurts page (Step C)")

        # ===================================================================
        # STEP C: Navigate to Volume Gainers & Spurts Page
        # ===================================================================
        current_step = "Step C: Navigate to Volume Gainers Page"
        print_banner("C", f"Navigate to Volume Gainers Page: {NSE_VOLUME_GAINERS_URL}")

        print_info("Action", f"page.goto('{NSE_VOLUME_GAINERS_URL}')")
        target_response = page.goto(
            NSE_VOLUME_GAINERS_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS
        )
        time.sleep(2.0)

        target_actual_url = page.url
        target_status = target_response.status if target_response else "Unknown"

        print_info("HTTP Status", str(target_status))
        print_info("Actual Resulting URL", target_actual_url)

        if "volume-gainers-spurts" not in target_actual_url:
            print_warning(f"Unexpected redirect detected! Current URL: {target_actual_url}")
        else:
            print_success(f"Confirmed on target page: {target_actual_url}")

        save_screenshot_safe(page, "02_target_page_loaded.png")
        prompt_user("C", "Wait for Data Table to Render (Step D)")

        # ===================================================================
        # STEP D: Wait for Data Table to Render
        # ===================================================================
        current_step = "Step D: Wait for Data Table"
        print_banner("D", "Wait for Live Data Table to Render in DOM")

        table_found = False
        matched_table_selector = None

        print_info("Checking selectors", ", ".join(TABLE_CONTAINER_SELECTORS[:3]))
        for selector in TABLE_CONTAINER_SELECTORS:
            try:
                page.wait_for_selector(selector, state="visible", timeout=15000)
                table_found = True
                matched_table_selector = selector
                break
            except Exception:
                continue

        if not table_found:
            # Fallback: wait for generic table body rows
            try:
                page.wait_for_selector("table tbody tr", state="visible", timeout=15000)
                table_found = True
                matched_table_selector = "table tbody tr"
            except Exception as e:
                print_warning(f"Table selector wait timed out: {e}")

        # Count rendered rows
        rendered_rows = page.locator("table tbody tr")
        row_count = rendered_rows.count()

        print_info("Table Detected", "Yes" if table_found else "No")
        print_info("Matched Table Selector", str(matched_table_selector))
        print_info("Rendered Rows Found", str(row_count))

        if row_count > 0:
            print_success(f"Data table successfully rendered with {row_count} rows.")
            # Scroll table into view for optimal screenshot
            try:
                rendered_rows.first.scroll_into_view_if_needed()
            except Exception:
                pass
        else:
            print_warning("No data rows detected in table tbody yet.")

        save_screenshot_safe(page, "03_table_rendered.png")
        prompt_user("D", "Locate and Highlight 'Download CSV' Button (Step E)")

        # ===================================================================
        # STEP E: Locate & Highlight "Download (.csv)" Element
        # ===================================================================
        current_step = "Step E: Locate & Highlight Download CSV Button"
        print_banner("E", "Locate & Visually Highlight 'Download CSV' Button")

        download_elem = None
        matched_btn_selector = None

        for selector in DOWNLOAD_BUTTON_SELECTORS:
            try:
                candidate = page.locator(selector).first
                if candidate.is_visible(timeout=2000):
                    download_elem = candidate
                    matched_btn_selector = selector
                    break
            except Exception:
                continue

        if download_elem is None:
            # Fallback: search by text matching CSV or Download
            try:
                candidate = page.locator("a:text-matches('CSV|Download', 'i')").first
                if candidate.is_visible(timeout=3000):
                    download_elem = candidate
                    matched_btn_selector = "a:text-matches('CSV|Download', 'i')"
            except Exception:
                pass

        if download_elem is None:
            raise RuntimeError(
                "Could not locate the 'Download CSV' button using any known selectors or text match."
            )

        # Inspect element properties
        btn_outer_html = download_elem.evaluate("el => el.outerHTML")
        btn_text = (download_elem.inner_text() or "").strip()

        print_info("Matched Selector", matched_btn_selector)
        print_info("Button Text Content", repr(btn_text))
        print_info("Element outerHTML", btn_outer_html)

        # Scroll into view and apply prominent visual highlight (Red border + glow + yellow background)
        download_elem.scroll_into_view_if_needed()
        download_elem.evaluate(
            """el => {
                el.style.border = '4px solid red';
                el.style.boxShadow = '0 0 15px 4px rgba(255, 0, 0, 0.9)';
                el.style.backgroundColor = 'rgba(255, 255, 0, 0.4)';
                el.style.transition = 'all 0.3s ease';
            }"""
        )

        try:
            download_elem.highlight()
        except Exception:
            pass  # highlight() may not be supported on older versions, styling above ensures visual clarity

        print_success("Download button highlighted in RED on screen!")
        save_screenshot_safe(page, "04_download_button_highlighted.png")
        prompt_user("E", "Click Download Button and Capture CSV (Step F)")

        # ===================================================================
        # STEP F: Click Download and Save CSV File
        # ===================================================================
        current_step = "Step F: Click Download and Save CSV"
        print_banner("F", "Trigger Click & Save Downloaded CSV")

        download_target_path = DEBUG_OUTPUT_DIR / "downloaded.csv"
        # Delete existing downloaded.csv if present to ensure fresh capture
        if download_target_path.exists():
            download_target_path.unlink()

        print_info("Download Target Path", str(download_target_path.resolve()))
        print_info("Action", "page.expect_download() and download_elem.click(force=True)")

        with page.expect_download(timeout=30000) as download_info:
            download_elem.click(force=True)

        download = download_info.value
        download.save_as(str(download_target_path))

        file_size_bytes = download_target_path.stat().st_size
        file_size_kb = file_size_bytes / 1024.0

        print_info("Downloaded File Size", f"{file_size_kb:.2f} KB ({file_size_bytes} bytes)")
        print_info("Suggested Filename", download.suggested_filename)
        print_success(f"CSV successfully captured and saved to {download_target_path.name}")

        save_screenshot_safe(page, "05_download_completed.png")
        prompt_user("F", "Inspect First 5 Raw Lines of CSV (Step G)")

        # ===================================================================
        # STEP G: Print First 5 Raw Lines of the Downloaded CSV
        # ===================================================================
        current_step = "Step G: Print Raw CSV Lines"
        print_banner("G", "Inspect First 5 Raw Lines of Downloaded CSV (Unparsed)")

        raw_lines: List[str] = []
        try:
            with open(download_target_path, "r", encoding="utf-8-sig", errors="replace") as f:
                for _ in range(5):
                    line = f.readline()
                    if not line:
                        break
                    raw_lines.append(line.rstrip("\r\n"))
        except Exception as e:
            print_warning(f"Error reading with utf-8-sig: {e}, falling back to latin-1")
            with open(download_target_path, "r", encoding="latin-1", errors="replace") as f:
                for _ in range(5):
                    line = f.readline()
                    if not line:
                        break
                    raw_lines.append(line.rstrip("\r\n"))

        print("  Raw File Content (Lines 1 to 5):")
        print("  " + "-" * 74)
        for idx, line_content in enumerate(raw_lines, 1):
            print(f"  Line {idx:02d}: {line_content}")
        print("  " + "-" * 74)

        prompt_user("G", "Parse CSV with Pandas and Display Table (Step H)")

        # ===================================================================
        # STEP H: Parse CSV with Pandas & Display Columns + First 10 Rows
        # ===================================================================
        current_step = "Step H: Parse CSV with Pandas"
        print_banner("H", "Parse CSV with Pandas & Display Columns / First 10 Rows")

        try:
            df = pd.read_csv(download_target_path, encoding="utf-8-sig", on_bad_lines="skip")
        except UnicodeDecodeError:
            df = pd.read_csv(download_target_path, encoding="latin-1", on_bad_lines="skip")

        # Strip whitespace from column names
        df.columns = [str(c).strip() for c in df.columns]

        print_info("Total CSV Rows", str(len(df)))
        print_info("Total CSV Columns", str(len(df.columns)))
        print_info("All Column Names", str(df.columns.tolist()))

        # Determine designate SYMBOL column
        symbol_col_name = None
        for col in df.columns:
            cleaned_col = str(col).strip().upper()
            if cleaned_col in ("SYMBOL", "SYMBOL*", "SECURITY", "SYMBOL \n"):
                symbol_col_name = col
                break

        if symbol_col_name is None:
            symbol_col_name = df.columns[0]
            print_info("Designated SYMBOL Column", f"'{symbol_col_name}' (Defaulted to column 0)")
        else:
            print_info("Designated SYMBOL Column", f"'{symbol_col_name}' (Matched by name)")

        print("\n  [DataFrame First 10 Rows (All Columns)]:")
        print("  " + "-" * 74)
        pd.set_option("display.max_columns", None)
        pd.set_option("display.width", 1000)
        pd.set_option("display.max_colwidth", 20)
        print(df.head(10).to_string())
        print("  " + "-" * 74)

        prompt_user("H", "Re-scrape HTML Table and Diff against CSV (Step I)")

        # ===================================================================
        # STEP I: Re-scrape Live HTML Table and Compare SYMBOLs Side-by-Side
        # ===================================================================
        current_step = "Step I: Side-by-Side Diff (HTML Table vs CSV)"
        print_banner("I", "Side-by-Side Diff: HTML Table Scraped vs CSV Extracted SYMBOLs")

        # Extract symbols directly from HTML table
        html_symbols = scrape_symbols_from_html_table(page, max_rows=10)

        # Extract symbols from pandas DataFrame
        raw_csv_symbols = df[symbol_col_name].dropna().astype(str).tolist()
        csv_symbols = [s.strip().upper() for s in raw_csv_symbols[:10]]

        num_compare = min(10, max(len(html_symbols), len(csv_symbols)))

        print("\n  " + "=" * 74)
        print(f"  {'#':<4} | {'Live HTML Table SYMBOL':<26} | {'Downloaded CSV SYMBOL':<26} | {'Status'}")
        print("  " + "-" * 74)

        matches = 0
        mismatches = 0

        for i in range(num_compare):
            html_sym = html_symbols[i] if i < len(html_symbols) else "<N/A>"
            csv_sym = csv_symbols[i] if i < len(csv_symbols) else "<N/A>"

            is_match = (html_sym == csv_sym) and (html_sym != "<N/A>")
            status_str = "[MATCH]" if is_match else "[MISMATCH]"

            if is_match:
                matches += 1
            else:
                mismatches += 1

            print(f"  {i+1:<4} | {html_sym:<26} | {csv_sym:<26} | {status_str}")

        print("  " + "=" * 74)
        print_info("Comparison Summary", f"{matches}/{num_compare} Matches, {mismatches}/{num_compare} Mismatches")

        if mismatches == 0 and matches > 0:
            print_success("PERFECT MATCH: Live HTML table and Downloaded CSV SYMBOL columns match 100%!")
        elif mismatches > 0:
            print_warning("DISCREPANCY DETECTED: Check column mapping or live table sorting differences above.")

        save_screenshot_safe(page, "06_comparison_done.png")

        # ===================================================================
        # FINAL INSPECTION STEP: Keep browser open for manual review
        # ===================================================================
        print_banner("DONE", "All Automated Verification Steps Completed Successfully")
        print("  The Chromium browser window remains open for your manual inspection.")
        print("  You can now interact with the live page, open DevTools, or check downloaded files.")
        print(f"  All step screenshots saved to: {DEBUG_OUTPUT_DIR.resolve()}\n")

        if AUTO_MODE:
            print("  [AUTO-MODE] Complete. Closing browser after 2s...")
            time.sleep(2.0)
        else:
            try:
                input("  [Press ENTER when you are ready to close the browser and exit...] ")
            except (EOFError, KeyboardInterrupt):
                print("  [Closing browser...]")

    except Exception as e:
        print("\n" + "!" * 78)
        print(f"  [FAILURE ENCOUNTERED AT: {current_step}]")
        print("!" * 78)
        print(f"\n  Error Type: {type(e).__name__}")
        print(f"  Error Details: {e}\n")
        print("  Full Traceback:")
        traceback.print_exc()

        # Capture emergency failure screenshot
        if page is not None and not page.is_closed():
            step_slug = current_step.lower().replace(" ", "_").replace(":", "")[:30]
            err_img = save_screenshot_safe(page, f"error_{step_slug}.png")
            if err_img:
                print(f"\n  Failure state screenshot saved to: {err_img.resolve()}")

            print("\n  [Browser is kept open for manual debugging of the failure.]")
            try:
                input("  Press ENTER to close the browser and exit after inspecting...")
            except (EOFError, KeyboardInterrupt):
                pass

    finally:
        print("\n  [Cleaning up browser resources...]")
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
        print("  [Finished clean-up. Debug session completed.]\n")


if __name__ == "__main__":
    run_debug_scraper()
