"""Groq LLM Extractor Module for Financial Statements.

Provides AI-based extraction of Consolidated/Standalone Balance Sheet and
Profit & Loss figures from raw financial report text using Groq's Llama models,
as well as AI-driven statement section discovery.
Includes disk-based response caching, 30s timeout per call, token usage logging,
and exponential backoff rate-limit handling.
"""

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

logger = logging.getLogger("llm_extractor")

# Load environment variables
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DEFAULT_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
CACHE_DIR = BASE_DIR / "data" / "llm_cache"
DEFAULT_TIMEOUT_SEC = 30.0


def _get_cache_path(symbol: str, section_type: str) -> Path:
    """Return cache file path for given symbol and section type ('balance_sheet', 'pnl', 'section_pages')."""
    clean_symbol = symbol.strip().upper().replace("/", "_").replace("\\", "_")
    return CACHE_DIR / f"{clean_symbol}_{section_type}.json"


def _load_from_cache(symbol: str, section_type: str) -> Optional[Dict[str, Any]]:
    """Retrieve cached LLM extraction result if available."""
    cache_path = _get_cache_path(symbol, section_type)
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                logger.info("[%s] Loaded LLM result from cache: %s", symbol, cache_path.name)
                return data
        except Exception as e:
            logger.warning("[%s] Failed to read LLM cache file %s: %s", symbol, cache_path, e)
    return None


def _save_to_cache(symbol: str, section_type: str, data: Dict[str, Any]) -> None:
    """Save successful LLM extraction result to cache."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path = _get_cache_path(symbol, section_type)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info("[%s] Saved LLM result to cache: %s", symbol, cache_path.name)
    except Exception as e:
        logger.warning("[%s] Failed to save LLM cache file: %s", symbol, e)


def _get_groq_client():
    """Instantiate and return Groq client if API key is present."""
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key or api_key == "your_groq_api_key_here":
        logger.warning("GROQ_API_KEY not set or is placeholder in .env; Groq AI extractor unavailable.")
        return None

    try:
        from groq import Groq
        return Groq(api_key=api_key)
    except ImportError:
        logger.error("groq package is not installed. Please install with `pip install groq`.")
        return None
    except Exception as e:
        logger.error("Failed to initialize Groq client: %s", e)
        return None


def _call_groq_with_retries(
    client,
    prompt: str,
    system_prompt: str,
    symbol: str,
    model: str = DEFAULT_MODEL,
    temperature: float = 0.1,
    timeout: float = DEFAULT_TIMEOUT_SEC,
) -> Optional[str]:
    """Execute Groq chat completion with JSON mode, 30s hard timeout, and retry on 429 rate limits."""
    backoff_delays = [2, 4, 8]
    active_model = model

    for attempt in range(len(backoff_delays) + 1):
        try:
            logger.info(
                "[%s] Calling Groq API (model=%s, timeout=%.0fs, attempt=%d)...",
                symbol,
                active_model,
                timeout,
                attempt + 1,
            )
            response = client.chat.completions.create(
                model=active_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                temperature=temperature,
                response_format={"type": "json_object"},
                timeout=timeout,
            )

            # Log token usage if available
            if hasattr(response, "usage") and response.usage:
                p_tok = getattr(response.usage, "prompt_tokens", 0)
                c_tok = getattr(response.usage, "completion_tokens", 0)
                t_tok = getattr(response.usage, "total_tokens", 0)
                logger.info(
                    "[%s] Groq API call succeeded (model=%s, tokens: prompt=%d, completion=%d, total=%d)",
                    symbol,
                    active_model,
                    p_tok,
                    c_tok,
                    t_tok,
                )

            content = response.choices[0].message.content
            if content:
                return content.strip()
            return None

        except Exception as e:
            err_str = str(e).lower()
            is_rate_limit = "429" in err_str or "rate limit" in err_str or "too many requests" in err_str
            is_model_error = "404" in err_str or "model_not_found" in err_str or "decommissioned" in err_str
            is_timeout = "timeout" in err_str or "timed out" in err_str or "deadline" in err_str

            if is_timeout:
                logger.error(
                    "[%s] Groq API call timed out after %.0fs on attempt %d: %s. Skipping symbol call to avoid pipeline stall.",
                    symbol,
                    timeout,
                    attempt + 1,
                    e,
                )
                return None

            if is_model_error and active_model != "llama-3.3-70b-versatile":
                logger.warning("[%s] Model '%s' unavailable; switching to 'llama-3.3-70b-versatile'...", symbol, active_model)
                active_model = "llama-3.3-70b-versatile"
                continue

            if is_rate_limit and attempt < len(backoff_delays):
                delay = backoff_delays[attempt]
                logger.warning("[%s] Groq 429 Rate Limit hit. Backing off for %ds (attempt %d/3)...", symbol, delay, attempt + 1)
                time.sleep(delay)
            else:
                logger.error("[%s] Groq API call failed: %s", symbol, e)
                return None

    return None


def _clean_numeric_field(val: Any) -> Optional[float]:
    """Helper to convert string/numeric values from LLM into a valid float or None."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        cleaned = val.replace(",", "").strip()
        if (cleaned.startswith("(") and cleaned.endswith(")")) or cleaned.startswith("-"):
            cleaned = cleaned.replace("(", "").replace(")", "").replace("-", "").strip()
            try:
                return -float(cleaned)
            except ValueError:
                return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _is_valid_expense_name(name: str) -> bool:
    """Sanity filter to reject non-alphabetic, garbled glyph, or total/subtotal items."""
    if not name or len(name.strip()) < 3:
        return False

    clean_name = name.strip()
    # Check for cid glyph artifacts
    if "(cid:" in clean_name.lower():
        return False

    # Check for minimal alphabetic content
    alpha_chars = sum(1 for c in clean_name if c.isalpha())
    non_space_chars = sum(1 for c in clean_name if not c.isspace())
    if alpha_chars < 3 or (non_space_chars > 0 and (alpha_chars / non_space_chars) < 0.45):
        return False

    low = clean_name.lower()
    # Reject Grand Totals, Subtotals, or Section Titles
    if re.search(r"^\s*(?:total\s+expenses?|total\s+income|sub-?total|total\s*\(|profit\s+before|profit\s*/\s*\(loss\))", low):
        return False

    return True


