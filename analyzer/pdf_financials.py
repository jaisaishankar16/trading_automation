"""PDF Financial Statement Extraction Module (AI-First Architecture).

Extracts Balance Sheet figures (Total Assets, Total Current Liabilities)
and Profit & Loss figures (Revenue from Operations, individual Expense line items)
from company Annual Report PDFs with:
1. Multi-tier font/OCR remediation (pdfplumber -> PyMuPDF -> pytesseract OCR).
2. AI-First statement section discovery (Groq LLM) with section caching and Consolidated-over-Standalone priority.
3. AI-First figure and line-item extraction (Groq LLM) as the sole extraction mechanism.
4. Robust sanity filtering, reconciliation verification, and provenance tracking.
"""

import io
import logging
import os
import re
import shutil
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from PIL import Image
import pdfplumber
import pymupdf  # PyMuPDF
import pypdfium2 as pdfium

from analyzer.llm_extractor import (
    llm_extract_balance_sheet,
    llm_extract_pnl,
    llm_locate_statement_sections,
)

warnings.filterwarnings("ignore")
logging.getLogger("pdfminer").setLevel(logging.ERROR)
logger = logging.getLogger("pdf_financials")


# ---------------------------------------------------------------------------
# Numeric & Text Parsing Helpers
# ---------------------------------------------------------------------------
def parse_numeric_value(raw_val: Any) -> Optional[float]:
    """Parse a numeric string from financial PDF tables into a standard float.

    Handles:
    - Commas in numbers: "12,34,567", "1,234.56"
    - Parentheses indicating negative values: "(1,234)" -> -1234.0, "( 45.60 )" -> -45.6
    - Minus signs: "-1,234.50" -> -1234.5
    - Dash or hyphen representing zero or null: "-" -> None
    """
    if raw_val is None:
        return None

    s = str(raw_val).strip()
    if not s or s in ("-", "—", "–", "N/A", "NA", "Nil", "nil", "."):
        return None

    # Check for negative value in parentheses
    is_negative = False
    if (s.startswith("(") and s.endswith(")")) or s.startswith("-"):
        is_negative = True
        s = re.sub(r"[\(\)\-]", "", s).strip()

    # Extract digits and decimal point
    cleaned = re.sub(r"[^\d.]", "", s)
    if not cleaned or cleaned == ".":
        return None

    try:
        val = float(cleaned)
        return -val if is_negative else val
    except ValueError:
        return None


def extract_numbers_from_text(text: str) -> List[float]:
    """Find and parse all numeric tokens within a line or text snippet."""
    if not text:
        return []
    tokens = re.findall(r"\(?\s*-?\s*\d[\d,]*\.?\d*\s*\)?", text)
    numbers = []
    for tok in tokens:
        v = parse_numeric_value(tok)
        if v is not None:
            numbers.append(v)
    return numbers


def detect_currency_unit(text: str) -> Optional[str]:
    """Detect currency unit mentions (e.g. '₹ in Lakhs', 'Rs. in Crores') on the page."""
    unit_match = re.search(
        r"(?:(?:in\s+)?(?:₹|rs\.?|inr)\s*(?:in\s+)?(?:lakhs?|crores?|millions?|thousands?|billions?)|"
        r"(?:lakhs?|crores?|millions?|thousands?|billions?)\s*(?:of\s+rupees)?)",
        text,
        re.IGNORECASE,
    )
    if unit_match:
        return unit_match.group(0).strip()
    return None


