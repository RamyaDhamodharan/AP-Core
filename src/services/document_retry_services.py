"""Bounded retry helpers for transient provider, storage and OCR failures."""

import os
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def is_transient_document_error(error: BaseException) -> bool:
    name = type(error).__name__.casefold()
    message = str(error).casefold()
    return any(
        token in name or token in message
        for token in (
            "timeout",
            "connection",
            "temporarily unavailable",
            "connection reset",
            "service unavailable",
            "rate limit",
            "too many requests",
            " 429",
            " 500",
            " 502",
            " 503",
            " 504",
            "eof",
            "endpoint connection",
        )
    )


def with_transient_retries(
    operation: Callable[[], T], *, sleep: Callable[[float], None] = time.sleep
) -> T:
    attempts = max(1, int(os.getenv("DOCUMENT_PROCESS_MAX_ATTEMPTS", "3")))
    base_delay = max(0.0, float(os.getenv("DOCUMENT_RETRY_BASE_SECONDS", "1")))
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as exc:
            if attempt >= attempts or not is_transient_document_error(exc):
                raise
            sleep(base_delay * (2 ** (attempt - 1)))
    raise RuntimeError("Document processing retry loop exited unexpectedly")
