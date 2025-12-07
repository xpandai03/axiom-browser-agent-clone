#!/usr/bin/env python3
"""Entry point for running the API service."""

import uvicorn
from services.api.config import get_config
from shared.utils.logging import setup_logging


def main():
    config = get_config()
    setup_logging(level="DEBUG" if config.debug else "INFO")

    uvicorn.run(
        "services.api.app:app",
        host=config.host,
        port=config.port,
        reload=config.debug,
    )


if __name__ == "__main__":
    main()