# ---------------------------------------------------------------------------
# Font Garbling Detection & Multi-Tier Clean Text Extraction
# ---------------------------------------------------------------------------
def _is_text_garbled(text: str) -> bool:
    """Check if extracted text is corrupted by embedded font encoding issues or glyph IDs.

    Detects:
    - High count/density of '(cid:XXX)' patterns from unmapped PDF subset fonts.
    - High density of control / unprintable characters (\\x00-\\x1f, \\x80-\\x9f).
    - High density of Greek archaic or Private-Use Area codepoints where Latin letters should appear.
    """
    if not text or not text.strip():
        return True

    # 1. CID pattern density
    cid_matches = re.findall(r"\(cid:\d+\)", text)
    if len(cid_matches) >= 3:
        return True
    if len(text) > 50 and (len(cid_matches) * 8 / len(text)) > 0.02:
        return True

    # 2. Control and corrupted font character density
    total_chars = len(text)
    corrupted_count = 0
    for char in text:
        cp = ord(char)
        # Control characters (excluding standard whitespace \t, \n, \r)
        if (0 <= cp <= 8) or (11 <= cp <= 12) or (14 <= cp <= 31) or (128 <= cp <= 159):
            corrupted_count += 1
        # Greek archaic numerals or PUA glyph substitutions used by broken font CMaps
        elif (0x370 <= cp <= 0x37F) or (0xE000 <= cp <= 0xF8FF) or (0x200 <= cp <= 0x24F and cp not in (0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2013, 0x2014)):
            corrupted_count += 1

    if total_chars > 30 and (corrupted_count / total_chars) > 0.06:
        return True

    return False


def _get_tesseract_cmd() -> Optional[str]:
    """Find the path to the system tesseract binary if installed."""
    # 1. Check environment variable
    env_cmd = os.getenv("TESSERACT_CMD")
    if env_cmd and os.path.isfile(env_cmd):
        return env_cmd

    # 2. Check system PATH
    which_cmd = shutil.which("tesseract")
    if which_cmd:
        return which_cmd

    # 3. Check common Windows installation directories
    common_win_paths = [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        str(Path.home() / "AppData" / "Local" / "Programs" / "Tesseract-OCR" / "tesseract.exe"),
        str(Path.home() / "AppData" / "Local" / "Tesseract-OCR" / "tesseract.exe"),
    ]
    for p in common_win_paths:
        if os.path.isfile(p):
            return p

    return None


def _extract_page_text_ocr(pdf_path: Path, page_idx: int, symbol: str = "") -> str:
    """Render a PDF page to image and perform OCR using pytesseract."""
    try:
        import pytesseract

        tess_cmd = _get_tesseract_cmd()
        if tess_cmd:
            pytesseract.pytesseract.tesseract_cmd = tess_cmd
        else:
            logger.warning(
                "[%s] Tesseract OCR requested for page %d but binary not found in PATH or standard locations. "
                "Please install tesseract-ocr (e.g. `winget install UB-Mannheim.TesseractOCR` or `apt install tesseract-ocr`).",
                symbol,
                page_idx + 1,
            )
            return ""

        # Render page to high-res image using PyMuPDF pixmap (self-contained, no poppler binary needed on Windows)
        doc = pymupdf.open(str(pdf_path))
        if page_idx >= len(doc):
            return ""

        page = doc[page_idx]
        pix = page.get_pixmap(dpi=200)
        img = Image.open(io.BytesIO(pix.tobytes("png")))

        ocr_text = pytesseract.image_to_string(img)
        doc.close()
        return ocr_text or ""

    except Exception as e:
        logger.warning("[%s] OCR extraction failed on page %d: %s", symbol, page_idx + 1, e)
        return ""


