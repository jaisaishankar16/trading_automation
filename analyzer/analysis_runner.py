"""Financial Analysis Pipeline Orchestrator.

Orchestrates:
1. NSE volume gainers scraping (Task 1).
2. Screener.in Annual Report PDF downloading (Task 2) with timestamped filenames and resumable discovery.
3. PDF financial extraction (Balance Sheet & P&L figures) with font/OCR remediation and AI section finding.
4. Capital Employed calculation, default expense auto-selection (excluding Finance Costs), and automatic ROC calculation.
5. JSON persistence to data/financial_analysis.json and Excel export with ROC (%) column.
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import pandas as pd
from playwright.sync_api import BrowserContext, Page, sync_playwright

from analyzer.pdf_financials import (
    extract_balance_sheet_figures,
    extract_pnl_figures,
)
from analyzer.financial_extractor import extract_financials
from analyzer.groq_fallback import check_groq_quota_preflight
from app.scrapers.nse_volume_gainers import scrape_volume_gainers
from app.storage import storage
from scrape_annual_reports import (
    DELAY_BETWEEN_SYMBOLS_SEC,
    HEADLESS_DEFAULT,
    MIN_VALID_PDF_BYTES,
    USER_AGENT,
    VIEWPORT,
    download_pdf_file,
    extract_latest_annual_report_link,
    find_latest_annual_report_pdf,
    get_pdf_downloaded_at,
    navigate_to_company_page,
)

logger = logging.getLogger("analysis_runner")

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
ANNUAL_REPORTS_DIR = BASE_DIR / "annual_reports"
FINANCIAL_ANALYSIS_FILE = DATA_DIR / "financial_analysis.json"
VOLUME_GAINERS_FILE = DATA_DIR / "volume_gainers.json"


# ---------------------------------------------------------------------------
# Storage Helpers
# ---------------------------------------------------------------------------
def load_financial_analysis() -> List[Dict[str, Any]]:
    """Load the current financial analysis dataset from disk."""
    if not FINANCIAL_ANALYSIS_FILE.exists():
        return []
    try:
        with open(FINANCIAL_ANALYSIS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        elif isinstance(data, dict):
            return data.get("records", [])
        return []
    except Exception as e:
        logger.error("Failed to read financial analysis file %s: %s", FINANCIAL_ANALYSIS_FILE, e)
        return []


def save_financial_analysis(records: List[Dict[str, Any]]) -> None:
    """Save the financial analysis dataset to disk with atomic write."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = FINANCIAL_ANALYSIS_FILE.with_suffix(".tmp")
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
        temp_file.replace(FINANCIAL_ANALYSIS_FILE)
        logger.info("Saved %d financial analysis records to %s", len(records), FINANCIAL_ANALYSIS_FILE)
    except Exception as e:
        logger.error("Failed to save financial analysis data: %s", e, exc_info=True)
        if temp_file.exists():
            try:
                temp_file.unlink()
            except Exception:
                pass
        raise


def get_financial_analysis() -> List[Dict[str, Any]]:
    """Public accessor for the financial analysis records."""
    return load_financial_analysis()


# ---------------------------------------------------------------------------
# Expense Selection & ROC Calculation Helpers (Fix 2 & Fix 3)
# ---------------------------------------------------------------------------
def is_finance_costs_item(name: str) -> bool:
    """Check if an expense line item represents Finance Costs."""
    if not name:
        return False
    patterns = [
        r"\bfinance\s*(?:costs?|charges?)\b",
        r"\bborrowing\s*costs?\b",
        r"\binterest\s*(?:and|&)\s*finance\b",
        r"\binterest\s*(?:expenses?|costs?|charges?)\b",
    ]
    return any(re.search(p, name, re.IGNORECASE) for p in patterns)


