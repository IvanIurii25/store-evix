"""Celery application instance for the evix-store backend.

The broker and result backend both point at a dedicated Redis DB (``/1`` — see
``settings.celery_broker_url`` / ``celery_result_backend``), separate from the
rate-limit/blacklist DB (``/0``). Task modules are auto-discovered via the
``include`` list; new task modules must be added there to be registered in the
worker.

Note on the async-DB pattern: tasks run in a prefork worker and MUST NOT reuse
``app.core.db.engine`` (its asyncpg pool is created at import and is fork-unsafe).
Use :func:`app.tasks.base.run_async_session` for any DB work in a task.
"""

from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "evix",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.tasks.health"],
)
"""The shared Celery app. Import this in task modules to register tasks and in
the worker CLI (``celery -A app.core.celery_app``)."""

celery_app.conf.update(
    # JSON-only (de)serialization: no pickle → no arbitrary-code-exec on the
    # broker payload, and portable across languages.
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    # Store/compare all times in UTC; the app has no local-time semantics.
    timezone="UTC",
    enable_utc=True,
    # Ack a message only AFTER the task returns, so a worker crash mid-task
    # leaves the message on the broker to be redelivered (at-least-once).
    task_acks_late=True,
    # With acks_late, prefetch 1 gives fair dispatch — a worker holds at most
    # one un-acked task instead of hoarding a batch it might crash on.
    worker_prefetch_multiplier=1,
    # Keep retrying the broker connection during worker startup instead of
    # failing fast (broker may boot slightly after the worker).
    broker_connection_retry_on_startup=True,
    # Drop task results from the backend after an hour — health/infra tasks
    # have no long-lived result to keep.
    result_expires=3600,
)
