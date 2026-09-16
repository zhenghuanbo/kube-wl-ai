"""Structured logging setup.

Routes both application and stdlib (uvicorn, SQLAlchemy, Celery) log records
through structlog's `ProcessorFormatter`, so every line is emitted in one
consistent format. Falls back to plain stdlib logging if structlog is missing.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from app.core.config import Settings

try:  # pragma: no cover - exercised implicitly
    import structlog

    _HAS_STRUCTLOG = True
except ImportError:  # pragma: no cover
    _HAS_STRUCTLOG = False
    structlog = None  # type: ignore[assignment]


def _build_renderer(settings: Settings) -> Any:
    assert structlog is not None
    if settings.log_json:
        return structlog.processors.JSONRenderer()
    return structlog.dev.ConsoleRenderer(colors=not settings.is_production)


def configure_logging(settings: Settings) -> None:
    """Configure stdlib logging and (if present) structlog."""
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    if not _HAS_STRUCTLOG:
        logging.basicConfig(
            format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
            stream=sys.stdout,
            level=log_level,
            force=True,
        )
        return

    # Processors shared by structlog-native and foreign (stdlib) records.
    pre_chain: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[*pre_chain, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=pre_chain,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _build_renderer(settings),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)

    # Keep third-party chatty loggers at a sane level.
    for name in ("uvicorn.error", "sqlalchemy.engine", "kubernetes"):
        logging.getLogger(name).setLevel(log_level)


def get_logger(name: str | None = None) -> Any:
    """Return a logger; structlog when available, stdlib otherwise."""
    if _HAS_STRUCTLOG:
        return structlog.get_logger(name)
    return logging.getLogger(name)