def extract_clean_page_text(
    pdf_path: Union[str, Path],
    page_idx: int,
    plumber_page: Optional[Any] = None,
    symbol: str = "",
) -> Tuple[str, str]:
    """Extract clean, readable text from a PDF page using a 3-tier fallback chain:
    
    1. pdfplumber plain-text extraction.
    2. PyMuPDF (fitz) text extraction (if pdfplumber output contains (cid: or garbled characters).
    3. pytesseract OCR (if PyMuPDF output is also garbled/empty).

    Returns:
        Tuple of (clean_text, method_used), where method_used is 'pdfplumber', 'pymupdf', or 'ocr'.
    """
    path = Path(pdf_path)
    sym = symbol or path.stem.split("_annual_report")[0].upper()

    # Tier 1: Try pdfplumber
    t_plumber = ""
    if plumber_page is not None:
        try:
            t_plumber = plumber_page.extract_text(layout=False) or ""
        except Exception:
            t_plumber = ""

    if t_plumber.strip() and not _is_text_garbled(t_plumber):
        logger.debug("[%s] Page %d extracted using: pdfplumber", sym, page_idx + 1)
        return t_plumber, "pdfplumber"

    # Tier 2: Try PyMuPDF
    t_mupdf = ""
    try:
        doc = pymupdf.open(str(path))
        if page_idx < len(doc):
            t_mupdf = doc[page_idx].get_text("text") or ""
        doc.close()
    except Exception as e:
        logger.debug("[%s] PyMuPDF extraction error on page %d: %s", sym, page_idx + 1, e)

    if t_mupdf.strip() and not _is_text_garbled(t_mupdf):
        logger.info("[%s] Page %d extracted using: PyMuPDF (resolved pdfplumber garble)", sym, page_idx + 1)
        return t_mupdf, "pymupdf"

    # Tier 3: Try OCR
    t_ocr = _extract_page_text_ocr(path, page_idx, symbol=sym)
    if t_ocr.strip() and not _is_text_garbled(t_ocr):
        logger.info("[%s] Page %d extracted using: OCR (pytesseract)", sym, page_idx + 1)
        return t_ocr, "ocr"

    # Best-effort fallback
    best_text = t_mupdf if t_mupdf.strip() else t_plumber
    logger.warning("[%s] Page %d plain text remains unverified/garbled across all extractors.", sym, page_idx + 1)
    return best_text, "fallback"


def _extract_section_clean_text(pdf_path: Path, page_indices: List[int], symbol: str = "") -> str:
    """Extract concatenated clean text of specified page indices using multi-tier extraction."""
    chunks = []
    for idx in page_indices[:3]:
        txt, method_used = extract_clean_page_text(pdf_path, idx, symbol=symbol)
        if txt.strip():
            chunks.append(f"--- PAGE {idx + 1} (Method: {method_used}) ---\n{txt.strip()}")
    return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# AI-First Statement Page Detection (Sole Section Discovery Mechanism)
