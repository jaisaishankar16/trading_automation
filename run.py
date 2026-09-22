"""Application Entry Point.

Starts the Flask development server for local execution and debugging.
"""

from app import create_app
from config import Config

app = create_app()

if __name__ == "__main__":
    app.logger.info("Starting NSE Scraper Web Application on http://127.0.0.1:%d", Config.PORT)
    app.logger.info("Headless Mode: %s | Anti-hammering Cooldown: %ds", Config.HEADLESS, Config.REFRESH_COOLDOWN_SECONDS)
    app.run(
        host="0.0.0.0",
        port=Config.PORT,
        debug=Config.DEBUG,
    )
