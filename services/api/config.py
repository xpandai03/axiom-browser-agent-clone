import os
from pydantic_settings import BaseSettings


class APIConfig(BaseSettings):
    """Configuration for the API service."""

    # Server configuration
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    # CORS configuration
    cors_origins: str = "*"

    # Browser configuration
    browser_headless: bool = True

    # Simulation mode (for testing without browser)
    use_simulation: bool = False

    class Config:
        env_prefix = "API_"
        env_file = ".env"


def get_config() -> APIConfig:
    """Get API configuration from environment."""
    return APIConfig()
