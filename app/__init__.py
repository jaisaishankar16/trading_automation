"""Flask Application Factory Module."""

import logging
import sys
from flask import Flask

from config import Config


def configure_logging(app: Flask) -> None:
    """Configure structured logging for stdout and application handlers."""
    log_level = logging.DEBUG if Config.DEBUG else logging.INFO
    log_format = "%(asctime)s [%(levelname)s] (%(name)s) %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"

    # Configure root logger
    logging.basicConfig(
        level=log_level,
        format=log_format,
        datefmt=date_format,
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )

    # Set external libraries to warning to keep logs clean
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.INFO)


def create_app() -> Flask:
    """Create and configure an instance of the Flask application."""
    app = Flask(__name__)
    app.config.from_object(Config)

    # Setup directories
    Config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    Config.TEMP_DIR.mkdir(parents=True, exist_ok=True)

    # Configure logging
    configure_logging(app)
    app.logger.info("Initializing NSE Trading Automation Web Application...")

    # Register blueprints
    from app.routes import main_bp

    app.register_blueprint(main_bp)

    app.logger.info("Application initialized successfully.")
    return app
