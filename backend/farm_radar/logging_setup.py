"""JSON形式のログ。RPCのURLに含まれるAPIキーはマスクする。"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

# Alchemy などのURLの末尾にあるキー部分（/v2/<key>）を隠す
_KEY_IN_URL = re.compile(r"(/v\d+/)[A-Za-z0-9_\-]{8,}")


def redact(text: str) -> str:
    return _KEY_IN_URL.sub(r"\1***", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "data", None)
        if extra:
            payload["data"] = extra
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, ensure_ascii=False, default=str))


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
