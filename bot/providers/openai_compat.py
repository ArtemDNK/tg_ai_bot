from __future__ import annotations

import base64

from openai import AsyncOpenAI, DefaultAsyncHttpxClient

from .base import ChatResult, Provider, data_url, merge_roles, parse_data_url


class OpenAIProvider(Provider):
    """OpenAI и любые OpenAI-совместимые API (DeepSeek, OpenRouter, Grok, Mistral, Groq, Qwen, Ollama…)."""

    def __init__(self, cfg, api_key, timeout):
        super().__init__(cfg, api_key, timeout)
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=cfg.base_url or None,
            # у SDK свой httpx-клиент (новые версии используют httpx2)
            http_client=DefaultAsyncHttpxClient(proxy=cfg.proxy or None, timeout=timeout),
            max_retries=0,
        )

    def _convert(self, messages):
        out = []
        for m in merge_roles(messages):
            parts = m["parts"]
            if all(p["type"] == "text" for p in parts):
                # строка, а не массив — так понимают даже провайдеры без мультимодальности
                out.append({"role": m["role"], "content": "\n\n".join(p["text"] for p in parts)})
                continue
            content = []
            for p in parts:
                if p["type"] == "text":
                    content.append({"type": "text", "text": p["text"]})
                elif p["type"] == "image":
                    content.append({"type": "image_url", "image_url": {"url": data_url(p["data"], p["mime"])}})
                elif p["type"] == "pdf":
                    content.append({"type": "file", "file": {
                        "filename": p["name"], "file_data": data_url(p["data"], "application/pdf")}})
            out.append({"role": m["role"], "content": content})
        return out

    async def chat(self, model, system, messages):
        msgs = self._convert(messages)
        if system:
            msgs.insert(0, {"role": "system", "content": system})
        resp = await self.client.chat.completions.create(model=model, messages=msgs, **self.cfg.params)
        if not resp.choices:
            raise RuntimeError(f"Пустой ответ от модели: {resp}")
        msg = resp.choices[0].message
        result = ChatResult(text=msg.content or "",
                            tokens=resp.usage.total_tokens if resp.usage else 0)

        # OpenRouter и некоторые прокси отдают картинки в message.images
        for img in (getattr(msg, "model_extra", None) or {}).get("images") or []:
            url = (img.get("image_url") or {}).get("url") if isinstance(img, dict) else None
            if not url:
                continue
            parsed = parse_data_url(url)
            result.images.append(parsed if parsed else await self.fetch_url(url))
        return result

    async def transcribe(self, model, data, filename, mime, language):
        kwargs = {"language": language} if language else {}
        resp = await self.client.audio.transcriptions.create(
            model=model, file=(filename, data, mime), **kwargs)
        return (getattr(resp, "text", None) or str(resp)).strip()

    async def close(self):
        await self.client.close()
        await super().close()

    async def list_models(self):
        page = await self.client.models.list()
        return self.filter_models([m.id for m in page.data])

    async def generate_image(self, model, prompt, refs):
        # OpenRouter и прочие агрегаторы генерят картинки через chat-модели
        if self.cfg.base_url and "openrouter" in self.cfg.base_url:
            parts = [{"type": "text", "text": prompt}] + [
                {"type": "image", "data": d, "mime": mm} for d, mm in refs]
            resp = await self.client.chat.completions.create(
                model=model, modalities=["image", "text"],
                messages=self._convert([{"role": "user", "parts": parts}]),
            )
            msg = resp.choices[0].message
            res = ChatResult(text=msg.content or "", tokens=resp.usage.total_tokens if resp.usage else 0)
            for img in (msg.model_extra or {}).get("images") or []:
                parsed = parse_data_url(img["image_url"]["url"])
                if parsed:
                    res.images.append(parsed)
            return res

        if refs:
            files = [(f"image{i}.{mime.split('/')[-1]}", data, mime) for i, (data, mime) in enumerate(refs)]
            resp = await self.client.images.edit(model=model, prompt=prompt,
                                                 image=files if len(files) > 1 else files[0])
        else:
            resp = await self.client.images.generate(model=model, prompt=prompt, n=1)

        res = ChatResult()
        for d in resp.data or []:
            if d.b64_json:
                res.images.append((base64.b64decode(d.b64_json), "image/png"))
            elif d.url:
                res.images.append(await self.fetch_url(d.url))
            if getattr(d, "revised_prompt", None):
                res.text = d.revised_prompt
        if resp.usage:
            res.tokens = getattr(resp.usage, "total_tokens", 0) or 0
        return res

