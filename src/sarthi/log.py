"""Logging setup with secret redaction.

Every log record passes through ``RedactingFilter``, which masks values that look like
API keys, bot tokens, bearer tokens and passwords, as a second line of defence after ``SecretStr``.
"""

from __future__ import annotations

import logging
import re

_MASK = "***"

# Whole match is the secret.
_SECRET_VALUES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),  # Telegram bot token
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),  # OpenAI-style keys
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b"),  # Groq keys
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),  # Google API keys
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}"),
)
# Only the value after "name=" or "name:" is the secret; the name is kept for context.
_SECRET_ASSIGNMENT = re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token)(\s*[=:]\s*)\S+")


def redact(text: str) -> str:
    """Return ``text`` with anything that looks like a secret replaced by ``***``."""
    for pattern in _SECRET_VALUES:
        text = pattern.sub(_MASK, text)
    return _SECRET_ASSIGNMENT.sub(lambda m: f"{m.group(1)}{m.group(2)}{_MASK}", text)


_PLACEHOLDER = re.compile(r"%(?!%)[-#0 +]*\d*(?:\.\d+)?[diouxXeEfFgGcrsa]")


def _as_text(record: logging.LogRecord) -> str:
    """The message with every placeholder read as ``%s``; the raw parts when even that fails.

    Each value stays next to its name (``token=...``), so redaction still finds it.
    """
    try:
        return _PLACEHOLDER.sub("%s", str(record.msg)) % record.args
    except (TypeError, ValueError):
        return f"{record.msg} {record.args!r}"


class RedactingFilter(logging.Filter):
    """Logging filter that redacts secrets from the fully formatted message."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Replace the record's message with its redacted, fully formatted form. Never drops a record.

        A log call whose arguments do not fit its format (``%d`` given None) is still logged, as the raw
        format and arguments: an error raised here would reach, and break, the code that logged.
        """
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            message = _as_text(record)
        record.msg = redact(message)
        record.args = None
        return True


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once, with redaction on every handler."""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
