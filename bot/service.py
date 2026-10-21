"""Ядро: история, выбор провайдера/модели, ротация ключей, лимиты пользователей."""
from __future__ import annotations

import logging
import mimetypes
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

from .config import Config, ProviderConfig
from .db import Database
from .errors import ErrorKind, classify, short_error
from .keypool import KeyPool, NoKeyAvailable, fmt_wait
from .providers import ChatResult, Provider, ProviderFactory
from .render import FILE_PROMPT

log = logging.getLogger(__name__)
T = TypeVar("T")
MODELS_TTL = 3600


class UserLimitExceeded(Exception):
    pass


class AIError(Exception):
    """Ошибка, которую можно показать пользователю как есть."""


@dataclass
class AccessEntry:
    role: str              # 'user' | 'admin' | 'blocked'
    name: str | None


class AIService:
    def __init__(self, cfg: Config, db: Database, files_dir: Path):
        self.cfg = cfg
        self.db = db
        self.pool = KeyPool(db, cfg)
        self.factory = ProviderFactory(cfg.request_timeout)
        self.files_dir = files_dir
        self._models: dict[str, tuple[float, list[str]]] = {}
        self.on_key_auth_error: Callable[[str], Awaitable[None]] | None = None
        self.access: dict[int, AccessEntry] = {}

    async def load_access(self) -> None:
        self.access = {uid: AccessEntry(role, name) for uid, role, name in await self.db.list_access()}

    # ── доступ ────────────────────────────────────────────────
    def is_superadmin(self, user_id: int) -> bool:
        """Админы из config.yaml: их нельзя снять из бота."""
        return user_id in self.cfg.admins

    def is_admin(self, user_id: int) -> bool:
        e = self.access.get(user_id)
        return self.is_superadmin(user_id) or (e is not None and e.role == "admin")

    def is_blocked(self, user_id: int) -> bool:
        e = self.access.get(user_id)
        return e is not None and e.role == "blocked" and not self.is_superadmin(user_id)

    def is_allowed(self, user_id: int) -> bool:
        if self.is_superadmin(user_id):
            return True
        e = self.access.get(user_id)
        if e is not None:
            return e.role in ("user", "admin")
        return self.cfg.allow_all or user_id in self.cfg.allowed_users

    def admin_ids(self) -> set[int]:
        return set(self.cfg.admins) | {u for u, e in self.access.items() if e.role == "admin"}

    async def set_role(self, user_id: int, role: str, name: str | None, by: int) -> None:
        if self.is_superadmin(user_id):
            raise AIError("Это администратор из config.yaml — его права меняются только в конфиге")
        await self.db.set_access(user_id, role, name, by)
        prev = self.access.get(user_id)
        self.access[user_id] = AccessEntry(role, name or (prev.name if prev else None))

    async def revoke(self, user_id: int) -> bool:
        if self.is_superadmin(user_id):
            raise AIError("Это администратор из config.yaml — его права меняются только в конфиге")
        self.access.pop(user_id, None)
        return await self.db.delete_access(user_id)

    def reload(self, cfg: Config) -> None:
        self.cfg = cfg
        self.pool.apply_config(cfg)
        self.factory.timeout = cfg.request_timeout
        self._models.clear()

    # ── файлы пользователя ────────────────────────────────────
    def save_file(self, user_id: int, data: bytes, ext: str) -> str:
        d = self.files_dir / str(user_id)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{uuid.uuid4().hex}{ext}"
        p.write_bytes(data)
        return str(p)

    async def reset(self, user_id: int) -> None:
        await self.db.clear_history(user_id)
        shutil.rmtree(self.files_dir / str(user_id), ignore_errors=True)

    # ── настройки пользователя ────────────────────────────────
    async def resolve(self, user_id: int) -> tuple[ProviderConfig, str, str]:
        s = await self.db.get_user(user_id)
        prov = self.cfg.providers.get(s.provider or "") or self.cfg.providers[self.cfg.default_provider]
        model = s.model if s.provider == prov.name and s.model else None
        if not model:
            if prov.name == self.cfg.default_provider and self.cfg.default_model:
                model = self.cfg.default_model
            else:
                model = prov.default_model or (await self.get_models(prov.name) or [None])[0]
        if not model:
            raise AIError(f"У провайдера {prov.title} не задана модель — выбери её через /model")
        system = s.system_prompt if s.system_prompt is not None else self.cfg.default_system_prompt
        return prov, model, system

    # ── ротация ключей ────────────────────────────────────────
    async def run_with_keys(self, prov: ProviderConfig, fn: Callable[[Provider], Awaitable[T]]) -> tuple[T, object]:
        tried: set[str] = set()
        last_exc: Exception | None = None
        while True:
            try:
                st = await self.pool.acquire(prov.name, tried)
            except NoKeyAvailable as e:
                detail = "\n".join(f"• {r}" for r in e.reasons)
                last = f"\nПоследняя ошибка: {short_error(last_exc)}" if last_exc else ""
                raise AIError(
                    f"Все ключи {prov.title} сейчас недоступны"
                    + (f", ближайший освободится через {fmt_wait(e.wait)}" if e.wait else "")
                    + f".\n{detail}{last}"
                ) from last_exc
            client = self.factory.get(prov, st.cfg.key)
            try:
                result = await fn(client)
                return result, st
            except Exception as exc:  # noqa: BLE001
                kind, retry_after = classify(exc)
                if kind == ErrorKind.FATAL:
                    raise AIError(f"{prov.title}: {short_error(exc)}") from exc
                self.pool.penalize(st, kind, retry_after, short_error(exc))
                if kind == ErrorKind.AUTH and self.on_key_auth_error:
                    await self.on_key_auth_error(
                        f"⚠️ Ключ {prov.title} / {st.cfg.name} ({st.cfg.masked}) отклонён API:\n{short_error(exc)}")
                tried.add(st.cfg.id)
                last_exc = exc

    # ── модели ────────────────────────────────────────────────
    async def get_models(self, provider: str, force: bool = False) -> list[str]:
        prov = self.cfg.providers[provider]
        if prov.models:
            return list(prov.models)
        cached = self._models.get(provider)
        if cached and not force and time.time() - cached[0] < MODELS_TTL:
            return cached[1]
        try:
            models, _ = await self.run_with_keys(prov, lambda c: c.list_models())
        except AIError as e:
            log.warning("Список моделей %s недоступен: %s", provider, e)
            models = []
        if prov.default_model and prov.default_model not in models:
            models.insert(0, prov.default_model)
        self._models[provider] = (time.time(), models)
        return models

    # ── лимиты пользователя ───────────────────────────────────
    async def check_limits(self, user_id: int, image: bool = False) -> None:
        lim = self.cfg.limits_for(user_id, self.is_admin(user_id))
        if lim is None:
            return
        u = await self.db.get_usage(self.pool.today(), "user", str(user_id))
        wait = fmt_wait(self.pool.seconds_to_midnight())
        if lim.messages_per_day and u.requests >= lim.messages_per_day:
            raise UserLimitExceeded(f"Дневной лимит сообщений ({lim.messages_per_day}) исчерпан. Сброс через {wait}.")
        if lim.tokens_per_day and u.tokens >= lim.tokens_per_day:
            raise UserLimitExceeded(f"Дневной лимит токенов ({lim.tokens_per_day}) исчерпан. Сброс через {wait}.")
        if image and lim.images_per_day and u.images >= lim.images_per_day:
            raise UserLimitExceeded(f"Дневной лимит картинок ({lim.images_per_day}) исчерпан. Сброс через {wait}.")

    async def _account(self, user_id: int, st, res: ChatResult, image: bool = False) -> None:
        await self.pool.add_tokens(st, res.tokens)
        await self.db.add_usage(self.pool.today(), "user", str(user_id),
                                requests=1, tokens=res.tokens, images=len(res.images) if image else 0)

    # ── подготовка истории ────────────────────────────────────
    def _prepare(self, history: list[dict], prov: ProviderConfig) -> list[dict]:
        budget = self.cfg.history_max_images
        prepared: list[dict] = []
        for msg in reversed(history):  # с конца, чтобы картинки оставались самые свежие
            parts = []
            for p in msg["parts"]:
                t = p["type"]
                if t == "text":
                    parts.append(p)
                elif t == "image":
                    path = Path(p["path"])
                    # OpenAI и Anthropic не принимают картинки в сообщениях ассистента
                    own_ok = msg["role"] == "user" or prov.type == "gemini"
                    if own_ok and budget > 0 and path.exists():
                        parts.append({"type": "image", "data": path.read_bytes(), "mime": p["mime"]})
                        budget -= 1
                    else:
                        parts.append({"type": "text", "text": "[изображение]"})
                elif t == "file":
                    path = Path(p["path"])
                    if prov.native_pdf and p["mime"] == "application/pdf" and path.exists():
                        parts.append({"type": "pdf", "data": path.read_bytes(), "name": p["name"]})
                    if p.get("text"):
                        parts.append({"type": "text",
                                      "text": f"📎 Файл «{p['name']}»:\n```\n{p['text']}\n```"})
                    elif not (prov.native_pdf and p["mime"] == "application/pdf"):
                        parts.append({"type": "text", "text": f"📎 Файл «{p['name']}» (текст извлечь не удалось)"})
            prepared.append({"role": msg["role"], "parts": parts})
        return list(reversed(prepared))

    # ── основные операции ─────────────────────────────────────
    async def chat(self, user_id: int, parts: list[dict]) -> ChatResult:
        await self.check_limits(user_id)
        prov, model, system = await self.resolve(user_id)
        await self.db.add_message(user_id, "user", parts)
        history = await self.db.get_history(user_id, self.cfg.history_max_messages)
        messages = self._prepare(history, prov)
        try:
            res, st = await self.run_with_keys(
                prov, lambda c: c.chat(model, (system or "") + FILE_PROMPT, messages))
        except Exception:
            await self.db.drop_last_user_message(user_id)
            raise
        await self._account(user_id, st, res)
        await self._save_assistant(user_id, res)
        return res

    async def image(self, user_id: int, prompt: str, refs: list[tuple[bytes, str]]) -> ChatResult:
        await self.check_limits(user_id, image=True)
        prov, _, _ = await self.resolve(user_id)
        if not prov.image_model:
            candidates = [p for p in self.cfg.providers.values() if p.image_model]
            if not candidates:
                raise AIError("Ни у одного провайдера не задан image_model в конфиге")
            prov = candidates[0]
        res, st = await self.run_with_keys(
            prov, lambda c: c.generate_image(prov.image_model, prompt, refs))
        if not res.images:
            raise AIError(f"{prov.title} не вернул изображение. {res.text}".strip())
        await self._account(user_id, st, res, image=True)
        # кладём в историю, чтобы можно было обсуждать/править результат в чате
        await self.db.add_message(user_id, "user", [{"type": "text", "text": f"Сгенерируй картинку: {prompt}"}])
        await self._save_assistant(user_id, res)
        return res

    async def _save_assistant(self, user_id: int, res: ChatResult) -> None:
        parts: list[dict] = []
        if res.text:
            parts.append({"type": "text", "text": res.text})
        for data, mime in res.images:
            ext = mimetypes.guess_extension(mime) or ".png"
            parts.append({"type": "image", "path": self.save_file(user_id, data, ext), "mime": mime})
        if parts:
            await self.db.add_message(user_id, "assistant", parts)

    # ── распознавание речи ────────────────────────────────────
    async def transcribe(self, user_id: int, data: bytes, filename: str, mime: str) -> str:
        tc = self.cfg.transcription
        if not tc.enabled:
            raise AIError("Распознавание голосовых не настроено (секция transcription в конфиге)")
        await self.check_limits(user_id)
        errors = []
        for eng in tc.engines:
            prov = self.cfg.providers.get(eng.provider)
            if not prov:
                continue
            try:
                text, _ = await self.run_with_keys(
                    prov, lambda c: c.transcribe(eng.model, data, filename, mime, tc.language))
            except AIError as e:  # пробуем следующий движок
                log.warning("STT %s: %s", eng.provider, e)
                errors.append(f"{prov.title}/{eng.model}: {e}")
                continue
            if text:
                return text
            errors.append(f"{prov.title}/{eng.model}: пустая расшифровка")
        raise AIError("Не удалось распознать голосовое.\n" + "\n".join(errors))
