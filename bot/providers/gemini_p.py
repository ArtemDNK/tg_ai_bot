from __future__ import annotations

from google import genai
from google.genai import types

from .base import ChatResult, Provider, merge_roles


class GeminiProvider(Provider):
    def __init__(self, cfg, api_key, timeout):
        super().__init__(cfg, api_key, timeout)
        http_options = types.HttpOptions(
            httpx_async_client=self.http,          # свой httpx — ради прокси
            timeout=int(timeout * 1000),
        )
        if cfg.base_url:
            http_options.base_url = cfg.base_url
        self.client = genai.Client(api_key=api_key, http_options=http_options)

    @staticmethod
    def _convert(messages):
        contents = []
        for m in merge_roles(messages):
            parts = []
            for p in m["parts"]:
                if p["type"] == "text":
                    if p["text"].strip():
                        parts.append(types.Part.from_text(text=p["text"]))
                elif p["type"] == "image":
                    parts.append(types.Part.from_bytes(data=p["data"], mime_type=p["mime"]))
                elif p["type"] == "pdf":
                    parts.append(types.Part.from_bytes(data=p["data"], mime_type="application/pdf"))
            if parts:
                contents.append(types.Content(role="model" if m["role"] == "assistant" else "user",
                                              parts=parts))
        return contents

    @staticmethod
    def _parse(resp) -> ChatResult:
        res = ChatResult()
        if resp.usage_metadata:
            res.tokens = resp.usage_metadata.total_token_count or 0
        if not resp.candidates:
            reason = getattr(resp.prompt_feedback, "block_reason", None)
            raise RuntimeError(f"Gemini не вернул ответ (block_reason={reason})")
        cand = resp.candidates[0]
        texts = []
        for part in (cand.content.parts if cand.content and cand.content.parts else []):
            if getattr(part, "thought", False):
                continue
            if part.text:
                texts.append(part.text)
            elif part.inline_data and part.inline_data.data:
                res.images.append((part.inline_data.data, part.inline_data.mime_type or "image/png"))
        res.text = "".join(texts)
        if not res.text and not res.images:
            res.text = f"(пустой ответ, finish_reason={cand.finish_reason})"
        return res

    def _config(self, model, system):
        params = dict(self.cfg.params)
        if "max_tokens" in params:
            params["max_output_tokens"] = params.pop("max_tokens")
        if "image" in model and "response_modalities" not in params:
            params["response_modalities"] = ["TEXT", "IMAGE"]
        return types.GenerateContentConfig(system_instruction=system or None, **params)

    async def chat(self, model, system, messages):
        resp = await self.client.aio.models.generate_content(
            model=model, contents=self._convert(messages), config=self._config(model, system))
        return self._parse(resp)

    async def list_models(self):
        ids = []
        async for m in await self.client.aio.models.list(config={"page_size": 200}):
            actions = m.supported_actions or []
            if "generateContent" in actions or "predict" in actions:
                ids.append(m.name.removeprefix("models/"))
        return self.filter_models(ids)

    async def generate_image(self, model, prompt, refs):
        if model.startswith("imagen"):
            if refs:
                raise RuntimeError("Imagen не принимает исходные картинки — используй gemini-*-image модель")
            resp = await self.client.aio.models.generate_images(
                model=model, prompt=prompt, config=types.GenerateImagesConfig(number_of_images=1))
            return ChatResult(images=[(g.image.image_bytes, g.image.mime_type or "image/png")
                                      for g in resp.generated_images or [] if g.image])
        parts = [{"type": "text", "text": prompt}] + [
            {"type": "image", "data": d, "mime": mm} for d, mm in refs]
        resp = await self.client.aio.models.generate_content(
            model=model, contents=self._convert([{"role": "user", "parts": parts}]),
            config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]))
        return self._parse(resp)

    async def transcribe(self, model, data, filename, mime, language):
        hint = f" Язык: {language}." if language else ""
        resp = await self.client.aio.models.generate_content(
            model=model,
            contents=[types.Content(role="user", parts=[
                types.Part.from_bytes(data=data, mime_type=mime),
                types.Part.from_text(text="Дословно расшифруй речь из этой записи." + hint +
                                     " Верни только текст расшифровки, без комментариев."),
            ])],
        )
        return self._parse(resp).text.strip()