# ---------------------------------------------------------------------------
# AI-Driven Statement Section Locator (Primary Section Finding)
# ---------------------------------------------------------------------------
def llm_locate_statement_sections(
    document_pages_summary: List[Dict[str, Any]],
    symbol: str,
) -> Optional[Dict[str, List[int]]]:
    """Identify Balance Sheet and Profit & Loss statement page numbers via Groq LLM.

    Args:
        document_pages_summary: List of dicts, each with 'page' (1-indexed int) and 'text_snippet' (top lines).
        symbol: Stock symbol for logging and caching.

    Returns:
        Dict with keys:
            - 'consolidated_balance_sheet_pages': List[int]
            - 'standalone_balance_sheet_pages': List[int]
            - 'consolidated_pnl_pages': List[int]
            - 'standalone_pnl_pages': List[int]
        Or None if extraction fails.
    """
    if not document_pages_summary:
        logger.warning("[%s] Empty document pages summary provided for AI section location.", symbol)
        return None

    # 1. Check cache first
    cached = _load_from_cache(symbol, "section_pages")
    if cached is not None and isinstance(cached, dict):
        return cached

    # 2. Get Groq client
    client = _get_groq_client()
    if not client:
        logger.warning("[%s] Groq client unavailable for section location.", symbol)
        return None

    system_prompt = (
        "You are an expert financial auditor specializing in analyzing Indian company annual reports. "
        "Your task is to identify the EXACT page numbers containing the primary financial statement tables. "
        "Respond with ONLY a valid JSON object matching the requested schema."
    )

    merged_result: Dict[str, List[int]] = {
        "consolidated_balance_sheet_pages": [],
        "standalone_balance_sheet_pages": [],
        "consolidated_pnl_pages": [],
        "standalone_pnl_pages": [],
    }

    # Batch in chunks of 50 pages to comfortably stay within context window
    chunk_size = 50
    chunks = [document_pages_summary[i:i + chunk_size] for i in range(0, len(document_pages_summary), chunk_size)]

    for chunk_idx, chunk in enumerate(chunks, 1):
        pages_text_block = []
        for p in chunk:
            page_num = p.get("page", 0)
            snippet = (p.get("text_snippet") or "").strip()
            if snippet:
                pages_text_block.append(f"[PAGE {page_num}]\n{snippet}\n")

        if not pages_text_block:
            continue

        raw_pages_str = "\n".join(pages_text_block)
        first_pg = chunk[0]["page"]
        last_pg = chunk[-1]["page"]
        logger.info(
            "[%s] Sending pages %d-%d summary snippet (%d chars) to AI section locator (chunk %d/%d)...",
            symbol,
            first_pg,
            last_pg,
            len(raw_pages_str),
            chunk_idx,
            len(chunks),
        )

        base_prompt = (
            "You are given page numbers and the first few lines of text from each page of a company's annual report.\n"
            "Identify:\n"
            "1. The page number(s) containing the Consolidated Balance Sheet (or equivalent — it may be titled differently, "
            "e.g. 'Consolidated Statement of Assets and Liabilities', 'Consolidated Balance Sheet as at...', etc. — use your judgment on financial statement structure, not just exact keyword matching).\n"
            "2. The page number(s) for the Standalone Balance Sheet as a separate fallback, if distinguishable.\n"
            "3. The page number(s) for the Consolidated Profit and Loss statement (may be titled 'Statement of Profit and Loss', 'Statement of Profit & Loss', 'Income Statement', etc.).\n"
            "4. The page number(s) for the Standalone P&L as fallback.\n\n"
            "IMPORTANT Rules:\n"
            "- Disregard Table of Contents (Index), Auditor's Reports, Director's Reports, and Narrative Notices that merely mention statements.\n"
            "- Only return the page numbers where the actual tabular financial statements begin.\n"
            "- Respond with ONLY JSON in this exact structure:\n"
            "{\n"
            '  "consolidated_balance_sheet_pages": [<int>, ...],\n'
            '  "standalone_balance_sheet_pages": [<int>, ...],\n'
            '  "consolidated_pnl_pages": [<int>, ...],\n'
            '  "standalone_pnl_pages": [<int>, ...]\n'
            "}\n"
            "Use empty arrays where a section cannot be confidently identified.\n\n"
            f"--- DOCUMENT PAGES SUMMARY (Symbol: {symbol}, Chunk {chunk_idx}/{len(chunks)}) ---\n"
            f"{raw_pages_str}\n"
            "--- END SUMMARY ---"
        )

        raw_response = _call_groq_with_retries(client, base_prompt, system_prompt, symbol)
        if raw_response:
            try:
                parsed = json.loads(raw_response)
                if isinstance(parsed, dict):
                    for k in merged_result.keys():
                        vals = parsed.get(k, [])
                        if isinstance(vals, list):
                            for v in vals:
                                if isinstance(v, int) and v not in merged_result[k]:
                                    merged_result[k].append(v)
            except json.JSONDecodeError as e:
                logger.warning("[%s] Failed to parse AI section locator JSON response in chunk %d: %s", symbol, chunk_idx, e)

    # Sort page numbers
    for k in merged_result:
        merged_result[k].sort()

    logger.info(
        "[%s] AI section locator identified pages: ConsBS=%s, StandBS=%s, ConsPnL=%s, StandPnL=%s",
        symbol,
        merged_result["consolidated_balance_sheet_pages"],
        merged_result["standalone_balance_sheet_pages"],
        merged_result["consolidated_pnl_pages"],
        merged_result["standalone_pnl_pages"],
    )

    # Save to cache
    _save_to_cache(symbol, "section_pages", merged_result)
    return merged_result


