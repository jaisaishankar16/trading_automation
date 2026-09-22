"""Application Configuration Module

Loads environment variables from .env using python-dotenv and provides
centralized configuration parameters for Flask, Playwright scrapers,
cooldown policies, and filesystem paths.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Base directory of the project
BASE_DIR = Path(__file__).resolve().parent

# Load environment variables from .env file
load_dotenv(BASE_DIR / ".env")


class Config:
    """Central configuration parameters."""

    # Server settings
    PORT = int(os.getenv("PORT", 5000))
    FLASK_ENV = os.getenv("FLASK_ENV", "production")
    DEBUG = os.getenv("DEBUG", "False").lower() in ("true", "1", "t")

    # Playwright Browser Scraper settings
    HEADLESS = os.getenv("HEADLESS", "true").lower() in ("true", "1", "t")
    BROWSER_TIMEOUT_MS = int(os.getenv("BROWSER_TIMEOUT_MS", 45000))

    # Anti-hammering cooldown in seconds between manual refresh requests
    REFRESH_COOLDOWN_SECONDS = int(os.getenv("REFRESH_COOLDOWN_SECONDS", 60))

    # Scraper retry settings
    MAX_RETRIES = int(os.getenv("MAX_RETRIES", 2))
    RETRY_DELAY_SECONDS = int(os.getenv("RETRY_DELAY_SECONDS", 3))

    # Storage paths
    DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
    TEMP_DIR = Path(os.getenv("TEMP_DIR", BASE_DIR / "temp" / "nse_downloads"))

    # File paths
    VOLUME_GAINERS_DATA_FILE = DATA_DIR / "volume_gainers.json"

    # NSE India URLs & Network Settings
    NSE_BASE_URL = "https://www.nseindia.com"
    NSE_VOLUME_GAINERS_URL = "https://www.nseindia.com/market-data/volume-gainers-spurts"

    # Realistic browser headers and User-Agent
    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
    VIEWPORT = {"width": 1920, "height": 1080}


# Ensure storage and temp directories exist
Config.DATA_DIR.mkdir(parents=True, exist_ok=True)
Config.TEMP_DIR.mkdir(parents=True, exist_ok=True)
