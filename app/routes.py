"""Flask API and Dashboard Routes Module.

Provides:
- GET  /                                  : Serves the web dashboard (index.html).
- GET  /api/health                        : Basic health check endpoint.
- GET  /api/volume-gainers                : Returns cached volume gainers data without scraping.
- POST /api/refresh/volume-gainers        : Triggers an on-demand scrape of NSE volume gainers.
- POST /api/refresh/full-pipeline         : Triggers the end-to-end pipeline (NSE -> Screener -> PDF Extraction).
- GET  /api/progress                      : Returns live pipeline progress for polling.
- GET  /api/analysis                      : Returns the latest financial analysis dataset.
- POST /api/analysis/<symbol>/select-expenses: Updates selected expenses and calculates ROC.
- GET  /api/analysis/export               : Generates and returns an Excel report (.xlsx).
"""

import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, make_response, render_template, request, send_file

from analyzer.analysis_runner import (
    apply_expense_selection,
    export_financial_analysis_excel,
    get_financial_analysis,
    run_full_pipeline,
)
from app.scrapers import NSEScrapeError
from app.scrapers.nse_volume_gainers import scrape_volume_gainers
from app.storage import storage
from config import Config

logger = logging.getLogger(__name__)

main_bp = Blueprint("main", __name__)

# ---------------------------------------------------------------------------
# In-Memory Thread-Safe Pipeline Progress State
# ---------------------------------------------------------------------------
_progress_lock = threading.Lock()
_progress_state: Dict[str, Any] = {
    "running": False,
    "percent": 0,
    "current_step": "Idle",
    "current_symbol": None,
    "status": "idle",
    "error": None,
    "started_at": None,
    "completed_at": None,
}


def _update_progress(
    percent: int,
    step: str,
    symbol: Optional[str] = None,
    status: str = "running",
    error: Optional[str] = None,
) -> None:
    """Safely update the shared progress state dictionary."""
    with _progress_lock:
        _progress_state["percent"] = max(0, min(100, int(percent)))
        _progress_state["current_step"] = step
        _progress_state["current_symbol"] = symbol
        _progress_state["status"] = status
        _progress_state["error"] = error
        if status == "running" and not _progress_state["running"]:
            _progress_state["running"] = True
            _progress_state["started_at"] = datetime.now(timezone.utc).isoformat()
        elif status in ("complete", "error"):
            _progress_state["running"] = False
            _progress_state["completed_at"] = datetime.now(timezone.utc).isoformat()


def _get_progress_snapshot() -> Dict[str, Any]:
    """Retrieve a copy of the current progress state."""
    with _progress_lock:
        return dict(_progress_state)


# ---------------------------------------------------------------------------
# Dashboard & Health Endpoints
# ---------------------------------------------------------------------------
@main_bp.route("/")
def index():
    """Render main trading dashboard template."""
    return render_template("index.html")


@main_bp.route("/api/health", methods=["GET"])
def health_check():
    """Health check endpoint."""
    return jsonify({
        "status": "healthy",
        "service": "Trading Automation & Financial Analysis Service",
    }), 200


# ---------------------------------------------------------------------------
# NSE Volume Gainers Endpoints
# ---------------------------------------------------------------------------
@main_bp.route("/api/volume-gainers", methods=["GET"])
def get_volume_gainers():
    """Retrieve the latest cached volume gainers data without scraping."""
    pid = os.getpid()
    try:
        data = storage.get_volume_gainers()
        response = make_response(jsonify(data), 200)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    except Exception as e:
        logger.error("Failed to read volume gainers from storage: %s", e, exc_info=True)
        return jsonify({
            "error": "Failed to read cached data from storage layer.",
            "details": str(e),
            "status": "error",
        }), 500


@main_bp.route("/api/refresh/volume-gainers", methods=["POST"])
def refresh_volume_gainers():
    """Trigger an on-demand scrape of NSE Volume Gainers & Spurts."""
    pid = os.getpid()
    cooldown_active, remaining_seconds = storage.is_cooldown_active(
        Config.REFRESH_COOLDOWN_SECONDS
    )
    if cooldown_active:
        logger.warning("Refresh rejected: cooldown active (%ds remaining)", remaining_seconds)
        return jsonify({
            "status": "rate_limited",
            "error": f"Please wait {remaining_seconds} seconds before triggering another refresh.",
            "retry_after": remaining_seconds,
        }), 429

    try:
        result = scrape_volume_gainers()
        storage.save_volume_gainers(
            symbols=result["symbols"],
            count=result["count"],
            fetched_at=result["fetched_at"],
        )
        response_payload = {
            "status": "success",
            "source": "volume_gainers_spurts",
            "symbols": result["symbols"],
            "count": result["count"],
            "fetched_at": result["fetched_at"],
            "duration_seconds": result.get("duration_seconds", 0.0),
        }
        response = make_response(jsonify(response_payload), 200)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response

    except NSEScrapeError as e:
        logger.error("Scraping failed: %s", e)
        return jsonify({
            "status": "error",
            "error": str(e),
            "message": "Scraper was unable to complete data extraction from NSE India.",
        }), 502
    except Exception as e:
        logger.error("Unexpected error during scrape: %s", e, exc_info=True)
        return jsonify({
            "status": "error",
            "error": f"Internal server error: {e}",
        }), 500


