"""Scraper Modules Package for NSE India Data Sources.

This package provides web automation scrapers for various NSE India data pages.
Scrapers are organized as standalone modules adhering to a consistent interface:
- Each module exposes a `scrape_<data_source>()` function.
- All scrapers raise `NSEScrapeError` or its subclasses on failure.
- Temp artifacts are cleaned up automatically.
"""


class NSEScrapeError(Exception):
    """Base exception for all NSE scraping failures."""
    pass


class NSEBlockedError(NSEScrapeError):
    """Raised when NSE anti-bot / Cloudflare / rate limits block the request."""
    pass


class NSEPageStructureError(NSEScrapeError):
    """Raised when expected page elements or CSV download button cannot be located."""
    pass


class NSEDownloadError(NSEScrapeError):
    """Raised when the CSV download fails or times out."""
    pass


__all__ = [
    "NSEScrapeError",
    "NSEBlockedError",
    "NSEPageStructureError",
    "NSEDownloadError",
]
