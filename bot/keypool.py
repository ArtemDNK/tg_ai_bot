"""Пул API-ключей: ротация по порядку, локальные лимиты и откат при ошибках API."""
from __future__ import annotations

import html
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import Config, KeyConfig, ProviderConfig
from .db import Database
from .errors import ErrorKind

log = logging.getLogger(__name__)

COOLDOWNS = {
    ErrorKind.RATE: 60,
    ErrorKind.QUOTA: 3600,
    ErrorKind.AUTH: 24 * 3600,
    ErrorKind.TRANSIENT: 20,
}


class NoKeyAvailable(Exception):
    def __init__(self, provider: str, wait: float, reasons: list[str]):
        self.provider = provider
        self.wait = wait
        self.reasons = reasons
        super().__init__(f"Все ключи провайдера {provider} недоступны")


@dataclass
class KeyState:
    cfg: KeyConfig
    provider: str
    day: str = ""
    req_today: int = 0
    tok_today: int = 0
    recent: deque = field(default_factory=deque)   # timestamps запросов за последнюю минуту
    cooldown_until: float = 0.0
    cooldown_reason: str = ""
    last_error: str = ""


class KeyPool:
    def __init__(self, db: Database, cfg: Config):
        self.db = db
        self.tz = ZoneInfo(cfg.limits_timezone)
        self.states: dict[str, list[KeyState]] = {}
        self._loaded_day: dict[str, str] = {}
        self.apply_config(cfg)

    # ── конфигурация ──────────────────────────────────────────
    def apply_config(self, cfg: Config) -> None:
        """Перестраивает пул после /reload, сохраняя состояние уже известных ключей."""
        self.tz = ZoneInfo(cfg.limits_timezone)
        old = {(s.provider, s.cfg.id): s for lst in self.states.values() for s in lst}
        self.states = {}
        for name, prov in cfg.providers.items():
            lst = []
            for k in prov.keys:
                st = old.get((name, k.id))
                if st:
                    st.cfg = k  # лимиты могли поменяться
                else:
                    st = KeyState(cfg=k, provider=name)
                lst.append(st)
            self.states[name] = lst
        self._loaded_day = {}

    def today(self) -> str:
        return datetime.now(self.tz).strftime("%Y-%m-%d")

    def seconds_to_midnight(self) -> float:
        now = datetime.now(self.tz)
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return (tomorrow - now).total_seconds()

    async def _ensure_day(self, provider: str) -> None:
        day = self.today()
        if self._loaded_day.get(provider) == day:
            return
        for st in self.states.get(provider, []):
            if st.day != day:
                u = await self.db.get_usage(day, "key", st.cfg.id)
                st.day, st.req_today, st.tok_today = day, u.requests, u.tokens
        self._loaded_day[provider] = day

    # ── выбор ключа ───────────────────────────────────────────
    def _blocked(self, st: KeyState, now: float) -> tuple[float, str] | None:
        """None — ключ свободен, иначе (сколько ждать, причина)."""
        if st.cooldown_until > now:
            return st.cooldown_until - now, st.cooldown_reason
        while st.recent and now - st.recent[0] > 60:
            st.recent.popleft()
        c = st.cfg
        if c.rpm and len(st.recent) >= c.rpm:
            return 60 - (now - st.recent[0]), f"лимит {c.rpm} запросов/мин"
        if c.rpd and st.req_today >= c.rpd:
            return self.seconds_to_midnight(), f"лимит {c.rpd} запросов/сутки"
        if c.tpd and st.tok_today >= c.tpd:
            return self.seconds_to_midnight(), f"лимит {c.tpd} токенов/сутки"
        return None

    async def acquire(self, provider: str, exclude: set[str]) -> KeyState:
        await self._ensure_day(provider)
        now = time.time()
        waits, reasons = [], []
        for st in self.states.get(provider, []):
            if st.cfg.id in exclude:
                continue
            blocked = self._blocked(st, now)
            if blocked is None:
                st.recent.append(now)
                st.req_today += 1
                await self.db.add_usage(st.day, "key", st.cfg.id, requests=1)
                return st
            waits.append(blocked[0])
            reasons.append(f"{st.cfg.name}: {blocked[1]}")
        raise NoKeyAvailable(provider, min(waits) if waits else 0, reasons)

    async def add_tokens(self, st: KeyState, tokens: int) -> None:
        if tokens:
            st.tok_today += tokens
            await self.db.add_usage(st.day, "key", st.cfg.id, tokens=tokens)

    def penalize(self, st: KeyState, kind: ErrorKind, retry_after: float | None, message: str) -> None:
        seconds = retry_after or COOLDOWNS.get(kind, 30)
        if kind == ErrorKind.QUOTA and not retry_after:
            seconds = min(COOLDOWNS[ErrorKind.QUOTA], self.seconds_to_midnight())
        st.cooldown_until = time.time() + seconds
        st.cooldown_reason = {
            ErrorKind.RATE: "429 от API",
            ErrorKind.QUOTA: "квота исчерпана",
            ErrorKind.AUTH: "ключ отклонён",
            ErrorKind.TRANSIENT: "ошибка сервера",
        }.get(kind, kind.value)
        st.last_error = message[:300]
        log.warning("Ключ %s/%s на паузе %ds: %s (%s)", st.provider, st.cfg.name,
                    seconds, st.cooldown_reason, message[:200])

    def reset_cooldowns(self, provider: str | None = None) -> None:
        for name, lst in self.states.items():
            if provider and name != provider:
                continue
            for st in lst:
                st.cooldown_until = 0

    # ── отчёт для /keys ───────────────────────────────────────
    async def summary(self, prov: ProviderConfig) -> tuple[int, int]:
        """(всего ключей, из них сейчас недоступны)."""
        await self._ensure_day(prov.name)
        now = time.time()
        lst = self.states.get(prov.name, [])
        return len(lst), sum(1 for st in lst if self._blocked(st, now) is not None)

    async def report(self, prov: ProviderConfig) -> list[str]:
        await self._ensure_day(prov.name)
        now = time.time()
        lines = []
        for i, st in enumerate(self.states.get(prov.name, []), 1):
            c = st.cfg
            blocked = self._blocked(st, now)
            status = "🟢" if blocked is None else f"🔴 {blocked[1]}, ещё {fmt_wait(blocked[0])}"
            lim = lambda used, cap: f"{used}/{cap}" if cap else f"{used}"
            lines.append(
                f"{i}. {html.escape(c.name)} <code>{html.escape(c.masked)}</code>"
                f"{'' if c.db_id else ' · config.yaml'} — {status}\n"
                f"   мин: {lim(len(st.recent), c.rpm)} · сутки: {lim(st.req_today, c.rpd)} req, "
                f"{lim(st.tok_today, c.tpd)} tok"
            )
        return lines


def fmt_wait(seconds: float) -> str:
    seconds = max(1, int(seconds))
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    return f"{seconds // 3600} ч {seconds % 3600 // 60} мин"