# ---------------------------------------------------------------------------
# Full Pipeline & Financial Analysis Endpoints
# ---------------------------------------------------------------------------
@main_bp.route("/api/refresh/full-pipeline", methods=["POST"])
def refresh_full_pipeline():
    """Trigger the full automated pipeline in a background thread."""
    with _progress_lock:
        if _progress_state["running"]:
            return jsonify({
                "status": "running",
                "message": "Pipeline already running.",
                "progress": _progress_state["percent"],
                "current_step": _progress_state["current_step"],
            }), 409

        # Initialize progress state for new run
        _progress_state["running"] = True
        _progress_state["percent"] = 0
        _progress_state["current_step"] = "Fetching latest NSE volume gainers data..."
        _progress_state["current_symbol"] = None
        _progress_state["status"] = "running"
        _progress_state["error"] = None
        _progress_state["started_at"] = datetime.now(timezone.utc).isoformat()
        _progress_state["completed_at"] = None

    def _worker():
        try:
            logger.info("Background thread starting full financial analysis pipeline...")
            run_full_pipeline(progress_callback=_update_progress)
        except Exception as e:
            logger.error("Error during background pipeline execution: %s", e, exc_info=True)
            _update_progress(100, f"Error: {e}", status="error", error=str(e))

    thread = threading.Thread(target=_worker, name="PipelineWorker", daemon=True)
    thread.start()

    return jsonify({
        "started": True,
        "status": "started",
        "message": "Full financial pipeline started in background.",
    }), 202


@main_bp.route("/api/progress", methods=["GET"])
def get_progress():
    """Return the current pipeline execution progress state for frontend polling."""
    snapshot = _get_progress_snapshot()
    response = make_response(jsonify(snapshot), 200)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@main_bp.route("/api/analysis", methods=["GET"])
def get_analysis_data():
    """Return the current financial analysis dataset."""
    try:
        records = get_financial_analysis()
        response = make_response(jsonify(records), 200)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    except Exception as e:
        logger.error("Failed to read financial analysis records: %s", e, exc_info=True)
        return jsonify({
            "error": "Failed to read financial analysis records.",
            "details": str(e),
        }), 500


@main_bp.route("/api/analysis/<symbol>/select-expenses", methods=["POST"])
def select_expenses(symbol: str):
    """Update selected expense items for a specific stock symbol and compute ROC."""
    try:
        body = request.get_json(silent=True) or {}
        selected_expenses = body.get("selected_expenses", [])

        if not isinstance(selected_expenses, list):
            return jsonify({
                "error": "'selected_expenses' must be a list of expense item names.",
            }), 400

        updated_record = apply_expense_selection(
            symbol=symbol,
            selected_expense_names=selected_expenses,
        )

        response = make_response(jsonify(updated_record), 200)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response

    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.error("Failed to apply expense selection for symbol '%s': %s", symbol, e, exc_info=True)
        return jsonify({
            "error": "Failed to apply expense selection.",
            "details": str(e),
        }), 500


@main_bp.route("/api/analysis/export", methods=["GET"])
def export_analysis():
    """Generate and return an Excel (.xlsx) report of the financial analysis."""
    try:
        records = get_financial_analysis()
        if not records:
            return jsonify({
                "error": "No financial analysis data available to export.",
            }), 404

        excel_path = export_financial_analysis_excel()
        if not excel_path.exists():
            return jsonify({"error": "Failed to generate Excel export file."}), 500

        return send_file(
            path_or_file=str(excel_path),
            as_attachment=True,
            download_name="financial_analysis_report.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    except Exception as e:
        logger.error("Error generating Excel export: %s", e, exc_info=True)
        return jsonify({
            "error": "Failed to export financial report.",
            "details": str(e),
        }), 500
