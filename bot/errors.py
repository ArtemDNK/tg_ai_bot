"""Классификация ошибок разных SDK: решаем, переключать ли ключ."""
from __future__ import annotations

import asyncio
import re
from enum import Enum

import httpx


class ErrorKind(Enum):
    RATE = "rate"            # 429 по частоте — ключ на короткую паузу
    QUOTA = "quota"          # квота/баланс кончились — ключ на долгую паузу
    AUTH = "auth"            # ключ невалиден/заблокирован
    TRANSIENT = "transient"  # 5xx, таймаут, обрыв — пробуем следующий ключ
    FATAL = "fatal"          # ошибка в самом запросе — другой ключ не поможет


_QUOTA_HINTS = ("insufficient_quota", "exceeded your current quota", "quota exceeded",
                "billing", "credit balance", "perday", "per_day", "per day",
                "insufficient balance", "payment required", "out of credits")
_AUTH_HINTS = ("api key not valid", "invalid api key", "invalid x-api-key", "incorrect api key",
               "api_key_invalid", "permission_denied", "unauthorized", "authentication")


def _status(exc: Exception) -> int | None:
    for attr in ("status_code", "code", "status"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(exc, "response", None)
    v = getattr(resp, "status_code", None)
    return v if isinstance(v, int) else None


def _retry_after(exc: Exception, text: str) -> float | None:
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None)
    if headers is not None:
        for h in ("retry-after", "x-ratelimit-reset-requests"):
            v = headers.get(h)
            if v:
                try:
                    return float(v)
                except ValueError:
                    m = re.match(r"(\d+(?:\.\d+)?)s$", v)
                    if m:
                        return float(m.group(1))
    m = re.search(r"retry(?:delay| in)[\"':\s]*([\d.]+)\s*s", text, re.I)
    return float(m.group(1)) + 1 if m else None


def classify(exc: Exception) -> tuple[ErrorKind, float | None]:
    text = str(exc)
    low = text.lower()
    status = _status(exc)

    if isinstance(exc, (asyncio.TimeoutError, httpx.TransportError)):
        return ErrorKind.TRANSIENT, None
    name = type(exc).__name__
    if name in ("APIConnectionError", "APITimeoutError") or name.endswith(("TransportError", "TimeoutException")):
        return ErrorKind.TRANSIENT, None

    if status in (401, 403) or any(h in low for h in _AUTH_HINTS):
        return ErrorKind.AUTH, None
    if status == 402:
        return ErrorKind.QUOTA, None
    if status == 429 or "resource_exhausted" in low or "rate limit" in low:
        if any(h in low for h in _QUOTA_HINTS):
            return ErrorKind.QUOTA, _retry_after(exc, text) if "perday" not in low else None
        return ErrorKind.RATE, _retry_after(exc, text)
    if status is not None and (status >= 500 or status in (408, 409)):
        return ErrorKind.TRANSIENT, None
    return ErrorKind.FATAL, None


def short_error(exc: Exception) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text if len(text) < 600 else text[:600] + "…"