# ---------------------------------------------------------------------------
def _find_statement_pages(
    pdf_path: Union[str, Path],
) -> Tuple[List[int], str, List[int], str]:
    """Locate candidate page indices for Balance Sheet and P&L statements using Groq AI.

    AI-first approach (no regex heading search):
    1. Scan document pages to construct top-lines summaries.
    2. Call `llm_locate_statement_sections` (backed by disk cache) as the SOLE section finder.
    3. Priority: 'consolidated' statements preferred; 'standalone' as fallback.

    Returns:
        (bs_pages, bs_type_used, pnl_pages, pnl_type_used)
        where types are 'consolidated' | 'standalone' | 'not_found'
    """
    path = Path(pdf_path)
    symbol = path.stem.split("_annual_report")[0].upper()

    page_summaries: List[Dict[str, Any]] = []
    num_pages = 0

    try:
        doc = pymupdf.open(str(path))
        num_pages = len(doc)

        for i in range(num_pages):
            try:
                page_text, _ = extract_clean_page_text(path, i, symbol=symbol)
                if not page_text.strip():
                    continue

                # Collect condensed snippet for AI locator
                lines = [line.strip() for line in page_text.split("\n") if line.strip()]
                top_snippet = "\n".join(lines[:8])[:300]
                page_summaries.append({
                    "page": i + 1,  # 1-indexed for LLM prompt
                    "text_snippet": top_snippet,
                })
            except Exception:
                continue
        doc.close()

    except Exception as e:
        logger.warning("[%s] PDF indexing encountered error: %s", path.name, e)
        return [], "not_found", [], "not_found"

    if not page_summaries:
        logger.warning("[%s] No readable pages found in PDF %s", symbol, path.name)
        return [], "not_found", [], "not_found"

    # AI Section Locator call (sole locator)
    ai_sections = llm_locate_statement_sections(page_summaries, symbol)
    if not ai_sections:
        logger.warning("[%s] AI section locator returned no sections for %s", symbol, path.name)
        return [], "not_found", [], "not_found"

    bs_cons = [p - 1 for p in ai_sections.get("consolidated_balance_sheet_pages", []) if 1 <= p <= num_pages]
    bs_std = [p - 1 for p in ai_sections.get("standalone_balance_sheet_pages", []) if 1 <= p <= num_pages]
    pnl_cons = [p - 1 for p in ai_sections.get("consolidated_pnl_pages", []) if 1 <= p <= num_pages]
    pnl_std = [p - 1 for p in ai_sections.get("standalone_pnl_pages", []) if 1 <= p <= num_pages]

    # 1. Determine Balance Sheet pages & tier (Consolidated preferred)
    if bs_cons:
        bs_type_used = "consolidated"
        first_bs = bs_cons[0]
        logger.info("[%s] Using AI-located Consolidated Balance Sheet (p.%d).", symbol, first_bs + 1)
    elif bs_std:
        bs_type_used = "standalone"
        first_bs = bs_std[0]
        logger.info("[%s] Consolidated Balance Sheet not found; using AI-located Standalone Balance Sheet (p.%d).", symbol, first_bs + 1)
    else:
        bs_type_used = "not_found"
        first_bs = None
        logger.warning("[%s] No Balance Sheet section found by AI locator.", symbol)

    bs_pages: List[int] = []
    if first_bs is not None:
        for offset in range(3):
            if first_bs + offset < num_pages:
                bs_pages.append(first_bs + offset)

    # 2. Determine P&L pages & tier (Consolidated preferred)
    if pnl_cons:
        pnl_type_used = "consolidated"
        first_pnl = pnl_cons[0]
        logger.info("[%s] Using AI-located Consolidated Statement of Profit & Loss (p.%d).", symbol, first_pnl + 1)
    elif pnl_std:
        pnl_type_used = "standalone"
        first_pnl = pnl_std[0]
        logger.info("[%s] Consolidated P&L not found; using AI-located Standalone P&L (p.%d).", symbol, first_pnl + 1)
    else:
        pnl_type_used = "not_found"
        first_pnl = None
        logger.warning("[%s] No Statement of Profit & Loss section found by AI locator.", symbol)

    pnl_pages: List[int] = []
    if first_pnl is not None:
        for offset in range(3):
            if first_pnl + offset < num_pages:
                pnl_pages.append(first_pnl + offset)

    return bs_pages, bs_type_used, pnl_pages, pnl_type_used


