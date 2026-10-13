"""Recent warnings and errors, kept in memory for /fix. Secrets are redacted on the way in.

Railway's log API needs a token Pai doesn't have, so Pai keeps its own short history: the
last MAX_RECORDS WARNING+ records, each with its traceback, newest last.
"""

from __future__ import annotations

import itertools
import logging
import re
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime

MAX_RECORDS = 50
MAX_TRACE_CHARS = 4000
# Token-shaped strings, in case a secret shows up that isn't one of the known values.
PATTERNS = [
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),  # Telegram bot token
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}"),  # Anthropic key
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub token
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)((?:token|key|secret|password|verifier|access_token)=)[^&\s\"']+"),
]


@dataclass
class Record:
    id: int
    at: datetime
    level: str
    logger: str
    message: str
    trace: str


class ErrorLog(logging.Handler):
    def __init__(self, secrets: list[str] | None = None):
        super().__init__(level=logging.WARNING)
        self.records: deque[Record] = deque(maxlen=MAX_RECORDS)
        self.secrets = sorted({s for s in (secrets or []) if s and len(s) >= 8}, key=len)[::-1]
        self._ids = itertools.count(1)

    def add_secret(self, value: str) -> None:
        if value and len(value) >= 8 and value not in self.secrets:
            self.secrets = sorted({*self.secrets, value}, key=len)[::-1]

    def redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "[redacted]")
        for p in PATTERNS:
            text = p.sub(lambda m: (m.group(1) if m.groups() else "") + "[redacted]", text)
        return text

    def emit(self, record: logging.LogRecord) -> None:
        try:
            trace = ""
            if record.exc_info and record.exc_info[1] is not None:
                trace = "".join(traceback.format_exception(*record.exc_info))[-MAX_TRACE_CHARS:]
            self.records.append(
                Record(
                    id=next(self._ids),
                    at=datetime.fromtimestamp(record.created, UTC),
                    level=record.levelname,
                    logger=record.name,
                    message=self.redact(record.getMessage())[:1000],
                    trace=self.redact(trace),
                )
            )
        except Exception:  # never let logging break the app
            self.handleError(record)

    def recent(self, limit: int = 10, min_level: str = "WARNING") -> list[Record]:
        floor = logging.getLevelName(min_level)
        hits = [r for r in self.records if logging.getLevelName(r.level) >= floor]
        return hits[-limit:]

    def get(self, rid: int) -> Record | None:
        return next((r for r in self.records if r.id == rid), None)


LOG = ErrorLog()


def install(secrets: list[str]) -> ErrorLog:
    """Attach the shared buffer to the root logger, redacting these secret values."""
    LOG.secrets = sorted({s for s in secrets if s and len(s) >= 8}, key=len)[::-1]
    root = logging.getLogger()
    if LOG not in root.handlers:
        root.addHandler(LOG)
    return LOG
