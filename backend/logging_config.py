"""Structured JSON logging with credential, private key, and URL redaction."""
import json
import logging
import re
from datetime import datetime, timezone


def redact(value):
    if not isinstance(value, str):
        value = str(value)
    # Redact URL basic auth userinfo
    value = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[redacted]@", value)
    # Redact PEM private key blocks
    value = re.sub(r"-----BEGIN[A-Z\s]+PRIVATE KEY-----.*?-----END[A-Z\s]+PRIVATE KEY-----",
                   "[redacted-private-key]", value, flags=re.DOTALL)
    # Redact common authentication headers and signatures
    value = re.sub(r"(KALSHI-ACCESS-KEY:\s*)[^\s,]+", r"\1[redacted]", value, flags=re.IGNORECASE)
    value = re.sub(r"(KALSHI-ACCESS-SIGNATURE:\s*)[^\s,]+", r"\1[redacted]", value, flags=re.IGNORECASE)
    value = re.sub(r"(Bearer\s+)[A-Za-z0-9_\-\.]{8,}", r"\1[redacted]", value, flags=re.IGNORECASE)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record):
        body = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage())
        }
        for field in ("venue", "market_id", "event_id"):
            value = getattr(record, field, None)
            if value is not None:
                body[field] = value
        if record.exc_info:
            body["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(body, separators=(",", ":"))


def configure_logging():
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            handler.setFormatter(JsonFormatter())
    root.setLevel(logging.INFO)
    # httpx's INFO message contains URL userinfo when ClickHouse Basic Auth is used.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
