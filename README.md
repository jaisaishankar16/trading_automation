# Financial Intelligence & Trading Automation System

An automated end-to-end web application and financial intelligence engine that integrates:
1. **NSE Volume Gainers & Spurts Scraping** (Task 1: Playwright automation).
2. **Screener.in Annual Report PDF Downloader** (Task 2: Dynamic year detection, timestamped filenames, and resumable downloading).
3. **AI-Driven Financial Statement Extraction & ROC Analyzer** (Task 3: Balance Sheet & Profit & Loss parsing with font/OCR remediation, Groq LLM integration, default expense auto-selection, and Return on Capital (ROC) calculations).

---

## Architecture Overview

```
Trading Automation/
├── analyzer/
│   ├── analysis_runner.py         # Full pipeline orchestrator & ROC calculation
│   ├── llm_extractor.py           # Groq LLM extraction (Llama 3.3 70B) & AI section locator
│   └── pdf_financials.py          # PDF statement extraction, font garble detection & OCR fallback
├── annual_reports/                # Downloaded Annual Report PDFs (<SYMBOL>_annual_report_<TIMESTAMP>.pdf)
├── app/
│   ├── __init__.py                # Flask application factory & logger setup
│   ├── routes.py                  # REST API endpoints & pipeline execution threads
│   ├── storage.py                 # JSON persistence layer for scraped data
│   ├── scrapers/
│   │   ├── __init__.py            # Custom exception definitions
│   │   └── nse_volume_gainers.py  # Playwright automation for NSE Volume Gainers
│   ├── templates/
│   │   └── index.html             # Financial analysis & ROC dashboard (Jinja2)
│   └── static/
│       ├── app.js                 # Frontend state, interactive expense modal, live progress polling
│       └── style.css              # Dark slate theme, glassmorphism cards, micro-animations
├── data/                          # Cached JSON datasets & LLM extraction caches
│   ├── financial_analysis.json    # Extracted financials, selected expenses, and ROC results
│   ├── llm_cache/                 # Cached Groq LLM extraction responses
│   └── volume_gainers.json        # Latest scraped NSE volume gainers
├── temp/                          # Temporary download artifacts (auto-cleaned)
├── .env.example                   # Environment configuration template
├── config.py                      # Centralized configuration loaded via python-dotenv
├── reanalyze_financials.py        # CLI tool to re-run financial extraction on downloaded PDFs
├── requirements.txt               # Core Python dependencies
├── run.py                         # Application entrypoint
├── scrape_annual_reports.py       # Standalone Screener.in PDF downloader CLI
└── README.md                      # Documentation & setup guide
```

---

## Prerequisites & Installation

### 1. Python & System Requirements
- **Python**: 3.10 or higher
- **Internet Connection**: To access NSE India, Screener.in, and the Groq API.

### 2. OCR System Dependencies (Required for Garbled PDF Font Fallback)
Certain company PDFs contain subset fonts without standard character mappings (producing unreadable glyph IDs like `(cid:145)` in plain text). The extraction engine automatically attempts plain-text parsing via `pdfplumber`, retries via `PyMuPDF`, and falls back to Optical Character Recognition (OCR) via `pytesseract`.

To enable OCR fallback, install the `tesseract-ocr` binary and `poppler-utils` on your system:

- **Windows (PowerShell / WinGet)**:
  ```powershell
  winget install UB-Mannheim.TesseractOCR
  ```
  *(Alternatively, download the installer from [UB-Mannheim Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki).)*

- **Ubuntu / Debian Linux**:
  ```bash
  sudo apt update
  sudo apt install -y tesseract-ocr poppler-utils
  ```

- **macOS (Homebrew)**:
  ```bash
  brew install tesseract poppler
  ```

---

## Quick Start Setup Guide

### 1. Create and Activate a Virtual Environment

**On Windows (PowerShell):**
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

**On macOS / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```

### 2. Install Python Dependencies

```bash
pip install -r requirements.txt
```

### 3. Install Playwright Chromium Browser

```bash
playwright install chromium
```

### 4. Configure Environment Variables

Copy `.env.example` to `.env` and configure your settings:
```bash
cp .env.example .env
```

Set your **Groq API Key** in `.env`:
```env
GROQ_API_KEY=your_actual_groq_api_key
GROQ_MODEL=llama-3.3-70b-versatile
```

### 5. Start the Application

```bash
python run.py
```

Open your browser and navigate to:
```
http://127.0.0.1:5000
```

---

## Core Pipeline Features

1. **NSE Volume Gainers**: Scrapes live spurts in volume and turnover from NSE India.
2. **Screener.in Downloader**: Automatically navigates to company pages on Screener.in, parses the latest Annual Report year, and saves PDFs with timestamped filenames (`<SYMBOL>_annual_report_<YYYYMMDD_HHMMSS>.pdf`). Resumes existing downloads automatically.
3. **Font Garble & OCR Remediation**: Detects unmapped glyph codes (`(cid:...`) and routes corrupted pages through PyMuPDF and OCR to ensure clean financial text.
4. **AI-Driven Section Finding**: Identifies Balance Sheet and Profit & Loss statement sections dynamically using Groq LLM when layout headings vary from standard wording.
5. **AI-First Expense Line Extraction**: Uses Groq LLM to extract granular, high-quality expense line items from P&L statements, with regex fallback.
6. **Default Expense Auto-Selection & ROC Calculation**: Automatically selects all operating expenses (excluding Finance Costs) upon pipeline completion and calculates:
   $$\text{Operating Profit} = \text{Revenue from Operations} - \text{Selected Expenses}$$
   $$\text{Capital Employed} = \text{Total Assets} - \text{Total Current Liabilities}$$
   $$\text{ROC (\%)} = \left( \frac{\text{Operating Profit}}{\text{Capital Employed}} \right) \times 100$$
7. **Interactive Expense Selection Modal**: Allows users to customize expense line items and recalculate ROC on-the-fly.
8. **Excel Report Export**: Exports all financial figures and calculated ROC metrics to `.xlsx` format.

---

## CLI Utilities

- **Re-run Financial Extraction on Downloaded Reports**:
  ```bash
  python reanalyze_financials.py --symbols BBTC ASTAR MANUGRAPH
  ```
- **Standalone Screener PDF Downloader**:
  ```bash
  python scrape_annual_reports.py
  ```

---

## API Endpoints

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/` | Serves the web dashboard interface |
| `GET` | `/api/health` | Health check endpoint |
| `GET` | `/api/volume-gainers` | Returns cached NSE volume gainers data |
| `POST` | `/api/refresh/volume-gainers` | Triggers on-demand scrape of NSE volume gainers |
| `POST` | `/api/refresh/full-pipeline` | Triggers the end-to-end automated pipeline |
| `GET` | `/api/progress` | Polls real-time pipeline execution progress |
| `GET` | `/api/analysis` | Returns financial analysis and ROC dataset |
| `POST` | `/api/analysis/<symbol>/select-expenses` | Updates selected expenses and recalculates ROC |
| `GET` | `/api/analysis/export` | Generates and downloads Excel report (`.xlsx`) |
