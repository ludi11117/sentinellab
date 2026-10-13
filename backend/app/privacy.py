"""Redact common credentials before persistence. This is not a complete DLP system."""
import re

SECRET = re.compile(r"password|secret|api[_-]?key|authorization|access[_-]?token", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}")
BEARER = re.compile(r"Bearer\s+[^\s\"']+", re.I)


def redact(value):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if SECRET.search(k) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return EMAIL.sub("[EMAIL]", BEARER.sub("Bearer [REDACTED]", value))
    return value
