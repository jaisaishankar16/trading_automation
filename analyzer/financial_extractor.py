"""Financial Data Extractor for Annual Report PDFs.

Orchestrates:
1. Local Text & OCR extraction per page (PyMuPDF + Tesseract fallback) with text caching.
2. Regex section detection (Consolidated preferred over Standalone).
3. Regex value extraction for Balance Sheet (Total Assets, Total Current Liabilities) and P&L (Revenue, Expenses).
4. Quota-aware Groq fallback ONLY for missing fields when quota permits.
5. Post-processing, validation, and provenance attribution.
"""

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from analyzer.groq_fallback import fill_gaps_via_groq, quota_manager
from analyzer.local_extractor import (
    _clean_number,
    _is_valid_expense_name,
    extract_text_by_page,
    extract_values_regex,
    find_statement_sections,
)

logger = logging.getLogger("financial_extractor")


def _build_context_for_groq(pages_text: Dict[int, str], section_info: Dict[str, Any], max_chars: int = 8000) -> str:
    """Build condensed text context from located statement pages for AI gap-filling."""
    bs_pages = section_info.get("balance_sheet_pages", [])
    pnl_pages = section_info.get("pnl_pages", [])
    target_pages = sorted(set(bs_pages + pnl_pages))

    chunks: List[str] = []
    for p in target_pages:
        txt = pages_text.get(p, "").strip()
        if txt:
            chunks.append(f"--- Page {p} ---\n{txt}")

    combined = "\n\n".join(chunks)
    if len(combined) > 200:
        return combined[:max_chars]

    # If no sections located, grab pages with financial keywords
    kw_chunks: List[str] = []
    keywords = ["balance sheet", "profit and loss", "profit & loss", "total assets", "revenue from operations", "expenses"]
    for p in sorted(pages_text.keys()):
        txt = pages_text.get(p, "")
        if any(kw in txt.lower() for kw in keywords):
            kw_chunks.append(f"--- Page {p} ---\n{txt.strip()}")
            if sum(len(c) for c in kw_chunks) >= max_chars:
                break

    if kw_chunks:
        return "\n\n".join(kw_chunks)[:max_chars]

    # Ultimate fallback: first 4 pages
    first_pages = [f"--- Page {p} ---\n{pages_text[p]}" for p in sorted(pages_text.keys())[:4] if p in pages_text]
    return "\n\n".join(first_pages)[:max_chars]