def apply_expense_selection(
    symbol: str, selected_expense_names: List[str]
) -> Dict[str, Any]:
    """Update selected expenses for a symbol and recalculate Operating Profit and ROC.

    Args:
        symbol: Stock symbol (e.g. "INFY", "BBTC").
        selected_expense_names: List of expense line item names chosen by user.

    Returns:
        The updated symbol record.
    """
    records = load_financial_analysis()
    clean_sym = symbol.strip().upper()

    target_record: Optional[Dict[str, Any]] = None
    for r in records:
        if r.get("symbol", "").upper() == clean_sym:
            target_record = r
            break

    if target_record is None:
        raise ValueError(f"Symbol '{symbol}' not found in financial analysis records.")

    expense_items = target_record.get("expense_items", [])
    selected_names_normalized = {name.strip().lower() for name in selected_expense_names}

    selected_total = 0.0
    for item in expense_items:
        item_name = item.get("name", "").strip()
        is_selected = item_name.lower() in selected_names_normalized or item_name in selected_expense_names
        item["selected"] = is_selected
        if is_selected:
            selected_total += float(item.get("value") or 0.0)

    target_record["selected_expense_total"] = round(selected_total, 2)
    target_record["is_default_selection"] = False  # Mark as user-adjusted

    # Calculate Operating Profit = Revenue from Operations - Selected Expenses
    rev_ops = target_record.get("revenue_from_operations")
    if rev_ops is not None:
        op_profit = round(float(rev_ops) - selected_total, 2)
        target_record["operating_profit"] = op_profit

        # Calculate ROC = (Operating Profit / Capital Employed) * 100
        cap_emp = target_record.get("capital_employed")
        if cap_emp is not None and float(cap_emp) != 0.0:
            roc = round((op_profit / float(cap_emp)) * 100.0, 2)
            target_record["roc"] = roc
            target_record["status"] = "complete"
            target_record["error_message"] = None
        else:
            target_record["roc"] = None
            target_record["status"] = "incomplete_data"
            target_record["error_message"] = "Cannot calculate ROC: Capital Employed is zero or missing."
    else:
        target_record["operating_profit"] = None
        target_record["roc"] = None
        target_record["status"] = "incomplete_data"
        target_record["error_message"] = "Cannot calculate Operating Profit: Revenue from Operations is missing."

    target_record["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_financial_analysis(records)
    return target_record


# ---------------------------------------------------------------------------
# Excel Export Helper (Fix 3: ROC column header)
# ---------------------------------------------------------------------------
def export_financial_analysis_excel(output_path: Optional[Path] = None) -> Path:
    """Export the current financial analysis dataset to an Excel workbook (.xlsx).

    Columns:
    - SYMBOL
    - Company Name
    - Total Assets
    - Total Current Liabilities
    - Capital Employed
    - Revenue from Operations
    - Selected Expenses
    - Operating Profit
    - ROC (%)
    - Status
    """
    records = load_financial_analysis()
    if output_path is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        output_path = DATA_DIR / "financial_analysis_report.xlsx"

    rows = []
    for r in records:
        selected_items = [
            f"{it['name']} ({it['value']:,.2f})"
            for it in r.get("expense_items", [])
            if it.get("selected")
        ]
        if selected_items:
            sel_exp_str = ", ".join(selected_items)
            if r.get("selected_expense_total") is not None:
                sel_exp_str += f" | Total: {r['selected_expense_total']:,.2f}"
        else:
            sel_exp_str = "None selected"

        rows.append({
            "SYMBOL": r.get("symbol", ""),
            "Company Name": r.get("company_name", r.get("symbol", "")),
            "Total Assets": r.get("total_assets"),
            "Total Current Liabilities": r.get("total_current_liabilities"),
            "Capital Employed": r.get("capital_employed"),
            "Revenue from Operations": r.get("revenue_from_operations"),
            "Selected Expenses": sel_exp_str,
            "Operating Profit": r.get("operating_profit"),
            "ROC (%)": r.get("roc"),
            "Status": r.get("status", ""),
        })

    df = pd.DataFrame(rows)
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Financial Analysis")

    logger.info("Exported financial analysis report to %s", output_path)
    return output_path


# ---------------------------------------------------------------------------
# Full Pipeline Orchestration (Fixes 0, 1, 2, 3, 4, 5)
# ---------------------------------------------------------------------------
def run_full_pipeline(
    progress_callback: Optional[Callable[..., None]] = None
) -> List[Dict[str, Any]]:
    """Execute the end-to-end trading and financial analysis pipeline.

    Steps:
    1. Scrape NSE Volume Gainers & Spurts -> symbols list.
    2. Download latest Annual Report PDF for each symbol via screener.in (timestamped filenames & resumable).
    3. Extract Balance Sheet and P&L figures with font/OCR remediation and AI section locator.
    4. Calculate Capital Employed, auto-select expenses (exclude Finance Costs), and compute ROC.
    5. Save results to data/financial_analysis.json.
    """
    def report_progress(
        percent: int,
        step: str,
        symbol: Optional[str] = None,
        status: str = "running",
        error: Optional[str] = None,
    ) -> None:
        if progress_callback:
            try:
                progress_callback(
                    percent=percent,
                    step=step,
                    symbol=symbol,
                    status=status,
                    error=error,
                )
            except Exception as e:
                logger.warning("Error in progress callback: %s", e)

    start_time = datetime.now(timezone.utc)
    logger.info("Starting Full Financial Analysis Pipeline...")
    report_progress(5, "Fetching latest NSE volume gainers data...", None)

    # 0. Load existing records into memory to carry over user custom selections
    existing_records = {r.get("symbol", "").upper(): r for r in load_financial_analysis()}
    save_financial_analysis([])
    logger.info("Reset financial_analysis.json before starting full pipeline run.")

    # -----------------------------------------------------------------------
    # Step 1: Scrape NSE Volume Gainers
    # -----------------------------------------------------------------------
    symbols: List[str] = []
    try:
        nse_result = scrape_volume_gainers()
        symbols = nse_result.get("symbols", [])
        fetched_at = nse_result.get("fetched_at", datetime.now(timezone.utc).isoformat())

        storage.save_volume_gainers(
            symbols=symbols,
            count=len(symbols),
            fetched_at=fetched_at,
        )
        logger.info("Step 1 Complete: Fetched %d symbols from NSE: %s", len(symbols), symbols)
        report_progress(15, "Fetching latest NSE volume gainers data...", None)

    except Exception as e:
        logger.error("Step 1 failed (NSE scrape error): %s", e, exc_info=True)
        if VOLUME_GAINERS_FILE.exists():
            try:
                with open(VOLUME_GAINERS_FILE, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                symbols = cached.get("symbols", [])
                logger.warning("Falling back to %d cached symbols from %s", len(symbols), VOLUME_GAINERS_FILE)
            except Exception:
                pass

        if not symbols:
            report_progress(100, f"Pipeline failed: {e}", None, status="error", error=str(e))
            raise RuntimeError(f"Could not retrieve stock symbols from NSE: {e}")

    ANNUAL_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    total_symbols = len(symbols)

    # -----------------------------------------------------------------------
    # Step 2: Download Annual Reports from Screener.in (Fix 4)
    # -----------------------------------------------------------------------
    logger.info("Step 2: Processing Annual Report downloads for %d symbols...", total_symbols)
    playwright_inst = None
    browser = None
    context = None
    page = None
    logged_search_selector = [False]

    downloaded_paths: Dict[str, Path] = {}

    try:
        playwright_inst = sync_playwright().start()
        browser = playwright_inst.chromium.launch(
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

            # Progress calculation: 15% -> 60%
            pct = 15 + int((idx / total_symbols) * 45)
            report_progress(
                pct,
                f"Downloading Annual Report for {clean_symbol} ({idx}/{total_symbols})...",
                clean_symbol,
            )

            # Check resumability using glob search (Fix 4)
            existing_pdf = find_latest_annual_report_pdf(clean_symbol, ANNUAL_REPORTS_DIR)
            if existing_pdf:
                downloaded_paths[clean_symbol] = existing_pdf
                logger.info("[%s] PDF already present (%s, %.2f KB), skipping download.", clean_symbol, existing_pdf.name, existing_pdf.stat().st_size / 1024.0)
                continue

            # Generate timestamped filename for new download (Fix 4)
            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            target_pdf_path = ANNUAL_REPORTS_DIR / f"{clean_symbol}_annual_report_{timestamp_str}.pdf"

            try:
                if not navigate_to_company_page(page, clean_symbol, logged_search_selector):
                    logger.warning("[%s] Navigation to company page failed on Screener.in.", clean_symbol)
                    continue

                year, report_text, pdf_url = extract_latest_annual_report_link(page, clean_symbol)
                if not pdf_url:
                    logger.warning("[%s] No Annual Report links found on Screener page.", clean_symbol)
                    continue

                download_ok = download_pdf_file(context, page, clean_symbol, pdf_url, target_pdf_path)
                if download_ok and target_pdf_path.exists() and target_pdf_path.stat().st_size > MIN_VALID_PDF_BYTES:
                    downloaded_paths[clean_symbol] = target_pdf_path
                    logger.info("[%s] Successfully downloaded Annual Report PDF (Year %s) -> %s", clean_symbol, year, target_pdf_path.name)

            except Exception as e:
                logger.warning("[%s] Error during Screener download: %s", clean_symbol, e)

            time.sleep(DELAY_BETWEEN_SYMBOLS_SEC)

    finally:
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
        if playwright_inst:
            try:
                playwright_inst.stop()
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # Step 3 & 4: Extract Financial Figures & Calculate ROC
    # -----------------------------------------------------------------------
    logger.info("Running Groq AI quota pre-flight check before extraction...")
    groq_available = check_groq_quota_preflight()

    logger.info("Step 3: Extracting Financial Figures from PDFs for %d NSE symbols (Groq available: %s)...", total_symbols, groq_available)
    final_records: List[Dict[str, Any]] = []

    for idx, symbol in enumerate(symbols, 1):
        clean_symbol = symbol.strip().upper()
        target_pdf_path = find_latest_annual_report_pdf(clean_symbol, ANNUAL_REPORTS_DIR)

        # Progress calculation: 60% -> 95%
        pct = 60 + int((idx / total_symbols) * 35)
        report_progress(
            pct,
            f"Extracting key financial information for {clean_symbol} ({idx}/{total_symbols})...",
            clean_symbol,
        )

        company_name = clean_symbol
        total_assets = None
        total_assets_source = None
        total_cur_liab = None
        total_cur_liab_source = None
        capital_employed = None
        rev_ops = None
        rev_ops_source = None
        expense_items: List[Dict[str, Any]] = []
        expense_items_source = None
        status = "incomplete_data"
        error_msg = None
        bs_type_used = "not_found"
        pnl_type_used = "not_found"
        downloaded_at = None
        is_default_sel = True

        if not target_pdf_path or not target_pdf_path.exists() or target_pdf_path.stat().st_size < MIN_VALID_PDF_BYTES:
            status = "incomplete_data"
            error_msg = "Annual Report PDF not available or download was incomplete."
            logger.warning("[%s] Annual Report PDF missing for analysis.", clean_symbol)
        else:
            downloaded_at = get_pdf_downloaded_at(target_pdf_path)

            try:
                # Extract Financials using Local OCR+Regex with Quota-Aware Groq Fallback
                rag_data = extract_financials(
                    clean_symbol,
                    target_pdf_path,
                    progress_callback=lambda percent, step, symbol=clean_symbol: report_progress(
                        60 + int((idx / total_symbols) * 35 * (percent / 100)),
                        step,
                        symbol,
                    ),
                    groq_available=groq_available,
                )
                total_assets = rag_data.get("total_assets")
                total_assets_source = rag_data.get("total_assets_source")
                total_cur_liab = rag_data.get("total_current_liabilities")
                total_cur_liab_source = rag_data.get("total_current_liabilities_source")
                bs_type_used = rag_data.get("balance_sheet_type_used", "not_found")
                rev_ops = rag_data.get("revenue_from_operations")
                rev_ops_source = rag_data.get("revenue_from_operations_source")
                extracted_expenses = rag_data.get("expense_items", [])
                expense_items_source = rag_data.get("expense_items_source")
                pnl_type_used = rag_data.get("pnl_type_used", "not_found")

                # Calculate Capital Employed
                if total_assets is not None and total_cur_liab is not None:
                    capital_employed = round(float(total_assets) - float(total_cur_liab), 2)
                else:
                    missing_reasons = []
                    if rag_data.get("error_message"):
                        missing_reasons.append(rag_data["error_message"])
                    else:
                        if total_assets is None:
                            missing_reasons.append("Missing Total Assets")
                        if total_cur_liab is None:
                            missing_reasons.append("Missing Total Current Liabilities")
                    error_msg = "; ".join(missing_reasons)

                if rev_ops is None:
                    if rag_data.get("error_message") and rag_data["error_message"] not in (error_msg or ""):
                        error_msg = f"{error_msg}; {rag_data['error_message']}" if error_msg else rag_data["error_message"]

                # 4. Fix 2: Auto-select expenses by default (exclude Finance Costs)
                prev_record = existing_records.get(clean_symbol)
                if prev_record:
                    company_name = prev_record.get("company_name", clean_symbol)

                # Check if previous manual selection existed (user explicitly adjusted)
                if prev_record and prev_record.get("is_default_selection") is False:
                    is_default_sel = False
                    prev_selected_names = {
                        item.get("name", "").strip().lower()
                        for item in prev_record.get("expense_items", [])
                        if item.get("selected")
                    }
                    for exp in extracted_expenses:
                        exp["selected"] = exp.get("name", "").strip().lower() in prev_selected_names
                else:
                    # Default selection: select ALL items EXCEPT Finance Costs
                    is_default_sel = True
                    for exp in extracted_expenses:
                        exp_name = exp.get("name", "").strip()
                        exp["selected"] = not is_finance_costs_item(exp_name)

                expense_items = extracted_expenses

            except Exception as e:
                logger.error("[%s] Financial extraction failed: %s", clean_symbol, e, exc_info=True)
                status = "error"
                error_msg = f"Extraction error: {e}"

        # 5. Fix 2: Auto-calculate Operating Profit and ROC during pipeline run
        selected_expense_total = None
        operating_profit = None
        roc = None

        if expense_items:
            selected_expense_total = round(
                sum(float(it.get("value") or 0.0) for it in expense_items if it.get("selected")), 2
            )

        if rev_ops is not None and selected_expense_total is not None:
            operating_profit = round(float(rev_ops) - selected_expense_total, 2)
            if capital_employed is not None and float(capital_employed) != 0.0:
                roc = round((operating_profit / float(capital_employed)) * 100.0, 2)
                status = "complete"
                error_msg = None
            else:
                status = "incomplete_data"
                if not error_msg:
                    error_msg = "Cannot calculate ROC: Capital Employed is zero or missing."
        elif status != "error":
            status = "incomplete_data"
            if not error_msg:
                error_msg = "Incomplete data for Operating Profit calculation."

        # Build record
        record: Dict[str, Any] = {
            "symbol": clean_symbol,
            "company_name": company_name,
            "total_assets": total_assets,
            "total_assets_source": total_assets_source,
            "total_current_liabilities": total_cur_liab,
            "total_current_liabilities_source": total_cur_liab_source,
            "balance_sheet_type_used": bs_type_used,
            "capital_employed": capital_employed,
            "revenue_from_operations": rev_ops,
            "revenue_from_operations_source": rev_ops_source,
            "expense_items": expense_items,
            "expense_items_source": expense_items_source,
            "pnl_type_used": pnl_type_used,
            "selected_expense_total": selected_expense_total,
            "operating_profit": operating_profit,
            "roc": roc,
            "is_default_selection": is_default_sel,
            "downloaded_at": downloaded_at,
            "status": status,
            "error_message": error_msg,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        final_records.append(record)

    # -----------------------------------------------------------------------
    # Step 5: Save Results & Final Progress Notification
    # -----------------------------------------------------------------------
    report_progress(98, "Finalizing results...", None)
    save_financial_analysis(final_records)
    duration_sec = round((datetime.now(timezone.utc) - start_time).total_seconds(), 1)

    successful_count = sum(1 for r in final_records if r.get("status") == "complete")
    failed_records = [r for r in final_records if r.get("status") in ("error", "incomplete_data")]

    logger.info(
        "Full Pipeline Complete: NSE symbol count = %d, Records written = %d (Complete: %d, Incomplete/Failed: %d) in %.1fs",
        len(symbols),
        len(final_records),
        successful_count,
        len(failed_records),
        duration_sec,
    )
    report_progress(100, "Finalizing results...", None, status="complete")

    return final_records


# ---------------------------------------------------------------------------
# Re-analyze Existing PDFs without re-downloading (Fixes 0, 1, 2, 3, 4, 5)
# ---------------------------------------------------------------------------
def reanalyze_existing_reports(
    symbols: Optional[List[str]] = None,
    progress_callback: Optional[Callable[..., None]] = None,
) -> List[Dict[str, Any]]:
    """Re-run financial extraction strictly on existing annual report PDFs in annual_reports/.

    Avoids re-scraping NSE or re-downloading from screener.in.
    Updates data/financial_analysis.json directly.
    """
    logger.info("Starting re-analysis of existing annual reports...")
    all_pdf_files = list(ANNUAL_REPORTS_DIR.glob("*.pdf"))
    if not all_pdf_files:
        logger.warning("No PDF annual reports found in %s", ANNUAL_REPORTS_DIR)
        return []

    # Deduplicate symbols by resolving newest PDF per symbol
    discovered_symbols = set()
    for p in all_pdf_files:
        stem = p.stem.split("_annual_report")[0].upper()
        if stem:
            discovered_symbols.add(stem)

    if symbols:
        filter_syms = {s.strip().upper() for s in symbols}
        target_symbols = [s for s in discovered_symbols if s in filter_syms]
    else:
        target_symbols = sorted(discovered_symbols)

    # Run preflight Groq check once for reanalyze run
    groq_available = check_groq_quota_preflight()

    existing_records = {r.get("symbol", "").upper(): r for r in load_financial_analysis()}
    final_records: List[Dict[str, Any]] = []
    total = len(target_symbols)

    for idx, clean_symbol in enumerate(target_symbols, 1):
        target_pdf_path = find_latest_annual_report_pdf(clean_symbol, ANNUAL_REPORTS_DIR)
        logger.info("[%d/%d] Re-analyzing financials for %s (PDF: %s)...", idx, total, clean_symbol, target_pdf_path.name if target_pdf_path else "None")

        company_name = clean_symbol
        total_assets = None
        total_assets_source = None
        total_cur_liab = None
        total_cur_liab_source = None
        capital_employed = None
        rev_ops = None
        rev_ops_source = None
        expense_items: List[Dict[str, Any]] = []
        expense_items_source = None
        status = "incomplete_data"
        error_msg = None
        bs_type_used = "not_found"
        pnl_type_used = "not_found"
        downloaded_at = None
        is_default_sel = True

        if not target_pdf_path or not target_pdf_path.exists() or target_pdf_path.stat().st_size < MIN_VALID_PDF_BYTES:
            status = "incomplete_data"
            error_msg = "Annual Report PDF not available or download was incomplete."
        else:
            downloaded_at = get_pdf_downloaded_at(target_pdf_path)

            try:
                # Extract Financials using Local OCR+Regex with Quota-Aware Groq Fallback
                rag_data = extract_financials(
                    clean_symbol,
                    target_pdf_path,
                    progress_callback=progress_callback,
                    groq_available=groq_available,
                )
                total_assets = rag_data.get("total_assets")
                total_assets_source = rag_data.get("total_assets_source")
                total_cur_liab = rag_data.get("total_current_liabilities")
                total_cur_liab_source = rag_data.get("total_current_liabilities_source")
                bs_type_used = rag_data.get("balance_sheet_type_used", "not_found")
                rev_ops = rag_data.get("revenue_from_operations")
                rev_ops_source = rag_data.get("revenue_from_operations_source")
                extracted_expenses = rag_data.get("expense_items", [])
                expense_items_source = rag_data.get("expense_items_source")
                pnl_type_used = rag_data.get("pnl_type_used", "not_found")

                # Calculate Capital Employed
                if total_assets is not None and total_cur_liab is not None:
                    capital_employed = round(float(total_assets) - float(total_cur_liab), 2)
                else:
                    missing_reasons = []
                    if rag_data.get("error_message"):
                        missing_reasons.append(rag_data["error_message"])
                    else:
                        if total_assets is None:
                            missing_reasons.append("Missing Total Assets")
                        if total_cur_liab is None:
                            missing_reasons.append("Missing Total Current Liabilities")
                    error_msg = "; ".join(missing_reasons)

                if rev_ops is None:
                    if rag_data.get("error_message") and rag_data["error_message"] not in (error_msg or ""):
                        error_msg = f"{error_msg}; {rag_data['error_message']}" if error_msg else rag_data["error_message"]

                # 4. Fix 2: Auto-select expenses by default (exclude Finance Costs)
                prev_record = existing_records.get(clean_symbol)
                if prev_record:
                    company_name = prev_record.get("company_name", clean_symbol)

                if prev_record and prev_record.get("is_default_selection") is False:
                    is_default_sel = False
                    prev_selected_names = {
                        item.get("name", "").strip().lower()
                        for item in prev_record.get("expense_items", [])
                        if item.get("selected")
                    }
                    for exp in extracted_expenses:
                        exp["selected"] = exp.get("name", "").strip().lower() in prev_selected_names
                else:
                    is_default_sel = True
                    for exp in extracted_expenses:
                        exp_name = exp.get("name", "").strip()
                        exp["selected"] = not is_finance_costs_item(exp_name)

                expense_items = extracted_expenses

            except Exception as e:
                logger.error("[%s] Re-analysis failed: %s", clean_symbol, e, exc_info=True)
                status = "error"
                error_msg = f"Extraction error: {e}"

        # 5. Fix 2: Auto-calculate Operating Profit and ROC
        selected_expense_total = None
        operating_profit = None
        roc = None

        if expense_items:
            selected_expense_total = round(
                sum(float(it.get("value") or 0.0) for it in expense_items if it.get("selected")), 2
            )

        if rev_ops is not None and selected_expense_total is not None:
            operating_profit = round(float(rev_ops) - selected_expense_total, 2)
            if capital_employed is not None and float(capital_employed) != 0.0:
                roc = round((operating_profit / float(capital_employed)) * 100.0, 2)
                status = "complete"
                error_msg = None
            else:
                status = "incomplete_data"
                if not error_msg:
                    error_msg = "Cannot calculate ROC: Capital Employed is zero or missing."
        elif status != "error":
            status = "incomplete_data"
            if not error_msg:
                error_msg = "Incomplete data for Operating Profit calculation."

        record: Dict[str, Any] = {
            "symbol": clean_symbol,
            "company_name": company_name,
            "total_assets": total_assets,
            "total_assets_source": total_assets_source,
            "total_current_liabilities": total_cur_liab,
            "total_current_liabilities_source": total_cur_liab_source,
            "balance_sheet_type_used": bs_type_used,
            "capital_employed": capital_employed,
            "revenue_from_operations": rev_ops,
            "revenue_from_operations_source": rev_ops_source,
            "expense_items": expense_items,
            "expense_items_source": expense_items_source,
            "pnl_type_used": pnl_type_used,
            "selected_expense_total": selected_expense_total,
            "operating_profit": operating_profit,
            "roc": roc,
            "is_default_selection": is_default_sel,
            "downloaded_at": downloaded_at,
            "status": status,
            "error_message": error_msg,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        final_records.append(record)

    if symbols:
        for rec in final_records:
            existing_records[rec["symbol"].upper()] = rec
        records_to_save = list(existing_records.values())
    else:
        records_to_save = final_records

    save_financial_analysis(records_to_save)
    logger.info("Re-analysis complete! Saved %d records in %s", len(records_to_save), FINANCIAL_ANALYSIS_FILE)
    return records_to_save


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Financial Analysis Pipeline & Re-analyzer CLI")
    parser.add_argument("--reanalyze", action="store_true", help="Re-analyze existing downloaded PDFs without scraping/downloading")
    parser.add_argument("--symbols", nargs="+", help="Specific symbols to process")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if args.reanalyze:
        reanalyze_existing_reports(symbols=args.symbols)
    else:
        run_full_pipeline()
