import os
import logging
from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


def get_port_from_env() -> int:
    """
    Get port from environment.

    Railway sets PORT directly (not API_PORT), so we check both.
    Priority: PORT > API_PORT > default 8080
    """
    # Railway sets PORT directly
    port_str = os.environ.get("PORT") or os.environ.get("API_PORT") or "8080"
    try:
        return int(port_str)
    except ValueError:
        return 8080


def get_openai_api_key() -> Tuple[Optional[str], Optional[str]]:
    """
    Get OpenAI API key from environment variables.

    Checks in order: API_OPENAI_API_KEY, OPENAI_API_KEY, AI_INTEGRATIONS_OPENAI_API_KEY

    Returns:
        Tuple of (api_key, source_env_var_name) or (None, None) if not found
    """
    env_vars = [
        "API_OPENAI_API_KEY",
        "OPENAI_API_KEY",
        "AI_INTEGRATIONS_OPENAI_API_KEY",
    ]

    for var_name in env_vars:
        key = os.environ.get(var_name)
        if key and len(key.strip()) > 0:
            return key.strip(), var_name

    return None, None


class APIConfig(BaseSettings):
    """Configuration for the API service."""

    # Server configuration
    host: str = "0.0.0.0"
    port: int = 8080  # Default matches Railway's typical port
    debug: bool = False

    # CORS configuration
    cors_origins: str = "*"

    # Browser configuration
    browser_headless: bool = True

    # Simulation mode (for testing without browser)
    use_simulation: bool = False

    # Stealth Mode (free, helps with some detection)
    stealth_mode: bool = True  # Default ON - applies playwright-stealth patches

    # Proxy Configuration (user provides credentials via env vars)
    proxy_enabled: bool = False
    proxy_server: Optional[str] = None      # e.g., "http://proxy.example.com:8080"
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = None

    class Config:
        env_prefix = "API_"
        env_file = ".env"
        extra = "ignore"

    @property
    def proxy_config(self) -> Optional[dict]:
        """Get Playwright proxy config dict."""
        if not self.proxy_enabled or not self.proxy_server:
            return None

        config = {"server": self.proxy_server}
        if self.proxy_username:
            config["username"] = self.proxy_username
        if self.proxy_password:
            config["password"] = self.proxy_password
        return config

    @property
    def openai_api_key(self) -> Optional[str]:
        """Get OpenAI API key from environment (supports multiple var names)."""
        key, _ = get_openai_api_key()
        return key

    @property
    def openai_key_loaded(self) -> bool:
        """Check if OpenAI API key is available."""
        key, _ = get_openai_api_key()
        return key is not None

    @property
    def openai_env_source(self) -> Optional[str]:
        """Get the env var name that provided the OpenAI key."""
        _, source = get_openai_api_key()
        return source


def get_config() -> APIConfig:
    """Get API configuration from environment."""
    # Create config and override port with Railway-compatible value
    config = APIConfig()
    # Override port from PORT env var (Railway sets this directly)
    config_dict = config.model_dump()
    config_dict["port"] = get_port_from_env()
    return APIConfig(**config_dict)


def log_openai_key_status():
    """Log OpenAI API key status at startup (does NOT log the key itself)."""
    key, source = get_openai_api_key()
    if key:
        # Mask the key for logging (show first 4 and last 4 chars)
        masked = f"{key[:4]}...{key[-4:]}" if len(key) > 8 else "***"
        logger.info(f"OpenAI API key FOUND from {source} (masked: {masked})")
    else:
        logger.warning("OpenAI API key NOT FOUND - checked: API_OPENAI_API_KEY, OPENAI_API_KEY, AI_INTEGRATIONS_OPENAI_API_KEY")
