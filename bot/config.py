from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROVIDER_TYPES = {"openai", "anthropic", "gemini"}


@dataclass
class KeyConfig:
    key: str
    name: str
    rpm: int = 0
    rpd: int = 0
    tpd: int = 0

    @property
    def id(self) -> str:
        # стабильный идентификатор ключа для БД, без хранения самого ключа
        return hashlib.sha256(self.key.encode()).hexdigest()[:16]

    @property
    def masked(self) -> str:
        k = self.key
        return f"{k[:6]}…{k[-4:]}" if len(k) > 12 else "***"


@dataclass
class ProviderConfig:
    name: str
    title: str
    type: str
    keys: list[KeyConfig]
    base_url: str | None = None
    default_model: str | None = None
    models: list[str] | None = None
    model_exclude: str | None = None
    image_model: str | None = None
    native_pdf: bool = False
    params: dict[str, Any] = field(default_factory=dict)
    proxy: str | None = None


@dataclass
class UserLimits:
    messages_per_day: int = 0
    tokens_per_day: int = 0
    images_per_day: int = 0


@dataclass
class TranscriptionEngine:
    provider: str
    model: str


@dataclass
class TranscriptionConfig:
    enabled: bool = True
    engines: list[TranscriptionEngine] = field(default_factory=list)
    language: str | None = None
    show_text: bool = True


@dataclass
class Config:
    tg_token: str
    tg_proxy: str | None
    admins: set[int]
    allowed_users: set[int]
    allow_all: bool
    default_limits: UserLimits
    limit_overrides: dict[int, UserLimits]
    limits_timezone: str
    default_provider: str
    default_model: str | None
    default_system_prompt: str
    history_max_messages: int
    history_max_images: int
    max_file_chars: int
    request_timeout: float
    providers: dict[str, ProviderConfig]
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)

    def limits_for(self, user_id: int, is_admin: bool) -> UserLimits | None:
        if is_admin:
            return None
        return self.limit_overrides.get(user_id, self.default_limits)


def _parse_key(raw: Any, idx: int) -> KeyConfig:
    if isinstance(raw, str):
        return KeyConfig(key=raw, name=f"key{idx + 1}")
    if isinstance(raw, dict) and raw.get("key"):
        return KeyConfig(
            key=str(raw["key"]),
            name=str(raw.get("name") or f"key{idx + 1}"),
            rpm=int(raw.get("rpm") or 0),
            rpd=int(raw.get("rpd") or 0),
            tpd=int(raw.get("tpd") or 0),
        )
    raise ValueError(f"Некорректный ключ #{idx + 1}: {raw!r}")


def _parse_limits(raw: dict | None) -> UserLimits:
    raw = raw or {}
    return UserLimits(
        messages_per_day=int(raw.get("messages_per_day") or 0),
        tokens_per_day=int(raw.get("tokens_per_day") or 0),
        images_per_day=int(raw.get("images_per_day") or 0),
    )


# движки распознавания по умолчанию, если секция transcription не задана
_AUTO_STT = [("groq", "whisper-large-v3-turbo"), ("openai", "gpt-4o-mini-transcribe")]


def _parse_transcription(raw: dict | None, providers: dict[str, ProviderConfig]) -> TranscriptionConfig:
    raw = raw or {}
    engines: list[TranscriptionEngine] = []
    for e in raw.get("engines") or []:
        if e.get("provider") in providers and e.get("model"):
            if providers[e["provider"]].type == "anthropic":
                raise ValueError("transcription: Anthropic не умеет распознавать аудио")
            engines.append(TranscriptionEngine(e["provider"], e["model"]))
    if not engines:  # не задано или все движки ссылаются на отсутствующих провайдеров
        engines = [TranscriptionEngine(p, m) for p, m in _AUTO_STT if p in providers]
        engines += [TranscriptionEngine(p.name, p.default_model) for p in providers.values()
                    if p.type == "gemini" and p.default_model]
    return TranscriptionConfig(
        enabled=bool(raw.get("enabled", True)) and bool(engines),
        engines=engines,
        language=raw.get("language") or None,
        show_text=bool(raw.get("show_text", True)),
    )


def load_config(path: str | Path) -> Config:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    tg = data.get("telegram") or {}
    if not tg.get("token"):
        raise ValueError("telegram.token не задан")

    access = data.get("access") or {}
    if not access.get("admins"):
        raise ValueError("access.admins пуст — укажи хотя бы один Telegram ID администратора")
    limits = data.get("user_limits") or {}
    defaults = data.get("defaults") or {}
    history = data.get("history") or {}
    ai_proxy = data.get("ai_proxy")

    providers: dict[str, ProviderConfig] = {}
    for name, p in (data.get("providers") or {}).items():
        p = p or {}
        ptype = p.get("type", "openai")
        if ptype not in PROVIDER_TYPES:
            raise ValueError(f"Провайдер {name}: неизвестный type={ptype}")
        keys = [_parse_key(k, i) for i, k in enumerate(p.get("keys") or [])]
        if not keys:
            continue  # провайдер без ключей просто не показываем
        providers[name] = ProviderConfig(
            name=name,
            title=p.get("title") or name,
            type=ptype,
            keys=keys,
            base_url=p.get("base_url"),
            default_model=p.get("default_model"),
            models=p.get("models"),
            model_exclude=p.get("model_exclude"),
            image_model=p.get("image_model"),
            native_pdf=bool(p.get("native_pdf", False)),
            params=dict(p.get("params") or {}),
            proxy=p["proxy"] if "proxy" in p else ai_proxy,
        )
    if not providers:
        raise ValueError("Нет ни одного провайдера с ключами")

    default_provider = defaults.get("provider") or next(iter(providers))
    if default_provider not in providers:
        default_provider = next(iter(providers))

    return Config(
        tg_token=tg["token"],
        tg_proxy=tg.get("proxy"),
        admins={int(x) for x in access.get("admins") or []},
        allowed_users={int(x) for x in access.get("allowed_users") or []},
        allow_all=bool(access.get("allow_all", False)),
        default_limits=_parse_limits(limits.get("default")),
        limit_overrides={int(k): _parse_limits(v) for k, v in (limits.get("overrides") or {}).items()},
        limits_timezone=data.get("limits_timezone") or "UTC",
        default_provider=default_provider,
        default_model=defaults.get("model"),
        default_system_prompt=defaults.get("system_prompt") or "",
        history_max_messages=int(history.get("max_messages", 30)),
        history_max_images=int(history.get("max_images", 4)),
        max_file_chars=int(history.get("max_file_chars", 200_000)),
        request_timeout=float(data.get("request_timeout", 300)),
        providers=providers,
        transcription=_parse_transcription(data.get("transcription"), providers),
    )