# ---------------------------------------------------------------------------
# Balance Sheet AI Value Extraction
# ---------------------------------------------------------------------------
def llm_extract_balance_sheet(section_text: str, symbol: str) -> Optional[Dict[str, Any]]:
    """Extract Total Assets and Total Current Liabilities from Balance Sheet text via Groq LLM.

    Args:
        section_text: Clean text of the Balance Sheet section.
        symbol: Stock symbol for logging and caching.

    Returns:
        Dict with keys:
            - 'total_assets': float | None
            - 'total_current_liabilities': float | None
            - 'total_assets_label_found': str | None
            - 'total_current_liabilities_label_found': str | None
        Or None if extraction fails completely.
    """
    if not section_text or not section_text.strip():
        logger.warning("[%s] Empty section text provided for LLM Balance Sheet extraction.", symbol)
        return None

    # 1. Check cache first
    cached = _load_from_cache(symbol, "balance_sheet")
    if cached is not None:
        return cached

    # 2. Get Groq client
    client = _get_groq_client()
    if not client:
        logger.warning("[%s] Groq client unavailable for Balance Sheet extraction.", symbol)
        return None

    # 3. Build Prompt
    system_prompt = (
        "You are an expert financial analyst extracting figures from annual report text. "
        "You must respond with ONLY a valid JSON object matching the requested schema. "
        "Never guess, estimate, or hallucinate numbers. If a line item is absent, return null."
    )

    logger.info("[%s] Sending Balance Sheet text (%d chars) to AI extractor...", symbol, len(section_text))

    base_prompt = (
        "You are extracting specific financial figures from a company's Balance Sheet (Consolidated or Standalone) text. "
        "Given the raw text below, find the values for 'Total Assets' and 'Total Current Liabilities' "
        "(or clearly equivalent line items, accounting for wording variations).\n\n"
        "Respond with ONLY valid JSON in this exact shape:\n"
        "{\n"
        '  "total_assets": <number or null>,\n'
        '  "total_current_liabilities": <number or null>,\n'
        '  "total_assets_label_found": "<exact text of the label found, or null>",\n'
        '  "total_current_liabilities_label_found": "<exact text of the label found, or null>"\n'
        "}\n\n"
        "If a value genuinely cannot be found in the text, use null — do not guess or estimate. "
        "Do not include any explanation, markdown formatting, or text outside the JSON object.\n\n"
        f"--- BALANCE SHEET RAW TEXT (Symbol: {symbol}) ---\n"
        f"{section_text}\n"
        "--- END RAW TEXT ---"
    )

    raw_response = _call_groq_with_retries(client, base_prompt, system_prompt, symbol)

    parsed_result = None
    if raw_response:
        try:
            parsed_result = json.loads(raw_response)
        except json.JSONDecodeError:
            logger.warning("[%s] Failed to parse initial JSON response from Groq. Retrying with stricter prompt...", symbol)
            retry_prompt = base_prompt + "\n\nCRITICAL: Your previous response was not valid JSON. You MUST return ONLY a pure JSON object without codeblocks, quotes around numbers, or extra commentary."
            retry_response = _call_groq_with_retries(client, retry_prompt, system_prompt, symbol)
            if retry_response:
                try:
                    parsed_result = json.loads(retry_response)
                except json.JSONDecodeError as e:
                    logger.error("[%s] Failed to parse retry JSON from Groq: %s. Raw response: %s", symbol, e, retry_response)

    if not parsed_result or not isinstance(parsed_result, dict):
        logger.warning("[%s] LLM Balance Sheet extraction failed to return valid JSON dict.", symbol)
        return None

    # Clean and validate fields
    structured_data = {
        "total_assets": _clean_numeric_field(parsed_result.get("total_assets")),
        "total_current_liabilities": _clean_numeric_field(parsed_result.get("total_current_liabilities")),
        "total_assets_label_found": parsed_result.get("total_assets_label_found"),
        "total_current_liabilities_label_found": parsed_result.get("total_current_liabilities_label_found"),
    }

    # Save to cache
    _save_to_cache(symbol, "balance_sheet", structured_data)
    logger.info(
        "[%s] LLM Balance Sheet extracted: Total Assets=%s, Total Current Liab=%s",
        symbol,
        structured_data["total_assets"],
        structured_data["total_current_liabilities"],
    )
    return structured_data


