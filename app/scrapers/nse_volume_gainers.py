"""NSE India Volume Gainers & Spurts Scraper Module (Task 1).

Automates the retrieval of the daily Volume Gainers & Spurts dataset from NSE India:
1. Launches Chromium using Playwright (sync API).
2. Initializes session cookies at the NSE homepage.
3. Navigates to the Volume Gainers page and waits for JS data tables to render.
4. Locates and clicks the "Download CSV" button to capture the official CSV export.
5. Parses the CSV to extract, clean, and deduplicate the SYMBOL column.
6. Cleans up temporary download files and closes browser resources reliably.

Extensibility Guide for Future Data Sources:
--------------------------------------------
To add a new data source (e.g., 52-Week Highs, F&O Participant OI, Delivery Position):
1. Create a new module: `app/scrapers/nse_<source_name>.py` following this same structure.
2. Implement `scrape_<source_name>()` with Playwright navigation, table wait, and download logic.
3. Add corresponding routes in `app/routes.py` (e.g., `POST /api/refresh/<source_name>`).
4. Cross-source analysis logic can be placed in a new `app/analysis.py` module to intersect/rank
   symbols across multiple scrapers.
"""

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import pandas as pd
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

from app.scrapers import (
    NSEBlockedError,
    NSEDownloadError,
    NSEPageStructureError,
    NSEScrapeError,
)
from config import Config

logger = logging.getLogger(__name__)

# Known selectors for the "Download CSV" button across NSE page templates
DOWNLOAD_BUTTON_SELECTORS = [
    "#vgainDownloadBtn",
    "a:has-text('Download (.csv)')",
    "span#dwldcsv",
    "#volumeGainersTable-download",
    "#cr_volume_gainers_table_download",
    "a[id*='download']",
    "a:has-text('Download CSV')",
    "a:has-text('CSV')",
    "div.download-data-link a",
    "div.table-download a",
    "[title*='Download']",
    "text=/download.*csv/i",
]

# Selectors indicating the live data table has finished rendering in the DOM
TABLE_CONTAINER_SELECTORS = [
    "table tbody tr",
    "#volumeGainersTable",
    "#cr_volume_gainers_table",
    "table.common_table",
    "table.table",
    "div.dataTables_wrapper table",
]


def _parse_symbols_from_csv(csv_path: Path) -> List[str]:
    """Parse downloaded CSV and extract unique, ordered SYMBOL strings.

    Args:
        csv_path: Path to the downloaded CSV file.

    Returns:
        List of non-empty, stripped, deduplicated stock symbols preserving order.
    """
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        raise NSEDownloadError(f"Downloaded CSV file is missing or empty at {csv_path}")

    try:
        # Load CSV with pandas, handling header variations and encodings
        df = pd.read_csv(csv_path, encoding="utf-8-sig", on_bad_lines="skip")
    except UnicodeDecodeError:
        df = pd.read_csv(csv_path, encoding="latin-1", on_bad_lines="skip")
    except Exception as e:
        raise NSEScrapeError(f"Failed to parse CSV file with pandas: {e}") from e

    if df.empty:
        logger.warning("Downloaded CSV contained 0 data rows.")
        return []

    # Locate the SYMBOL column (check column names or default to the first column)
    symbol_col = None
    for col in df.columns:
        if str(col).strip().upper() in ("SYMBOL", "SYMBOL \n", "SYMBOL*", "SECURITY"):
            symbol_col = col
            break

    if symbol_col is None:
        # Fallback to column 0 if specific header name is not matched
        symbol_col = df.columns[0]
        logger.info("Using first column '%s' as SYMBOL column.", symbol_col)
    else:
        logger.info("Found designated SYMBOL column: '%s'", symbol_col)

    raw_symbols = df[symbol_col].dropna().astype(str).tolist()

    # Clean whitespace, discard total/header rows, and deduplicate preserving order
    symbols: List[str] = []
    seen = set()

    for item in raw_symbols:
        cleaned = item.strip().upper()
        if (
            cleaned
            and cleaned not in seen
            and cleaned not in ("TOTAL", "SYMBOL", "INDEX", "N/A", "NAN")
        ):
            seen.add(cleaned)
            symbols.append(cleaned)

    return symbols


