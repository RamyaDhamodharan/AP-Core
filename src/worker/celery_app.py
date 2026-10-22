"""Celery application for background document processing."""

import os

from celery import Celery
from celery.schedules import schedule

from src.services.llm_pipeline_services import validate_llm_provider_config
from src.services.runtime_config_services import (
    warn_if_container_minio_endpoint_is_localhost,
    warn_if_container_redis_url_is_localhost,
)

warn_if_container_redis_url_is_localhost()
warn_if_container_minio_endpoint_is_localhost()
validate_llm_provider_config()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "genworx_ap_core",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["src.worker.tasks"],
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_retry_delay=10,
    broker_connection_retry_on_startup=True,
    task_default_queue="documents",
    task_routes={
        "ap.poll_email": {"queue": "email"},
        "ap.send_notification": {"queue": "email"},
        "ap.process_document": {"queue": "documents"},
    },
)

if (
    os.getenv("EMAIL_POLL_ENABLED", "false").lower() == "true"
    and os.getenv("EMAIL_PROVIDER", "imap").lower() == "imap"
):
    celery_app.conf.beat_schedule = {
        "poll-ap-email": {
            "task": "ap.poll_email",
            "schedule": schedule(
                float(os.getenv("EMAIL_POLL_INTERVAL_SECONDS", "300"))
            ),
        }
    }
