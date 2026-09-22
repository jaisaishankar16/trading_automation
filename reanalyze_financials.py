"""Utility CLI script to re-analyze financial metrics on already-downloaded PDFs without re-downloading."""

import argparse
import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from analyzer.analysis_runner import reanalyze_existing_reports

def main():
    parser = argparse.ArgumentParser(description="Re-run financial extraction on existing downloaded Annual Report PDFs.")
    parser.add_argument("--symbols", nargs="+", help="Specific stock symbols to re-analyze (e.g. --symbols BBTC ASTAR TALBROAUTO)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("\n" + "=" * 60)
    print(" Re-running Financial Extraction on Downloaded Reports")
    print("=" * 60 + "\n")

    records = reanalyze_existing_reports(symbols=args.symbols)
    print(f"\n✓ Successfully updated {len(records)} financial records in data/financial_analysis.json")

if __name__ == "__main__":
    main()