# ---------------------------------------------------------------------------
# Balance Sheet Extraction (AI-First Sole Mechanism)
# ---------------------------------------------------------------------------
def extract_balance_sheet_figures(
    pdf_path: Union[str, Path],
    symbol: Optional[str] = None,
) -> Dict[str, Any]:
    """Extract Total Assets and Total Current Liabilities using Groq AI as the sole extractor.

    1. Consolidated Balance Sheet (preferred)
    2. Standalone Balance Sheet (fallback)

    Regex table/line parsing is completely bypassed.
    `_source` fields indicate the statement tier ('consolidated' | 'standalone' | 'not_found').
    """
    path = Path(pdf_path)
    sym = symbol or path.stem.split("_annual_report")[0].upper()

    result: Dict[str, Any] = {
        "total_assets": None,
        "total_assets_source": None,
        "total_current_liabilities": None,
        "total_current_liabilities_source": None,
        "balance_sheet_type_used": "not_found",
        "unit": None,
        "error_message": None,
        "raw_matches": [],
    }

    if not path.exists():
        logger.warning("[%s] PDF file does not exist at path: %s", sym, path)
        result["error_message"] = f"Annual report PDF not found at {path.name}"
        return result

    bs_page_indices, bs_type_used, _, _ = _find_statement_pages(path)
    result["balance_sheet_type_used"] = bs_type_used

    if not bs_page_indices or bs_type_used == "not_found":
        msg = "No Balance Sheet section (Consolidated or Standalone) found in this report."
        logger.warning("[%s] %s", sym, msg)
        result["error_message"] = msg
        return result

    section_text = _extract_section_clean_text(path, bs_page_indices, symbol=sym)
    if not section_text.strip():
        msg = f"Extracted text from {bs_type_used} Balance Sheet pages was empty."
        logger.warning("[%s] %s", sym, msg)
        result["error_message"] = msg
        return result

    # Currency unit detection
    unit = detect_currency_unit(section_text)
    if unit:
        result["unit"] = unit

    # Call AI extractor as the sole extraction mechanism
    logger.info("[%s] Calling AI extractor for %s Balance Sheet figures...", sym, bs_type_used)
    llm_data = llm_extract_balance_sheet(section_text, sym)

    if llm_data:
        ta = llm_data.get("total_assets")
        tcl = llm_data.get("total_current_liabilities")

        if ta is not None:
            result["total_assets"] = ta
            result["total_assets_source"] = bs_type_used
            result["raw_matches"].append({
                "field": "total_assets",
                "source": "ai_extractor",
                "label_found": llm_data.get("total_assets_label_found"),
                "extracted_value": ta,
            })
            logger.info("[%s] Total Assets extracted via AI: %s", sym, ta)

        if tcl is not None:
            result["total_current_liabilities"] = tcl
            result["total_current_liabilities_source"] = bs_type_used
            result["raw_matches"].append({
                "field": "total_current_liabilities",
                "source": "ai_extractor",
                "label_found": llm_data.get("total_current_liabilities_label_found"),
                "extracted_value": tcl,
            })
            logger.info("[%s] Total Current Liabilities extracted via AI: %s", sym, tcl)
    else:
        logger.warning("[%s] AI extractor returned no data for Balance Sheet.", sym)

    if result["total_assets"] is None or result["total_current_liabilities"] is None:
        missing = []
        if result["total_assets"] is None:
            missing.append("Total Assets")
        if result["total_current_liabilities"] is None:
            missing.append("Total Current Liabilities")
        result["error_message"] = f"Could not extract {bs_type_used.capitalize()} Balance Sheet figures for: {', '.join(missing)}"

    return result


# ---------------------------------------------------------------------------
# Profit & Loss Statement Extraction (AI-First Sole Mechanism)
# ---------------------------------------------------------------------------
def _clean_expense_line_item(raw_label: str) -> Optional[str]:
    """Clean and validate an expense line item description.

    Strips leading Roman numerals, alphabetic or numeric indices (e.g. '(a)', 'a.', 'IV.').
    Rejects column headers, narrative footnotes, and standalone subtotal titles.
    """
    if not raw_label:
        return None

    # Strip leading indexing: '(a)', 'a.', '1.', '(iv)', 'IV.', etc.
    cleaned = re.sub(
        r"^\s*(?:\([a-z0-9ivx]+\)|[a-z0-9ivx]{1,3}[\.\)]|\([0-9]+\))\s*",
        "",
        raw_label.strip(),
        flags=re.IGNORECASE,
    ).strip()

    if len(cleaned) < 3:
        return None

    if "(cid:" in cleaned.lower():
        return None

    # Alphabetic sanity check
    alpha_chars = sum(1 for c in cleaned if c.isalpha())
    non_space_chars = sum(1 for c in cleaned if not c.isspace())
    if alpha_chars < 3 or (non_space_chars > 0 and (alpha_chars / non_space_chars) < 0.45):
        return None

    low = cleaned.lower()

    # Reject table headers and column descriptions
    if low in ("particulars", "notes", "note", "note no", "note no.", "year ended", "figures as at"):
        return None
    if re.match(r"^figures\s+for\s+the\b", low) or re.match(r"^year\s+ended\b", low):
        return None

    # Reject section headers
    if low in ("expenses", "iv. expenses", "iv expenses", "expenses:"):
        return None

    # Reject section grand totals or standalone subtotals
    if re.match(r"^total[\s_-]*expenses?\b", low) or re.match(r"^sub-?total\b", low) or re.match(r"^total\s*\(", low):
        return None

    # Reject non-expense narrative lines
    if any(k in low for k in [
        "profit before", "profit / (loss)", "profit/(loss)", "tax expense", "taxexpense", "profit for the year",
        "statement of cash", "cash flow", "cashflow", "notesto", "notes to", "corporate information",
        "material accounting", "firm registration", "membership no", "din:", "dated:", "directors"
    ]):
        return None

    return cleaned


