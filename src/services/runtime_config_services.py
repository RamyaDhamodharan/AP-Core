"""
Runtime checks for configuration that commonly fails inside containers.
Flow: shared business-flow service called by API routes and workers.
Called by: src/api/rest/app.py, src/worker/celery_app.py.
Calls: standard-library or framework helpers.
Tables: no database tables directly.
"""

import logging
import os
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def is_running_in_container() -> bool:
    """
    Handle is running in container.
    Inputs: no explicit inputs.
    Output: bool.
    Effects: does not write database rows or queue work directly.
    Results: no workflow status or reason code directly.
    """
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _is_localhost_endpoint(value: str) -> bool:
    candidate = value if "://" in value else f"//{value}"
    hostname = urlparse(candidate).hostname
    return hostname == "localhost" or hostname == "::1" or (
        hostname is not None and hostname.startswith("127.")
    )


def warn_if_container_redis_url_is_localhost() -> None:
    """
    Handle warn if container redis url is localhost.
    Inputs: no explicit inputs.
    Output: None.
    Effects: does not write database rows or queue work directly.
    Results: no workflow status or reason code directly.
    """
    app_env = os.getenv("APP_ENV", "").casefold()
    if app_env == "dev" or not is_running_in_container():
        return

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    if _is_localhost_endpoint(redis_url):
        logger.warning(
            "REDIS_URL points to localhost inside a container (APP_ENV=%s); "
            "use the Redis service hostname, for example redis://redis:6379/0",
            app_env or "unset",
        )


def warn_if_container_minio_endpoint_is_localhost() -> None:
    """
    Handle warn if container minio endpoint is localhost.
    Inputs: no explicit inputs.
    Output: None.
    Effects: does not write database rows or queue work directly.
    Results: no workflow status or reason code directly.
    """
    app_env = os.getenv("APP_ENV", "").casefold()
    if app_env == "dev" or not is_running_in_container():
        return

    endpoint = os.getenv("MINIO_ENDPOINT", "http://localhost:9000")
    if _is_localhost_endpoint(endpoint):
        logger.warning(
            "MINIO_ENDPOINT points to localhost inside a container (APP_ENV=%s); "
            "use the reachable MinIO service or host endpoint",
            app_env or "unset",
        )
