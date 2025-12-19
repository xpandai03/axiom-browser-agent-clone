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
    proxy_server: Optional[str] = None      # e.g., "geo.iproyal.com:12321" (host:port only)
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = None
    proxy_country: str = "us"               # Default to US for Uber Eats
    proxy_session: Optional[str] = None     # Sticky session ID (e.g., "ubereats1")

    class Config:
        env_prefix = "API_"
        env_file = ".env"
        extra = "ignore"

    @property
    def proxy_config(self) -> Optional[dict]:
        """
        Get Playwright proxy config using SOCKS5 protocol.

        WHY SOCKS5 INSTEAD OF HTTP:
        - HTTP proxies require CONNECT method for HTTPS tunneling
        - Chromium's HTTP proxy auth is broken (ERR_PROXY_AUTH_UNSUPPORTED)
        - Even with embedded auth, HTTPS traffic times out through HTTP proxies
        - SOCKS5 operates at TCP level, tunnels ALL traffic including HTTPS
        - Chromium DOES support username/password auth for SOCKS5

        IPRoyal SOCKS5 port: 12321 (same as HTTP, protocol auto-detected)
        Or explicit SOCKS5 port: 12322

        IPRoyal username format for geo + sticky session:
        username_country-us_session-mysession123
        """
        if not self.proxy_enabled:
            return None

        if not self.proxy_server:
            return None

        if not self.proxy_username or not self.proxy_password:
            return None

        # Build IPRoyal-formatted username with country + session
        username_parts = [self.proxy_username]

        # Add country targeting (always US for Uber Eats)
        if self.proxy_country:
            username_parts.append(f"country-{self.proxy_country}")

        # Add sticky session if configured
        if self.proxy_session:
            username_parts.append(f"session-{self.proxy_session}")

        formatted_username = "_".join(username_parts)

        # Normalize server: strip any protocol prefix
        server = self.proxy_server
        for prefix in ["socks5://", "socks4://", "http://", "https://"]:
            if server.startswith(prefix):
                server = server[len(prefix):]
                break

        # SOCKS5 with separate username/password (Chromium supports this!)
        return {
            "server": f"socks5://{server}",
            "username": formatted_username,
            "password": self.proxy_password,
        }

    @property
    def proxy_server_host(self) -> Optional[str]:
        """Get just the hostname (for logging without credentials)."""
        if not self.proxy_server:
            return None
        server = self.proxy_server
        # Strip any protocol prefix
        for prefix in ["socks5://", "socks4://", "http://", "https://"]:
            if server.startswith(prefix):
                server = server[len(prefix):]
                break
        # Remove port if present for cleaner logging
        return server.split(":")[0] if ":" in server else server

    @property
    def proxy_config_display(self) -> str:
        """Get a safe-to-log proxy config summary (no credentials)."""
        if not self.proxy_enabled:
            return "DISABLED"
        if not self.proxy_server:
            return "NO_SERVER"
        if not self.proxy_username:
            return "NO_USERNAME"
        return f"host={self.proxy_server_host}, user={self.proxy_username[:4]}***_country-{self.proxy_country}_session-{self.proxy_session or 'none'}"

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