def extract_pnl_figures(
    pdf_path: Union[str, Path],
    symbol: Optional[str] = None,
) -> Dict[str, Any]:
    """Extract Revenue from Operations and individual Expense line items using Groq AI as the sole extractor.

    1. Consolidated Statement of Profit and Loss (preferred)
    2. Standalone Statement of Profit and Loss (fallback)

    Regex table/line parsing is completely bypassed.
    `_source` fields indicate the statement tier ('consolidated' | 'standalone' | 'not_found').
    """
    path = Path(pdf_path)
    sym = symbol or path.stem.split("_annual_report")[0].upper()

    result: Dict[str, Any] = {
        "revenue_from_operations": None,
        "revenue_from_operations_source": None,
        "expense_items": [],
        "expense_items_source": None,
        "reported_total_expenses": None,
        "pnl_type_used": "not_found",
        "unit": None,
        "error_message": None,
        "raw_matches": [],
    }

    if not path.exists():
        logger.warning("[%s] PDF file does not exist at path: %s", sym, path)
        result["error_message"] = f"Annual report PDF not found at {path.name}"
        return result

    _, _, pnl_page_indices, pnl_type_used = _find_statement_pages(path)
    result["pnl_type_used"] = pnl_type_used

    if not pnl_page_indices or pnl_type_used == "not_found":
        msg = "No Statement of Profit and Loss section (Consolidated or Standalone) found in this report."
        logger.warning("[%s] %s", sym, msg)
        result["error_message"] = msg
        return result

    section_text = _extract_section_clean_text(path, pnl_page_indices, symbol=sym)
    if not section_text.strip():
        msg = f"Extracted text from {pnl_type_used} P&L pages was empty."
        logger.warning("[%s] %s", sym, msg)
        result["error_message"] = msg
        return result

    # Currency unit detection
    unit = detect_currency_unit(section_text)
    if unit:
        result["unit"] = unit

    # Call AI extractor as the sole extraction mechanism
    logger.info("[%s] Calling AI extractor for %s P&L figures and expense line items...", sym, pnl_type_used)
    llm_data = llm_extract_pnl(section_text, sym)

    if llm_data:
        # Revenue from Operations
        rev = llm_data.get("revenue_from_operations")
        if rev is not None:
            result["revenue_from_operations"] = rev
            result["revenue_from_operations_source"] = pnl_type_used
            result["raw_matches"].append({
                "field": "revenue_from_operations",
                "source": "ai_extractor",
                "label_found": llm_data.get("revenue_label_found"),
                "extracted_value": rev,
            })
            logger.info("[%s] Revenue from Operations extracted via AI: %s", sym, rev)

        # Reported Total Expenses (for reconciliation check)
        rep_tot_exp = llm_data.get("reported_total_expenses")
        if rep_tot_exp is not None:
            result["reported_total_expenses"] = rep_tot_exp
            logger.info("[%s] Reported Total Expenses extracted via AI: %s", sym, rep_tot_exp)

        # Expense Line Items
        raw_expenses = llm_data.get("expense_items") or []
        cleaned_expenses = []
        for item in raw_expenses:
            raw_name = item.get("name", "").strip()
            clean_name = _clean_expense_line_item(raw_name)
            val = item.get("value")
            if clean_name and val is not None:
                cleaned_expenses.append({
                    "name": clean_name,
                    "value": float(val),
                    "selected": False,
                })
            elif raw_name:
                logger.warning("[%s] Dropped invalid/corrupted expense item: '%s'", sym, raw_name)

        if cleaned_expenses:
            result["expense_items"] = cleaned_expenses
            result["expense_items_source"] = pnl_type_used
            logger.info("[%s] %d valid Expense items extracted via AI", sym, len(cleaned_expenses))
        else:
            logger.warning("[%s] AI extractor returned 0 valid expense items.", sym)

    else:
        logger.warning("[%s] AI extractor returned no data for P&L.", sym)

    # -----------------------------------------------------------------------
    # Verification & Reconciliations
    # -----------------------------------------------------------------------
    has_other_expenses = any(
        re.search(r"\bother\s+expenses?\b|\bmiscellaneous\s+expenses?\b", it["name"], re.IGNORECASE)
        for it in result["expense_items"]
    )
    if result["expense_items"] and not has_other_expenses:
        logger.warning(
            "[%s] 'Other Expenses' line item not found in extracted P&L expenses — expense list may be incomplete.",
            sym,
        )

    reported_tot = result.get("reported_total_expenses")
    if reported_tot is not None and reported_tot > 0 and result["expense_items"]:
        sum_extracted = sum(float(it["value"] or 0.0) for it in result["expense_items"])
        diff_ratio = abs(sum_extracted - reported_tot) / reported_tot
        if diff_ratio > 0.02:
            logger.warning(
                "[%s] Expense items sum (%.2f) differs from reported Total Expenses (%.2f) by %.1f%% (>2%% tolerance).",
                sym,
                sum_extracted,
                reported_tot,
                diff_ratio * 100.0,
            )
        else:
            logger.info(
                "[%s] Expense items sum (%.2f) successfully reconciled with reported Total Expenses (%.2f) (diff: %.2f%%).",
                sym,
                sum_extracted,
                reported_tot,
                diff_ratio * 100.0,
            )

    if result["revenue_from_operations"] is None or not result["expense_items"]:
        missing_pnl = []
        if result["revenue_from_operations"] is None:
            missing_pnl.append("Revenue from Operations")
        if not result["expense_items"]:
            missing_pnl.append("Expense line items")
        result["error_message"] = f"Could not extract {pnl_type_used.capitalize()} P&L figures for: {', '.join(missing_pnl)}"

    return result


# ===========================================================================
# DEPRECATED / UNUSED LEGACY REGEX EXTRACTION FUNCTIONS
# Replaced by AI-first extraction architecture. Kept for historical reference.
# These functions are NOT called anywhere in the active pipeline.
# ===========================================================================
def _legacy_is_balance_sheet_page(text: str, target_type: str = "consolidated") -> bool:
    """[DEPRECATED] Legacy regex check for Balance Sheet page."""
    return False


def _legacy_is_pnl_page(text: str, target_type: str = "consolidated") -> bool:
    """[DEPRECATED] Legacy regex check for P&L page."""
    return False


def _legacy_extract_balance_sheet_regex(pdf_path: Path, page_indices: List[int]) -> Dict[str, Any]:
    """[DEPRECATED] Legacy regex extraction for Balance Sheet figures."""
    return {}


def _legacy_extract_pnl_regex(pdf_path: Path, page_indices: List[int]) -> Dict[str, Any]:
    """[DEPRECATED] Legacy regex extraction for P&L figures."""
    return {}
