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
        extra = "ignore"

    @property
    def openai_api_key(self) -> str | None:
        """Get OpenAI API key from environment (supports multiple var names)."""
        return (
            os.environ.get("API_OPENAI_API_KEY") or
            os.environ.get("OPENAI_API_KEY") or
            os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY")
        )

    @property
    def openai_key_loaded(self) -> bool:
        """Check if OpenAI API key is available."""
        return self.openai_api_key is not None and len(self.openai_api_key) > 0


def get_config() -> APIConfig:
    """Get API configuration from environment."""
    return APIConfig()
