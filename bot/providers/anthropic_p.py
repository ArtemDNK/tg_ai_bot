from __future__ import annotations

from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from .base import ChatResult, Provider, b64, merge_roles

ANTHROPIC_IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


class AnthropicProvider(Provider):
    def __init__(self, cfg, api_key, timeout):
        super().__init__(cfg, api_key, timeout)
        self.client = AsyncAnthropic(
            api_key=api_key,
            base_url=cfg.base_url or None,
            http_client=DefaultAsyncHttpxClient(proxy=cfg.proxy or None, timeout=timeout),
            max_retries=0,
        )

    def _convert(self, messages):
        out = []
        for m in merge_roles(messages):
            content = []
            for p in m["parts"]:
                if p["type"] == "text":
                    if p["text"].strip():
                        content.append({"type": "text", "text": p["text"]})
                elif p["type"] == "image" and p["mime"] in ANTHROPIC_IMAGE_MIMES:
                    content.append({"type": "image", "source": {
                        "type": "base64", "media_type": p["mime"], "data": b64(p["data"])}})
                elif p["type"] == "pdf":
                    content.append({"type": "document", "title": p["name"], "source": {
                        "type": "base64", "media_type": "application/pdf", "data": b64(p["data"])}})
            if content:
                out.append({"role": m["role"], "content": content})
        return out

    async def chat(self, model, system, messages):
        params = dict(self.cfg.params)
        params.setdefault("max_tokens", 8192)
        kwargs = {"system": system} if system else {}
        resp = await self.client.messages.create(
            model=model, messages=self._convert(messages), **kwargs, **params)
        text = "".join(b.text for b in resp.content if b.type == "text")
        tokens = (resp.usage.input_tokens or 0) + (resp.usage.output_tokens or 0) if resp.usage else 0
        return ChatResult(text=text, tokens=tokens)

    async def list_models(self):
        ids = []
        async for m in self.client.models.list(limit=100):
            ids.append(m.id)
        return self.filter_models(ids)

    async def close(self):
        await self.client.close()
        await super().close()
