"""Local OCR & Regex Financial Extractor for Annual Report PDFs.

Primary extraction method (Zero external API dependency, zero quota risk):
1. PyMuPDF (fitz) text extraction per page with targeted pytesseract OCR fallback for garbled/empty pages.
2. Full extracted document text caching in data/text_cache/<SYMBOL>_<pdf_filename>.json.
3. Regex-based financial section detection (Consolidated preferred, Standalone fallback).
4. Window-aware regex financial value and individual expense line item extraction.
"""

import io
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from PIL import Image
import pymupdf

logger = logging.getLogger("local_extractor")

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
TEXT_CACHE_DIR = DATA_DIR / "text_cache"


# ---------------------------------------------------------------------------
# Numeric & Text Parsing Helpers
# ---------------------------------------------------------------------------
def _clean_number(val: Any) -> Optional[float]:
    """Parse numeric string into float or None."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return round(float(val), 2)
    s = str(val).replace(",", "").replace("₹", "").replace("$", "").replace("`", "").strip()
    if not s or s in ("-", "—", "–", "N/A", "NA", "Nil", "nil", ".", "None", "null"):
        return None

    is_negative = False
    if (s.startswith("(") and s.endswith(")")) or s.startswith("-"):
        is_negative = True
        s = re.sub(r"[\(\)\-]", "", s).strip()

    cleaned = re.sub(r"[^\d.]", "", s)
    if not cleaned or cleaned == ".":
        return None

    try:
        f = float(cleaned)
        return round(-f if is_negative else f, 2)
    except (ValueError, TypeError):
        return None


def _is_valid_expense_name(name: str) -> bool:
    """Validate expense line item name, rejecting header or total lines."""
    if not name or not isinstance(name, str):
        return False
    clean = name.strip()
    if len(clean) < 3 or len(clean) > 120:
        return False
    lower = clean.lower()
    # Reject summary / non-expense lines
    if lower in (
        "total expenses", "total expense", "total", "subtotal", "total expenditure",
        "exceptional items", "profit before tax", "expenses", "expenses:", "iv. expenses", "iv expenses",
        "particulars", "notes", "notes:", "note no.", "note", "for the year ended", "as at", "amount",
        "restated", "income", "total income", "revenue:", "revenue from operations", "revenue"
    ):
        return False
    if "(cid:" in lower:
        return False
    if not any(c.isalpha() for c in clean):
        return False
    return True


# ---------------------------------------------------------------------------
# Font Garbling Detection & OCR
# ---------------------------------------------------------------------------
def _is_text_garbled(text: str) -> bool:
    """Detect if extracted page text is empty or corrupted with font encoding glyph garbage."""
    if not text or not text.strip():
        return True

    # 1. CID glyph pattern count/density
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
        if (0 <= cp <= 8) or (11 <= cp <= 12) or (14 <= cp <= 31) or (128 <= cp <= 159):
            corrupted_count += 1
        elif (0x370 <= cp <= 0x37F) or (0xE000 <= cp <= 0xF8FF) or (0x200 <= cp <= 0x24F and cp not in (0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2013, 0x2014)):
            corrupted_count += 1

    if total_chars > 30 and (corrupted_count / total_chars) > 0.06:
        return True

    return False


def _get_tesseract_cmd() -> Optional[str]:
    """Find the path to the system tesseract binary if installed."""
    env_cmd = os.getenv("TESSERACT_CMD")
    if env_cmd and os.path.isfile(env_cmd):
        return env_cmd
    which_cmd = shutil.which("tesseract")
    if which_cmd:
        return which_cmd
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
                "[%s] Tesseract binary not found for OCR on page %d.",
                symbol,
                page_idx + 1,
            )
            return ""

        doc = pymupdf.open(str(pdf_path))
        if page_idx >= len(doc):
            doc.close()
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


# ---------------------------------------------------------------------------
# Part A.1: Local Text Extraction by Page (with Disk Cache)
# ---------------------------------------------------------------------------
def extract_text_by_page(pdf_path: Path, symbol: str = "") -> Dict[int, str]:
    """Extract clean text per page from PDF (PyMuPDF with per-page OCR fallback).

    Returns:
        Dict[int, str]: Mapping 1-indexed page number -> page text.
    """
    clean_sym = symbol.strip().upper() if symbol else pdf_path.stem.split("_")[0].upper()
    TEXT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = TEXT_CACHE_DIR / f"{clean_sym}_{pdf_path.name}_text.json"

    # 1. Check disk cache
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cached = json.load(f)
            if cached and isinstance(cached, dict):
                int_keyed = {int(k): v for k, v in cached.items()}
                logger.info("[%s] Text cache HIT: loaded %d pages from %s", clean_sym, len(int_keyed), cache_file.name)
                return int_keyed
        except Exception as e:
            logger.warning("[%s] Failed to read text cache %s: %s", clean_sym, cache_file.name, e)

    logger.info("[%s] Starting local text extraction for %s (size: %.2f MB)...", clean_sym, pdf_path.name, pdf_path.stat().st_size / (1024 * 1024))
    pages_text: Dict[int, str] = {}
    ocr_pages_count = 0

    try:
        doc = pymupdf.open(str(pdf_path))
        num_pages = len(doc)

        for i in range(num_pages):
            page_num = i + 1
            page = doc[i]
            t_mupdf = page.get_text("text") or ""

            if t_mupdf.strip() and not _is_text_garbled(t_mupdf):
                pages_text[page_num] = t_mupdf
            else:
                logger.info("[%s] Page %d text is garbled or empty; running OCR fallback...", clean_sym, page_num)
                t_ocr = _extract_page_text_ocr(pdf_path, i, symbol=clean_sym)
                if t_ocr.strip():
                    pages_text[page_num] = t_ocr
                    ocr_pages_count += 1
                else:
                    pages_text[page_num] = t_mupdf

        doc.close()

    except Exception as e:
        logger.error("[%s] Error extracting text from PDF: %s", clean_sym, e)

    logger.info(
        "[%s] Text extraction complete: %d pages extracted (PyMuPDF: %d, OCR: %d).",
        clean_sym,
        len(pages_text),
        len(pages_text) - ocr_pages_count,
        ocr_pages_count,
    )

    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(pages_text, f, ensure_ascii=False, indent=2)
        logger.info("[%s] Saved extracted text cache to %s", clean_sym, cache_file.name)
    except Exception as e:
        logger.warning("[%s] Failed to write text cache: %s", clean_sym, e)

    return pages_text


# ---------------------------------------------------------------------------
# Part A.2: Financial Statement Section Finding
# ---------------------------------------------------------------------------
def find_statement_sections(pages_text: Dict[int, str]) -> Dict[str, Any]:
    """Locate Balance Sheet and P&L pages using robust heading and tabular content detection.

    Consolidated statements are prioritized over Standalone statements.
    """
    bs_cons_pages: List[int] = []
    bs_std_pages: List[int] = []
    pnl_cons_pages: List[int] = []
    pnl_std_pages: List[int] = []

    def _is_report_front_matter(text: str, page_num: int) -> bool:
        top_snippet = text[:800].lower()
        if any(h in top_snippet for h in ["independent auditor", "report on the audit", "to the members of", "opinion"]):
            return True
        if page_num <= 20 and any(h in top_snippet for h in ["table of contents", "annual report index", "contents"]):
            return True
        if text.count("...") > 4 or text.count("…") > 4:
            return True
        if any(h in top_snippet for h in ["directors' report", "director`s report", "board's report", "management discussion and analysis"]):
            return True
        return False

    sorted_pages = sorted(pages_text.keys())

    for p in sorted_pages:
        txt = pages_text[p]
        if not txt.strip() or _is_report_front_matter(txt, p):
            continue

        txt_lower = txt.lower()
        all_lines = [l.strip().lower() for l in txt.split("\n") if l.strip()]
        top_lines = all_lines[:35]
        header_block = " \n ".join(top_lines)

        is_consolidated = (
            "consolidated" in header_block or
            ("consolidated balance sheet" in txt_lower) or
            ("consolidated statement of profit" in txt_lower) or
            ("consolidated financial statements" in txt_lower)
        )

        # Exclude note disclosure headers (e.g. "Note 27 Revenue from operations" or "Notes to financial statements")
        is_note_page = any(re.search(r"^note\s+\d+[:\s]", l) for l in top_lines[:5]) or "notes to the financial" in header_block or "notes to financial" in header_block

        has_bs_heading = (
            any(re.search(r"^(?:(?:standalone|consolidated)\s+)?balance\s+sheet\b", l) for l in top_lines) or
            any(re.search(r"^(?:(?:standalone|consolidated)\s+)?statement\s+of\s+assets\s+and\s+liabilities\b", l) for l in top_lines) or
            ("balance sheet" in header_block and any(t in header_block for t in ["as at", "as on", "particulars"])) or
            ("assets" in header_block and "equity and liabilities" in txt_lower and "particulars" in header_block)
        )

        if has_bs_heading and not is_note_page and any(kw in txt_lower for kw in ["total assets", "non-current assets", "current assets", "total equity and liabilities", "total liabilities and equity"]):
            if is_consolidated:
                bs_cons_pages.append(p)
            else:
                bs_std_pages.append(p)

        has_pnl_heading = (
            any(re.search(r"^(?:(?:standalone|consolidated)\s+)?statement\s+of\s+profit\s+(?:and|&)\s+loss\b", l) for l in top_lines) or
            any(re.search(r"^(?:(?:standalone|consolidated)\s+)?profit\s+(?:and|&)\s+loss\s+account\b", l) for l in top_lines) or
            ("statement of profit" in header_block) or
            ("revenue from operations" in header_block and "expenses" in txt_lower and "particulars" in header_block)
        )

        if has_pnl_heading and not is_note_page and any(kw in txt_lower for kw in ["revenue from operations", "total revenue", "total income", "expenses", "employee benefits"]):
            if is_consolidated:
                pnl_cons_pages.append(p)
            else:
                pnl_std_pages.append(p)

    # Determine Balance Sheet
    if bs_cons_pages:
        bs_type_used = "consolidated"
        first_bs = bs_cons_pages[0]
    elif bs_std_pages:
        bs_type_used = "standalone"
        first_bs = bs_std_pages[0]
    else:
        bs_type_used = "not_found"
        first_bs = None

    bs_pages: List[int] = []
    if first_bs is not None:
        max_page = max(sorted_pages) if sorted_pages else first_bs
        for offset in range(3):
            p_cand = first_bs + offset
            if p_cand <= max_page and p_cand in pages_text:
                bs_pages.append(p_cand)

    # Determine P&L (prefer P&L page matching the same type and immediately adjacent to Balance Sheet)
    first_pnl = None
    if first_bs is not None:
        # Check immediate adjacent pages (first_bs, first_bs + 1, first_bs + 2) first
        for cand in [first_bs, first_bs + 1, first_bs + 2]:
            if cand in pages_text:
                cand_lower = pages_text[cand].lower()
                if "revenue from operations" in cand_lower and ("expenses" in cand_lower or "total income" in cand_lower):
                    first_pnl = cand
                    pnl_type_used = bs_type_used
                    break

    if first_pnl is None:
        if bs_type_used == "consolidated" and pnl_cons_pages:
            pnl_type_used = "consolidated"
            adj_pnl = [p for p in pnl_cons_pages if p >= (first_bs or 0)]
            first_pnl = adj_pnl[0] if adj_pnl else pnl_cons_pages[0]
        elif pnl_cons_pages:
            pnl_type_used = "consolidated"
            first_pnl = pnl_cons_pages[0]
        elif pnl_std_pages:
            pnl_type_used = "standalone"
            adj_pnl = [p for p in pnl_std_pages if first_bs and p >= first_bs]
            first_pnl = adj_pnl[0] if adj_pnl else pnl_std_pages[0]
        elif first_bs is not None:
            pnl_type_used = bs_type_used
            first_pnl = first_bs + 1 if (first_bs + 1) in pages_text else first_bs
        else:
            pnl_type_used = "not_found"
            first_pnl = None

    pnl_pages: List[int] = []
    if first_pnl is not None:
        max_page = max(sorted_pages) if sorted_pages else first_pnl
        for offset in range(3):
            p_cand = first_pnl + offset
            if p_cand <= max_page and p_cand in pages_text:
                pnl_pages.append(p_cand)

    logger.info(
        "Section finder results: BS=%s (pages: %s), PnL=%s (pages: %s)",
        bs_type_used,
        bs_pages,
        pnl_type_used,
        pnl_pages,
    )

    return {
        "balance_sheet_pages": bs_pages,
        "balance_sheet_type_used": bs_type_used,
        "pnl_pages": pnl_pages,
        "pnl_type_used": pnl_type_used,
    }


# ---------------------------------------------------------------------------
# Part A.3: Window-Aware Regex Value Extraction
# ---------------------------------------------------------------------------
def _parse_val_in_window(lines: List[str], idx: int) -> Optional[float]:
    """Extract the current-year monetary figure for a line item across same line or subsequent lines.

    Handles Note Reference columns: e.g. [Item Name] [24] [1,81,890.05] [1,88,729.81].
    """
    curr_line = lines[idx]
    curr_tokens = re.findall(r"\(?\s*-?\s*\d[\d,]*\.?\d*\s*\)?", curr_line)

    # 1. Check numbers on the same line
    if curr_tokens:
        parsed_same: List[float] = []
        for tok in curr_tokens:
            val = _clean_number(tok)
            if val is not None:
                parsed_same.append(val)

        if parsed_same:
            # If first number is an integer <= 60 (note reference) and followed by values:
            if len(parsed_same) >= 2 and parsed_same[0].is_integer() and 1 <= parsed_same[0] <= 60 and abs(parsed_same[1]) > 0:
                return round(parsed_same[1], 2)
            return round(parsed_same[0], 2)

    # 2. Check numbers on subsequent 1-4 lines (multi-line layout)
    sub_numbers: List[float] = []
    for off in range(1, 5):
        if idx + off < len(lines):
            nxt = lines[idx + off].strip()
            if not nxt:
                continue
            # If next line is clearly another item text label, stop scanning
            tokens = re.findall(r"\(?\s*-?\s*\d[\d,]*\.?\d*\s*\)?", nxt)
            if not tokens and any(c.isalpha() for c in nxt) and len(nxt) > 4:
                break
            for tok in tokens:
                val = _clean_number(tok)
                if val is not None:
                    sub_numbers.append(val)
            if len(sub_numbers) >= 2:
                break

    if not sub_numbers:
        return None

    if len(sub_numbers) >= 2 and sub_numbers[0].is_integer() and 1 <= sub_numbers[0] <= 60 and abs(sub_numbers[1]) > 0:
        return round(sub_numbers[1], 2)

    return round(sub_numbers[0], 2)


def extract_values_regex(
    pages_text: Dict[int, str],
    section_info: Dict[str, Any],
) -> Dict[str, Any]:
    """Extract Balance Sheet and P&L figures using regular expressions.

    Returns dict shape:
        {
            "total_assets": float | None,
            "total_current_liabilities": float | None,
            "revenue_from_operations": float | None,
            "expense_items": list[dict],
            "balance_sheet_type_used": str,
            "pnl_type_used": str,
        }
    """
    total_assets: Optional[float] = None
    total_cur_liab: Optional[float] = None
    rev_ops: Optional[float] = None
    expense_items: List[Dict[str, Any]] = []

    bs_pages = section_info.get("balance_sheet_pages", [])
    bs_type = section_info.get("balance_sheet_type_used", "not_found")
    pnl_pages = section_info.get("pnl_pages", [])
    pnl_type = section_info.get("pnl_type_used", "not_found")

    # 1. Extract Balance Sheet Figures
    if bs_pages:
        bs_lines: List[str] = []
        for p in bs_pages:
            txt = pages_text.get(p, "")
            bs_lines.extend(txt.split("\n"))

        ta_patterns = [
            re.compile(r"^\s*total\s+(?:non-current\s+and\s+current\s+)?assets\b", re.IGNORECASE),
            re.compile(r"^\s*total\s+assets\b", re.IGNORECASE),
            re.compile(r"\btotal\s+assets\b", re.IGNORECASE),
            re.compile(r"^\s*total\s+equity\s+and\s+liabilities\b", re.IGNORECASE),
            re.compile(r"\btotal\s+equity\s+and\s+liabilities\b", re.IGNORECASE),
            re.compile(r"^\s*total\s+liabilities\s+and\s+equity\b", re.IGNORECASE),
            re.compile(r"\btotal\s+liabilities\s+and\s+equity\b", re.IGNORECASE),
        ]

        tcl_patterns = [
            re.compile(r"^\s*total\s+current\s+liabilities\b", re.IGNORECASE),
            re.compile(r"\btotal\s+current\s+liabilities\b", re.IGNORECASE),
            re.compile(r"\bcurrent\s+liabilities\s+total\b", re.IGNORECASE),
            re.compile(r"^\s*total\s+-\s*current\s+liabilities\b", re.IGNORECASE),
        ]

        for i, line in enumerate(bs_lines):
            line_clean = line.strip()
            if not line_clean:
                continue

            if total_assets is None:
                for pat in ta_patterns:
                    if pat.search(line_clean):
                        if "%" in line_clean or "ratio" in line_clean.lower():
                            continue
                        val = _parse_val_in_window(bs_lines, i)
                        if val is not None and val > 0:
                            total_assets = val
                            logger.info("Found Total Assets via regex: %s on line: '%s'", val, line_clean)
                            break

            if total_cur_liab is None:
                for pat in tcl_patterns:
                    if pat.search(line_clean):
                        if "%" in line_clean or "ratio" in line_clean.lower():
                            continue
                        val = _parse_val_in_window(bs_lines, i)
                        if val is not None and val >= 0:
                            total_cur_liab = val
                            logger.info("Found Total Current Liabilities via regex: %s on line: '%s'", val, line_clean)
                            break

    # 2. Extract P&L Figures
    if pnl_pages:
        pnl_lines: List[str] = []
        for p in pnl_pages:
            txt = pages_text.get(p, "")
            pnl_lines.extend(txt.split("\n"))

        rev_patterns = [
            re.compile(r"^\s*(?:i\.\s*|i\s+)?total\s+revenue\s+from\s+operations\b", re.IGNORECASE),
            re.compile(r"^\s*(?:i\.\s*|i\s+)?revenue\s+from\s+operations\b", re.IGNORECASE),
            re.compile(r"\brevenue\s+from\s+operations\b", re.IGNORECASE),
            re.compile(r"^\s*(?:i\.\s*)?sale\s+of\s+products\s+/\s+services\b", re.IGNORECASE),
            re.compile(r"^\s*(?:i\.\s*)?income\s+from\s+operations\b", re.IGNORECASE),
        ]

        for i, line in enumerate(pnl_lines):
            line_clean = line.strip()
            if not line_clean:
                continue

            if rev_ops is None:
                for pat in rev_patterns:
                    if pat.search(line_clean):
                        if "%" in line_clean or "adjusted" in line_clean.lower() or ("note" in line_clean.lower() and "operations" not in line_clean[:25].lower()):
                            continue
                        val = _parse_val_in_window(pnl_lines, i)
                        if val is not None and val >= 0:
                            rev_ops = val
                            logger.info("Found Revenue from Operations via regex: %s on line: '%s'", val, line_clean)
                            break

        # Scan for Expense Items between "Expenses" header and "Total Expenses" subtotal
        in_expenses_block = False
        re_expenses_header = re.compile(r"^\s*(?:iv\.|iv|ii\.|ii|iii\.|iii)?\s*expenses\s*:?\s*$", re.IGNORECASE)
        re_total_expenses = re.compile(r"\btotal\s+(?:expenses|expenditure)\b", re.IGNORECASE)
        seen_expense_names = set()

        for i, line in enumerate(pnl_lines):
            line_clean = line.strip()
            if not line_clean:
                continue
            line_lower = line_clean.lower()

            if not in_expenses_block:
                if re_expenses_header.match(line_clean) or line_lower in ("expenses", "expenses:", "iv. expenses", "iv expenses", "expenditure"):
                    in_expenses_block = True
                    continue
                if re.match(r"^(?:iv|iv\.)\s+expenses\b", line_clean, re.IGNORECASE) and not re.findall(r"\d", line_clean):
                    in_expenses_block = True
                    continue

            if in_expenses_block:
                if re_total_expenses.search(line_clean):
                    logger.info("Reached 'Total Expenses' line ('%s'), ending expense items parse.", line_clean)
                    in_expenses_block = False
                    break

                if re.search(r"\b(?:profit\s+before\s+tax|exceptional\s+items)\b", line_clean, re.IGNORECASE):
                    in_expenses_block = False
                    break

                if any(c.isalpha() for c in line_clean) and len(line_clean) >= 3 and not re.search(r"^(?:particulars|notes?|restated|dues|amount)", line_lower):
                    val = _parse_val_in_window(pnl_lines, i)
                    if val is not None and val >= 0:
                        raw_name = re.sub(r"\(?\s*-?\s*\d[\d,]*\.?\d*\s*\)?", "", line_clean).strip()
                        raw_name = re.sub(r"^[\d\.\-\)\(]+\s*", "", raw_name).strip()
                        raw_name = re.sub(r"[\:\.\-]+$", "", raw_name).strip()

                        if _is_valid_expense_name(raw_name) and raw_name.lower() not in seen_expense_names:
                            seen_expense_names.add(raw_name.lower())
                            expense_items.append({
                                "name": raw_name,
                                "value": val,
                            })
                            logger.debug("Parsed expense item: '%s' -> %s", raw_name, val)

    logger.info(
        "Regex extraction results: Assets=%s, Liabilities=%s, Rev=%s, ExpenseItems=%d",
        total_assets,
        total_cur_liab,
        rev_ops,
        len(expense_items),
    )

    return {
        "total_assets": total_assets,
        "total_current_liabilities": total_cur_liab,
        "revenue_from_operations": rev_ops,
        "expense_items": expense_items,
        "balance_sheet_type_used": bs_type,
        "pnl_type_used": pnl_type,
    }