def extract_financials(
    symbol: str,
    pdf_path: Path,
    progress_callback: Optional[Callable[..., Any]] = None,
    groq_available: bool = True,
) -> Dict[str, Any]:
    """Execute complete financial extraction pipeline.

    1. Local text extraction with OCR fallback per page.
    2. Regex section detection.
    3. Regex figure extraction.
    4. Narrow Groq fallback for missing fields if available.
    5. Expense cleaning and validation.
    """
    clean_sym = symbol.strip().upper() if symbol else pdf_path.stem.split("_")[0].upper()
    logger.info("==================== STARTING FINANCIAL EXTRACTION FOR %s ====================", clean_sym)

    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        logger.error("[%s] PDF file missing or 0 bytes: %s", clean_sym, pdf_path)
        return {
            "symbol": clean_sym,
            "total_assets": None,
            "total_assets_source": None,
            "total_current_liabilities": None,
            "total_current_liabilities_source": None,
            "revenue_from_operations": None,
            "revenue_from_operations_source": None,
            "expense_items": [],
            "expense_items_source": None,
            "reported_total_expenses": None,
            "balance_sheet_type_used": "not_found",
            "pnl_type_used": "not_found",
            "status": "incomplete_data",
            "error_message": "Annual Report PDF not available or download was incomplete.",
        }

    # Step 1: Local Text Extraction (PyMuPDF with OCR fallback per page)
    if progress_callback:
        progress_callback(percent=65, step=f"Extracting text for {clean_sym}...", symbol=clean_sym)
    pages_text = extract_text_by_page(pdf_path, symbol=clean_sym)

    # Step 2: Locate Financial Statement Sections
    if progress_callback:
        progress_callback(percent=75, step=f"Locating financial statements for {clean_sym}...", symbol=clean_sym)
    section_info = find_statement_sections(pages_text)

    # Step 3: Regex Value Extraction
    if progress_callback:
        progress_callback(percent=85, step=f"Extracting figures for {clean_sym}...", symbol=clean_sym)
    extracted_data = extract_values_regex(pages_text, section_info)

    # Track field origins
    assets_from_regex = extracted_data.get("total_assets") is not None
    liab_from_regex = extracted_data.get("total_current_liabilities") is not None
    rev_from_regex = extracted_data.get("revenue_from_operations") is not None
    exp_from_regex = bool(extracted_data.get("expense_items"))

    # Identify missing fields
    missing_fields: List[str] = []
    if not assets_from_regex:
        missing_fields.append("total_assets")
    if not liab_from_regex:
        missing_fields.append("total_current_liabilities")
    if not rev_from_regex:
        missing_fields.append("revenue_from_operations")
    if not exp_from_regex:
        missing_fields.append("expense_items")

    # Step 4: Groq fallback if gaps exist AND Groq is available
    groq_used_for: List[str] = []
    if missing_fields and groq_available and quota_manager.is_available:
        logger.info("[%s] Regex left missing fields: %s. Invoking narrow Groq fallback...", clean_sym, missing_fields)
        if progress_callback:
            progress_callback(percent=92, step=f"Filling gaps via AI for {clean_sym}...", symbol=clean_sym)

        context_text = _build_context_for_groq(pages_text, section_info)
        groq_result = fill_gaps_via_groq(clean_sym, missing_fields, context_text)

        if groq_result:
            for field in missing_fields:
                if field in groq_result and groq_result[field] is not None:
                    extracted_data[field] = groq_result[field]
                    groq_used_for.append(field)
                    logger.info("[%s] Filled gap for '%s': %s (source: Groq)", clean_sym, field, groq_result[field])

            if "balance_sheet_type_used" in groq_result and extracted_data.get("balance_sheet_type_used") in ("not_found", None):
                extracted_data["balance_sheet_type_used"] = groq_result["balance_sheet_type_used"]
            if "pnl_type_used" in groq_result and extracted_data.get("pnl_type_used") in ("not_found", None):
                extracted_data["pnl_type_used"] = groq_result["pnl_type_used"]
    elif missing_fields:
        logger.info("[%s] Missing fields: %s, but Groq fallback is disabled/unavailable. Skipping AI call.", clean_sym, missing_fields)

    # Step 5: Post-processing & Validation
    total_assets = _clean_number(extracted_data.get("total_assets"))
    total_cur_liab = _clean_number(extracted_data.get("total_current_liabilities"))
    rev_ops = _clean_number(extracted_data.get("revenue_from_operations"))

    bs_type = extracted_data.get("balance_sheet_type_used", "not_found")
    if bs_type not in ("consolidated", "standalone"):
        bs_type = "consolidated" if (total_assets is not None) else "not_found"

    pnl_type = extracted_data.get("pnl_type_used", "not_found")
    if pnl_type not in ("consolidated", "standalone"):
        pnl_type = "consolidated" if (rev_ops is not None) else "not_found"

    # Clean expense items
    cleaned_expenses: List[Dict[str, Any]] = []
    raw_expenses = extracted_data.get("expense_items") or []
    seen_names = set()

    for item in raw_expenses:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        val = _clean_number(item.get("value"))
        if val is not None and val >= 0 and _is_valid_expense_name(name):
            name_norm = name.lower()
            if name_norm not in seen_names:
                seen_names.add(name_norm)
                cleaned_expenses.append({
                    "name": name,
                    "value": val,
                })

    reported_total_expenses = round(sum(it["value"] for it in cleaned_expenses), 2) if cleaned_expenses else None

    # Determine status & error message
    status = "success"
    missing_reasons: List[str] = []

    if total_assets is None:
        missing_reasons.append("Missing Total Assets")
    if total_cur_liab is None:
        missing_reasons.append("Missing Total Current Liabilities")
    if rev_ops is None:
        missing_reasons.append("Missing Revenue from Operations")

    error_msg = None
    if missing_reasons:
        status = "incomplete_data"
        error_msg = "; ".join(missing_reasons)

    # Determine source attribution
    def _source_for(field_name: str, val: Any, from_regex: bool) -> Optional[str]:
        if val is None:
            return None
        if from_regex:
            return "Regex Extractor"
        if field_name in groq_used_for:
            return "Groq Fallback"
        return "Regex Extractor"

    total_assets_src = _source_for("total_assets", total_assets, assets_from_regex)
    total_liab_src = _source_for("total_current_liabilities", total_cur_liab, liab_from_regex)
    rev_src = _source_for("revenue_from_operations", rev_ops, rev_from_regex)
    exp_src = _source_for("expense_items", cleaned_expenses if cleaned_expenses else None, exp_from_regex)

    result = {
        "symbol": clean_sym,
        "total_assets": total_assets,
        "total_assets_source": total_assets_src,
        "total_current_liabilities": total_cur_liab,
        "total_current_liabilities_source": total_liab_src,
        "revenue_from_operations": rev_ops,
        "revenue_from_operations_source": rev_src,
        "expense_items": cleaned_expenses,
        "expense_items_source": exp_src,
        "reported_total_expenses": reported_total_expenses,
        "balance_sheet_type_used": bs_type,
        "pnl_type_used": pnl_type,
        "status": status,
        "error_message": error_msg,
    }

    logger.info("==================== FINISHED FINANCIAL EXTRACTION FOR %s (status=%s) ====================", clean_sym, status)
    return result