# ---------------------------------------------------------------------------
# Profit & Loss Statement AI Value Extraction
# ---------------------------------------------------------------------------
def llm_extract_pnl(section_text: str, symbol: str) -> Optional[Dict[str, Any]]:
    """Extract Revenue from Operations, reported Total Expenses, and individual Expense line items from clean P&L text via Groq LLM.

    Args:
        section_text: Clean text of the Statement of Profit and Loss section.
        symbol: Stock symbol for logging and caching.

    Returns:
        Dict with keys:
            - 'revenue_from_operations': float | None
            - 'reported_total_expenses': float | None
            - 'expense_items': list of {'name': str, 'value': float}
            - 'revenue_label_found': str | None
        Or None if extraction fails completely.
    """
    if not section_text or not section_text.strip():
        logger.warning("[%s] Empty section text provided for LLM P&L extraction.", symbol)
        return None

    # 1. Check cache first
    cached = _load_from_cache(symbol, "pnl")
    if cached is not None:
        return cached

    # 2. Get Groq client
    client = _get_groq_client()
    if not client:
        logger.warning("[%s] Groq client unavailable for P&L extraction.", symbol)
        return None

    # 3. Build Prompt
    system_prompt = (
        "You are an expert financial analyst extracting figures from annual report text. "
        "You must respond with ONLY a valid JSON object matching the requested schema. "
        "Never guess, estimate, or hallucinate numbers. If a line item is absent, return null."
    )

    logger.info("[%s] Sending P&L text (%d chars) to AI extractor...", symbol, len(section_text))

    base_prompt = (
        "You are extracting specific financial figures from a company's Statement of Profit and Loss (Consolidated or Standalone) text. "
        "Given the text below, find the value for 'Revenue from Operations', the reported 'Total Expenses' figure if present, "
        "and a list of all individual expense line items.\n\n"
        "CRITICAL INSTRUCTIONS FOR EXPENSES:\n"
        "1. Extract every individual expense line item exactly as labeled in the source text, with its numeric value. "
        "Each item name must be a real, readable expense description (e.g. 'Cost of materials consumed', 'Employee benefit expenses', "
        "'Finance Costs', 'Depreciation and amortisation expense', 'Other Expenses') — never glyph codes, garbled characters, or placeholder text.\n"
        "2. If any line's label is unreadable or corrupted in the given text, exclude that line entirely rather than including a garbled name.\n"
        "3. Explicitly skip any subtotal/sub-breakdown lines that aren't standalone top-level expense categories.\n"
        "4. Always include 'Other Expenses' (or equivalently labeled miscellaneous expenses) if present before Total Expenses.\n"
        "5. Extract 'reported_total_expenses' as the explicit Total Expenses number shown in the statement (or null if absent).\n"
        "6. Never guess, estimate, or fabricate values — if an item is not found, do not make it up.\n\n"
        "Respond with ONLY valid JSON in this exact shape:\n"
        "{\n"
        '  "revenue_from_operations": <number or null>,\n'
        '  "reported_total_expenses": <number or null>,\n'
        '  "expense_items": [\n'
        '    {"name": "<real readable line item label>", "value": <number>}\n'
        "  ],\n"
        '  "revenue_label_found": "<exact text or null>"\n'
        "}\n\n"
        f"--- STATEMENT OF PROFIT AND LOSS RAW TEXT (Symbol: {symbol}) ---\n"
        f"{section_text}\n"
        "--- END RAW TEXT ---"
    )

    raw_response = _call_groq_with_retries(client, base_prompt, system_prompt, symbol)

    parsed_result = None
    if raw_response:
        try:
            parsed_result = json.loads(raw_response)
        except json.JSONDecodeError:
            logger.warning("[%s] Failed to parse initial P&L JSON from Groq. Retrying with stricter prompt...", symbol)
            retry_prompt = base_prompt + "\n\nCRITICAL: Your previous response was not valid JSON. You MUST return ONLY pure valid JSON without codeblocks or markdown."
            retry_response = _call_groq_with_retries(client, retry_prompt, system_prompt, symbol)
            if retry_response:
                try:
                    parsed_result = json.loads(retry_response)
                except json.JSONDecodeError as e:
                    logger.error("[%s] Failed to parse retry P&L JSON from Groq: %s. Raw response: %s", symbol, e, retry_response)

    if not parsed_result or not isinstance(parsed_result, dict):
        logger.warning("[%s] LLM P&L extraction failed to return valid JSON dict.", symbol)
        return None

    # Clean and validate expense items using sanity filter
    cleaned_expenses: List[Dict[str, Any]] = []
    raw_expenses = parsed_result.get("expense_items", [])
    if isinstance(raw_expenses, list):
        for item in raw_expenses:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
                val = _clean_numeric_field(item.get("value"))
                if _is_valid_expense_name(name) and val is not None:
                    cleaned_expenses.append({"name": name, "value": val})
                elif name:
                    logger.warning("[%s] Dropped invalid/corrupted LLM expense item: '%s'", symbol, name)

    structured_data = {
        "revenue_from_operations": _clean_numeric_field(parsed_result.get("revenue_from_operations")),
        "reported_total_expenses": _clean_numeric_field(parsed_result.get("reported_total_expenses")),
        "expense_items": cleaned_expenses,
        "revenue_label_found": parsed_result.get("revenue_label_found"),
    }

    # Save to cache
    _save_to_cache(symbol, "pnl", structured_data)
    logger.info(
        "[%s] LLM P&L extracted: Revenue=%s, Reported Total Exp=%s, %d valid expense items",
        symbol,
        structured_data["revenue_from_operations"],
        structured_data["reported_total_expenses"],
        len(cleaned_expenses),
    )
    return structured_data
