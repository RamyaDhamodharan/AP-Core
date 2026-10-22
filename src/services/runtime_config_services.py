"""Runtime checks for configuration that commonly fails inside containers."""

import logging
import os
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def is_running_in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def warn_if_container_redis_url_is_localhost() -> None:
    app_env = os.getenv("APP_ENV", "").casefold()
    if app_env == "dev" or not is_running_in_container():
        return

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    hostname = urlparse(redis_url).hostname
    if hostname == "localhost" or hostname == "::1" or (
        hostname is not None and hostname.startswith("127.")
    ):
        logger.warning(
            "REDIS_URL points to localhost inside a container (APP_ENV=%s); "
            "use the Redis service hostname, for example redis://redis:6379/0",
            app_env or "unset",
        )
