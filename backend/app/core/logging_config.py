from __future__ import annotations

import contextvars
import json
import logging

# Per-request correlation id, set by the HTTP middleware. Because Starlette
# copies the contextvars context into the threadpool that runs sync endpoints,
# every log line emitted while handling a request — from any module — carries
# the same request_id without threading it through call signatures.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line — greppable and ingest-friendly."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(*, json_logs: bool, level: int = logging.INFO) -> None:
    """Install a single root handler with request-id enrichment.

    JSON in production (machine-parseable); human-readable text in dev.
    """
    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-7s | %(name)s | req=%(request_id)s | %(message)s"
            )
        )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
