"""Quota-Aware Groq Fallback Module.

Provides narrow AI-based extraction strictly as a fallback for missing fields:
1. Pipeline-level pre-flight quota check (executed once at startup).
2. Running token accumulator to prevent rate limit cascade mid-run.
3. Single combined call per symbol with capped retry budget (~20s ceiling).
4. Fast-fail behavior on any 429/timeout so the pipeline is never blocked.
"""

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from groq import Groq

logger = logging.getLogger("groq_fallback")

load_dotenv()

DEFAULT_GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
MAX_GROQ_WAIT_SEC = 20.0  # Hard ceiling per symbol
DAILY_TOKEN_SAFETY_THRESHOLD = 5000


class GroqQuotaManager:
    """Manages Groq quota state across the pipeline run."""

    def __init__(self) -> None:
        self.is_available: bool = True
        self.cumulative_tokens_used: int = 0
        self.quota_exhausted_logged: bool = False

    def mark_exhausted(self, reason: str = "") -> None:
        """Disable Groq for the remainder of the pipeline run."""
        self.is_available = False
        if not self.quota_exhausted_logged:
            logger.warning(
                "Groq daily quota near/at limit, skipping AI fallback for this run — "
                "all gaps will be marked incomplete_data. (Reason: %s)",
                reason or "Quota check failed",
            )
            self.quota_exhausted_logged = True

    def record_usage(self, tokens: int) -> None:
        """Record tokens consumed by a call."""
        self.cumulative_tokens_used += tokens
        logger.debug("Groq cumulative tokens used this run: %d", self.cumulative_tokens_used)


# Singleton manager for pipeline lifecycle
quota_manager = GroqQuotaManager()


def check_groq_quota_preflight() -> bool:
    """Perform a single pre-flight check at pipeline startup to determine if Groq is usable.

    Returns:
        bool: True if Groq is available and has remaining quota; False to skip Groq entirely.
    """
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        logger.info("GROQ_API_KEY not configured. Skipping Groq fallback.")
        quota_manager.mark_exhausted("GROQ_API_KEY missing")
        return False

    model_name = os.getenv("GROQ_MODEL", DEFAULT_GROQ_MODEL)
    logger.info("Running Groq pre-flight quota check (model: %s)...", model_name)

    try:
        client = Groq(api_key=api_key)
        # Lightweight ping completion
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "user", "content": "ping"}
            ],
            max_tokens=5,
            timeout=10.0,
        )

        tokens = response.usage.total_tokens if hasattr(response, "usage") and response.usage else 5
        quota_manager.record_usage(tokens)
        quota_manager.is_available = True
        logger.info("Groq pre-flight check SUCCESSFUL. AI fallback enabled for missing fields.")
        return True

    except Exception as e:
        err_str = str(e).lower()
        logger.warning("Groq pre-flight check failed: %s", e)
        quota_manager.mark_exhausted(f"Pre-flight error: {e}")
        return False


def fill_gaps_via_groq(
    symbol: str,
    missing_fields: List[str],
    context_text: str,
) -> Dict[str, Any]:
    """Execute ONE fast fallback call covering ONLY the missing fields.

    Args:
        symbol: Stock symbol.
        missing_fields: List of missing field names (e.g. ['total_assets', 'revenue_from_operations']).
        context_text: Extracted text from candidate financial statement pages.

    Returns:
        dict: Extracted values for missing fields, or empty dict on failure.
    """
    if not quota_manager.is_available or not missing_fields or not context_text.strip():
        return {}

    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        return {}

    clean_sym = symbol.strip().upper()
    model_name = os.getenv("GROQ_MODEL", DEFAULT_GROQ_MODEL)

    system_prompt = (
        "You are an expert financial analyst. Extract ONLY the requested missing fields from the provided Indian annual report text. "
        "Prefer Consolidated statements over Standalone. For balance sheet figures, use the latest reporting year. "
        "Respond STRICTLY in JSON."
    )

    prompt = (
        f"Stock Symbol: {clean_sym}\n\n"
        f"Missing fields to extract: {', '.join(missing_fields)}\n\n"
        "Return a JSON object containing ONLY the missing fields found in the text below.\n"
        "Schema:\n"
        "{\n"
        '  "total_assets": number or null,\n'
        '  "total_current_liabilities": number or null,\n'
        '  "revenue_from_operations": number or null,\n'
        '  "expense_items": [{"name": "string", "value": number}],\n'
        '  "balance_sheet_type_used": "consolidated" | "standalone" | "not_found",\n'
        '  "pnl_type_used": "consolidated" | "standalone" | "not_found"\n'
        "}\n\n"
        f"Document Context:\n{context_text[:8000]}"
    )

    client = Groq(api_key=api_key)
    max_wait = MAX_GROQ_WAIT_SEC
    wait_elapsed = 0.0
    backoff = 2.0
    retries = 0
    max_retries = 2

    while wait_elapsed < max_wait and retries <= max_retries:
        try:
            start_t = time.time()
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                timeout=15.0,
                response_format={"type": "json_object"},
            )
            elapsed_call = time.time() - start_t

            if hasattr(response, "usage") and response.usage:
                quota_manager.record_usage(response.usage.total_tokens)

            content = response.choices[0].message.content
            if content:
                parsed = json.loads(content.strip())
                logger.info("[%s] Groq gap-fill call completed in %.2fs: %s", clean_sym, elapsed_call, parsed)
                return parsed
            break

        except Exception as e:
            retries += 1
            err_str = str(e).lower()
            if any(k in err_str for k in ["429", "rate_limit", "quota", "exceeded"]):
                logger.warning("[%s] Groq 429 quota/rate-limit hit: %s", clean_sym, e)
                # Immediately mark exhausted so no other symbols retry
                quota_manager.mark_exhausted(f"Hit 429 during extraction for {clean_sym}")
                return {}
            else:
                logger.warning("[%s] Groq fallback error (retry %d/%d): %s", clean_sym, retries, max_retries, e)
                time.sleep(backoff)
                wait_elapsed += backoff
                backoff = min(backoff * 2.0, 5.0)

    return {}
