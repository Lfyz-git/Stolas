"""Bounded, secret-safe operational events. CLI result JSON stays on stdout."""
import datetime as dt
import json
import logging
import os
import sys

EVENTS = {
    "service_started", "service_stopped", "configuration_loaded",
    "measurement_started", "measurement_completed", "server_error",
    "measurement_retry", "server_busy", "server_unavailable", "wan_check_failed",
    "speed_degradation_confirmed", "history_error", "api_error", "api_rejected",
    "application_failed",
}
FIELDS = {"test_id", "server", "status", "duration_ms", "download_mbps", "upload_mbps", "attempt", "reason", "http_status", "operation"}
logger = logging.getLogger("stolas")
logger.addHandler(logging.NullHandler())


class EventHandler(logging.Handler):
    def __init__(self, node, stream=None, format="json"):
        super().__init__()
        self.node, self.stream, self.format = node, stream or sys.stderr, format

    def emit(self, record):
        try:
            data = {"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "level": record.levelname, "service": "stolas", "node": self.node,
                    "event": record.event, **record.fields}
            value = json.dumps(data, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            if self.format == "text":
                value = f"{data['timestamp']} {data['level']} {data['event']} " + json.dumps(record.fields, ensure_ascii=True)
            self.stream.write(value + "\n")
            self.stream.flush()
        except Exception:
            # A closed pipe or unavailable collector must not break a measurement.
            pass


def configure(node, stream=None):
    level = os.getenv("STOLAS_LOG_LEVEL", "INFO").upper()
    format = os.getenv("STOLAS_LOG_FORMAT", "json").lower()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL") or format not in ("json", "text"):
        raise ValueError("Invalid logging settings")
    logger.handlers[:] = [EventHandler(node, stream, format)]
    logger.setLevel(level)
    logger.propagate = False


def emit(event, level="INFO", **fields):
    if event not in EVENTS or fields.keys() - FIELDS:
        raise ValueError("Unknown event or event fields")
    # Call sites pass identifiers/numbers and reason codes, never exceptions,
    # request URLs/headers or arbitrary diagnostic strings.
    logger.log(getattr(logging, level), "", extra={"event": event, "fields": fields})
