from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field

import httpx

from ..config import ProviderConfig

# Подготовленные к отправке части сообщения:
#   {"type": "text", "text": str}
#   {"type": "image", "data": bytes, "mime": str}
#   {"type": "pdf", "data": bytes, "name": str}     — только если provider.native_pdf
Message = dict  # {"role": "user"|"assistant", "parts": [...]}


@dataclass
class ChatResult:
    text: str = ""
    images: list[tuple[bytes, str]] = field(default_factory=list)   # (data, mime)
    tokens: int = 0


class Provider:
    """Один экземпляр = один провайдер + один API-ключ."""

    def __init__(self, cfg: ProviderConfig, api_key: str, timeout: float):
        self.cfg = cfg
        self.api_key = api_key
        self.timeout = timeout
        self.http = httpx.AsyncClient(
            proxy=cfg.proxy or None,
            timeout=httpx.Timeout(timeout, connect=30),
            follow_redirects=True,
        )

    async def chat(self, model: str, system: str, messages: list[Message]) -> ChatResult:
        raise NotImplementedError

    async def list_models(self) -> list[str]:
        raise NotImplementedError

    async def generate_image(self, model: str, prompt: str,
                             refs: list[tuple[bytes, str]]) -> ChatResult:
        raise NotImplementedError("Этот провайдер не умеет генерировать изображения")

    async def transcribe(self, model: str, data: bytes, filename: str, mime: str,
                         language: str | None) -> str:
        raise NotImplementedError("Этот провайдер не умеет распознавать речь")

    async def close(self) -> None:
        await self.http.aclose()

    # ── утилиты ───────────────────────────────────────────────
    def filter_models(self, models: list[str]) -> list[str]:
        if self.cfg.model_exclude:
            rx = re.compile(self.cfg.model_exclude, re.I)
            models = [m for m in models if not rx.search(m)]
        return sorted(set(models))

    async def fetch_url(self, url: str) -> tuple[bytes, str]:
        r = await self.http.get(url)
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "image/png").split(";")[0]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{b64(data)}"


def parse_data_url(url: str) -> tuple[bytes, str] | None:
    m = re.match(r"data:([\w/+.-]+);base64,(.+)", url, re.S)
    return (base64.b64decode(m.group(2)), m.group(1)) if m else None


def merge_roles(messages: list[Message]) -> list[Message]:
    """Склеивает подряд идущие сообщения одной роли и гарантирует, что первое — от user."""
    out: list[Message] = []
    for m in messages:
        if not m["parts"]:
            continue
        if out and out[-1]["role"] == m["role"]:
            out[-1] = {"role": m["role"], "parts": out[-1]["parts"] + m["parts"]}
        else:
            out.append({"role": m["role"], "parts": list(m["parts"])})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out
