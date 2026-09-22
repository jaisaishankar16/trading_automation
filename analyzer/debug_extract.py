"""CLI Diagnostic Runner for Local OCR + Regex Financial Extraction.

Usage:
    python -m analyzer.debug_extract CONS SUVIDHAA ARVEE
    python -m analyzer.debug_extract CONS --no-groq
"""

import argparse
import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from analyzer.financial_extractor import extract_financials
from analyzer.groq_fallback import check_groq_quota_preflight
from scrape_annual_reports import find_latest_annual_report_pdf


def main():
    parser = argparse.ArgumentParser(description="Debug financial extraction on downloaded PDFs with full diagnostics.")
    parser.add_argument("symbols", nargs="+", help="Stock symbol(s) to debug (e.g. CONS SUVIDHAA ARVEE)")
    parser.add_argument("--no-groq", action="store_true", help="Force-disable Groq fallback (test 100% regex mode)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    print("\n" + "=" * 80)
    print(" FINANCIAL EXTRACTION DIAGNOSTIC RUNNER (Local OCR + Regex Primary) ")
    print(f" Targets: {', '.join(args.symbols)}")
    print(f" Groq Mode: {'DISABLED via --no-groq' if args.no_groq else 'ENABLED (subject to preflight quota)'}")
    print("=" * 80 + "\n")

    groq_available = False
    if not args.no_groq:
        groq_available = check_groq_quota_preflight()

    results = {}
    for sym in args.symbols:
        clean_sym = sym.strip().upper()
        pdf_path = find_latest_annual_report_pdf(clean_sym)
        if not pdf_path or not pdf_path.exists():
            print(f"\n[ERROR] No downloaded Annual Report PDF found for {clean_sym} in annual_reports/\n")
            continue

        print(f"\n>>> Running Extraction for {clean_sym} on {pdf_path.name} (size: {pdf_path.stat().st_size / 1024:.1f} KB)...")
        res = extract_financials(clean_sym, pdf_path, groq_available=groq_available)
        results[clean_sym] = res

        print(f"\n--- Result Summary for {clean_sym} ---")
        print(f"  Status: {res.get('status')}")
        print(f"  Error: {res.get('error_message')}")
        print(f"  Balance Sheet Type: {res.get('balance_sheet_type_used')}")
        print(f"  P&L Type: {res.get('pnl_type_used')}")
        print(f"  Total Assets: {res.get('total_assets')} ({res.get('total_assets_source')})")
        print(f"  Total Current Liabilities: {res.get('total_current_liabilities')} ({res.get('total_current_liabilities_source')})")
        print(f"  Revenue from Operations: {res.get('revenue_from_operations')} ({res.get('revenue_from_operations_source')})")
        print(f"  Expense Items Count: {len(res.get('expense_items', []))} (Reported Total: {res.get('reported_total_expenses')})")
        if res.get('expense_items'):
            print("  Sample Expense Items:")
            for it in res.get('expense_items')[:5]:
                print(f"    - {it.get('name')}: {it.get('value')}")
            if len(res.get('expense_items')) > 5:
                print(f"    ... and {len(res.get('expense_items')) - 5} more items")
        print("-" * 50)


if __name__ == "__main__":
    main()
