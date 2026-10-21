from __future__ import annotations

from ..config import ProviderConfig
from .anthropic_p import AnthropicProvider
from .base import ChatResult, Provider
from .gemini_p import GeminiProvider
from .openai_compat import OpenAIProvider

_TYPES = {"openai": OpenAIProvider, "anthropic": AnthropicProvider, "gemini": GeminiProvider}


class ProviderFactory:
    """Кэширует клиентов по (провайдер, ключ), чтобы переиспользовать соединения."""

    def __init__(self, timeout: float):
        self.timeout = timeout
        self._cache: dict[tuple, Provider] = {}

    def get(self, cfg: ProviderConfig, api_key: str) -> Provider:
        ck = (cfg.name, api_key, cfg.base_url, cfg.proxy, cfg.type, repr(sorted(cfg.params.items())),
              cfg.model_exclude)
        inst = self._cache.get(ck)
        if inst is None:
            inst = _TYPES[cfg.type](cfg, api_key, self.timeout)
            self._cache[ck] = inst
        else:
            inst.cfg = cfg  # после /reload: те же ключевые поля, свежие прочие настройки
        return inst

    async def close(self) -> None:
        for p in self._cache.values():
            await p.close()
        self._cache.clear()


__all__ = ["ChatResult", "Provider", "ProviderFactory"]