def _perform_single_scrape_attempt() -> Dict[str, Any]:
    """Execute a single browser automation run to fetch and parse the CSV.

    Raises:
        NSEBlockedError, NSEPageStructureError, NSEDownloadError, NSEScrapeError
    """
    temp_dir = Config.TEMP_DIR
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_csv_file: Optional[Path] = None

    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    temp_csv_file = temp_dir / f"volume_gainers_{timestamp_str}.csv"

    playwright_instance = None
    browser = None
    context = None

    try:
        playwright_instance = sync_playwright().start()
        
        # Launch Chromium with anti-detection options
        browser = playwright_instance.chromium.launch(
            headless=Config.HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-infobars",
            ],
        )

        context = browser.new_context(
            user_agent=Config.USER_AGENT,
            viewport=Config.VIEWPORT,
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
        page.set_default_timeout(Config.BROWSER_TIMEOUT_MS)

        # Step 1: Establish session cookies by navigating to the NSE homepage first
        logger.info("Scraper Step 1: Navigating to NSE home page to establish session cookies...")
        try:
            home_response = page.goto(
                Config.NSE_BASE_URL,
                wait_until="domcontentloaded",
                timeout=Config.BROWSER_TIMEOUT_MS,
            )
            if home_response and home_response.status in (403, 429, 503):
                raise NSEBlockedError(
                    f"NSE India blocked initial homepage request with HTTP {home_response.status}."
                )
        except PlaywrightTimeoutError as e:
            logger.warning("Homepage initial load timeout (continuing anyway): %s", e)
        except Exception as e:
            if isinstance(e, NSEBlockedError):
                raise
            logger.warning("Homepage session load encountered an error: %s", e)

        # Brief delay to allow initial session cookies/storage to settle
        time.sleep(1.5)

        # Step 2: Navigate to the Volume Gainers & Spurts market data page
        logger.info(
            "Scraper Step 2: Navigating to Volume Gainers page: %s",
            Config.NSE_VOLUME_GAINERS_URL,
        )
        page_response = page.goto(
            Config.NSE_VOLUME_GAINERS_URL,
            wait_until="domcontentloaded",
            timeout=Config.BROWSER_TIMEOUT_MS,
        )

        if page_response and page_response.status in (403, 429, 503):
            raise NSEBlockedError(
                f"NSE India blocked access to the Volume Gainers page with HTTP {page_response.status}."
            )

        # Step 3: Wait explicitly for the data table to be rendered by JavaScript
        logger.info("Scraper Step 3: Waiting for live data table to render in DOM...")
        table_found = False
        for selector in TABLE_CONTAINER_SELECTORS:
            try:
                page.wait_for_selector(selector, state="visible", timeout=12000)
                logger.info("Data table found via selector '%s'.", selector)
                table_found = True
                break
            except PlaywrightTimeoutError:
                continue

        if not table_found:
            logger.warning(
                "Specific table selector not detected in 12s; checking page content..."
            )
            page_text = page.content()
            if "Access Denied" in page_text or "403 Forbidden" in page_text:
                raise NSEBlockedError("NSE returned 'Access Denied' / Blocked page content.")

        # Brief stabilization pause
        time.sleep(1.0)

        # Step 4: Locate the Download CSV button
        logger.info("Scraper Step 4: Locating 'Download CSV' button...")
        download_btn = None
        matched_selector = None

        for selector in DOWNLOAD_BUTTON_SELECTORS:
            try:
                locator = page.locator(selector).first
                if locator.is_visible(timeout=2000):
                    download_btn = locator
                    matched_selector = selector
                    logger.info("Found download trigger using selector: '%s'", matched_selector)
                    break
            except Exception:
                continue

        if download_btn is None:
            # Fallback: scan for any element containing 'csv' or 'download' in text or id
            try:
                fallback_locator = page.locator("a:text-matches('CSV|Download', 'i')").first
                if fallback_locator.is_visible(timeout=3000):
                    download_btn = fallback_locator
                    logger.info("Found download trigger using regex text fallback.")
            except Exception:
                pass

        if download_btn is None:
            raise NSEPageStructureError(
                "Could not locate the 'Download CSV' button on the NSE Volume Gainers page. "
                "NSE page structure or UI classes may have changed."
            )

        # Step 5: Capture the file download via Playwright's expect_download context manager
        logger.info("Scraper Step 5: Triggering CSV download...")
        try:
            with page.expect_download(timeout=20000) as download_info:
                download_btn.click(force=True)
            download = download_info.value
            download.save_as(str(temp_csv_file))
            logger.info("CSV successfully saved to temp file: %s", temp_csv_file)
        except PlaywrightTimeoutError as e:
            raise NSEDownloadError(
                "Timed out waiting for NSE to initiate the CSV download after clicking the button."
            ) from e
        except Exception as e:
            raise NSEDownloadError(f"Unexpected error while capturing download: {e}") from e

        # Step 6: Parse extracted symbols from the CSV
        logger.info("Scraper Step 6: Parsing symbols from downloaded CSV...")
        symbols = _parse_symbols_from_csv(temp_csv_file)
        fetched_at = datetime.now(timezone.utc).isoformat()

        return {
            "symbols": symbols,
            "count": len(symbols),
            "fetched_at": fetched_at,
        }

    finally:
        # Step 7: Cleanup temp files and close all Playwright browser instances
        if temp_csv_file and temp_csv_file.exists():
            try:
                temp_csv_file.unlink()
                logger.debug("Cleaned up temporary CSV file: %s", temp_csv_file)
            except Exception as e:
                logger.warning("Could not delete temp CSV file %s: %s", temp_csv_file, e)

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


def scrape_volume_gainers() -> Dict[str, Any]:
    """Scrapes the latest NSE Volume Gainers dataset with automatic retry logic.

    Returns:
        Dict containing:
        - symbols: List[str] of extracted stock symbols
        - count: int total count of symbols
        - fetched_at: ISO timestamp of scrape completion
        - duration_seconds: float total execution time

    Raises:
        NSEScrapeError: If all retry attempts fail with descriptive error details.
    """
    max_retries = max(1, Config.MAX_RETRIES + 1)
    last_exception: Optional[Exception] = None
    start_time = time.time()

    logger.info("Initiating scrape for NSE Volume Gainers (max attempts: %d)...", max_retries)

    for attempt in range(1, max_retries + 1):
        attempt_start = time.time()
        logger.info("--- Scrape Attempt %d of %d ---", attempt, max_retries)

        try:
            result = _perform_single_scrape_attempt()
            duration = round(time.time() - start_time, 2)
            result["duration_seconds"] = duration
            logger.info(
                "Scrape successful on attempt %d: Extracted %d symbols in %.2fs.",
                attempt,
                result["count"],
                duration,
            )
            return result

        except (NSEBlockedError, NSEPageStructureError, NSEDownloadError, NSEScrapeError) as e:
            last_exception = e
            logger.warning("Attempt %d failed with error: %s", attempt, str(e))
        except Exception as e:
            last_exception = NSEScrapeError(f"Unexpected error during scraping attempt {attempt}: {e}")
            logger.error("Attempt %d failed with unhandled exception: %s", attempt, str(e), exc_info=True)

        # Delay before next attempt if retries remain
        if attempt < max_retries:
            delay = Config.RETRY_DELAY_SECONDS * attempt
            logger.info("Waiting %d seconds before retrying scrape...", delay)
            time.sleep(delay)

    total_duration = round(time.time() - start_time, 2)
    error_message = (
        f"Failed to scrape NSE Volume Gainers after {max_retries} attempts "
        f"(total elapsed: {total_duration}s). Last error: {last_exception}"
    )
    logger.error(error_message)
    raise NSEScrapeError(error_message) from last_exception


# =====================================================================
# Extensibility Stubs for Future Data Sources (Task 2, Task 3, etc.)
# =====================================================================
#
# TODO (Task 2): Implement `scrape_52_week_highs()`
# URL: https://www.nseindia.com/market-data/52-week-high-equity-market
# def scrape_52_week_highs() -> Dict[str, Any]:
#     """Scrape 52-week high symbols from NSE."""
#     pass
#
# TODO (Task 3): Implement `scrape_fno_participants()`
# URL: https://www.nseindia.com/all-reports (Participant-wise Open Interest)
# def scrape_fno_participants() -> Dict[str, Any]:
#     """Scrape participant-wise open interest data."""
#     pass
#
# TODO: Implement `analyze_cross_source_signals()`
# def analyze_cross_source_signals(volume_symbols: List[str], breakout_symbols: List[str]) -> List[str]:
#     """Find high-conviction intersection of volume gainers + 52-week breakout candidates."""
#     return list(set(volume_symbols).intersection(set(breakout_symbols)))
