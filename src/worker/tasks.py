"""Celery tasks for the AP document pipeline."""

import uuid

from src.worker.celery_app import celery_app


@celery_app.task(
    name="ap.process_document",
    bind=True,
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def process_document(self, document_id: str) -> None:
    """Parse, classify and extract one document (idempotent)."""
    # Imported lazily so the worker boots without loading the web app.
    from src.api.rest.routers.document_routers import _process_in_background

    _process_in_background(uuid.UUID(document_id))
