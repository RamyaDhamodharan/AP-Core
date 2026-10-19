"""Choose between Celery and FastAPI BackgroundTasks for processing."""

import logging
import os
import uuid

from fastapi import BackgroundTasks

logger = logging.getLogger(__name__)


def dispatch_document_processing(
    background_tasks: BackgroundTasks,
    document_id: uuid.UUID,
    fallback,
) -> str:
    """Queue processing on Celery when enabled, else run in-process."""
    if os.getenv("USE_CELERY", "false").lower() == "true":
        try:
            from src.worker.tasks import process_document

            process_document.delay(str(document_id))
            return "celery"
        except Exception:
            logger.exception(
                "Celery dispatch failed for %s; using in-process fallback",
                document_id,
            )

    background_tasks.add_task(fallback, document_id)
    return "background"
