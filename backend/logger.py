"""Shared structured JSON logger."""
import os
import json
import logging
import logging.handlers
from datetime import datetime, timezone

LOGS_DIR = os.getenv("LOGS_DIR", "agent_logs")
os.makedirs(LOGS_DIR, exist_ok=True)


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level":     record.levelname,
            "logger":    record.name,
            "message":   record.getMessage(),
            "module":    record.module,
            "line":      record.lineno,
        }
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj)


_rotating_handler = logging.handlers.RotatingFileHandler(
    filename=os.path.join(LOGS_DIR, "agent.log"),
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_rotating_handler.setFormatter(JSONFormatter())

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(JSONFormatter())

logging.basicConfig(level=logging.INFO, handlers=[_rotating_handler, _console_handler])


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
