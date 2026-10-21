from __future__ import annotations

import asyncio
import html
import io
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, InputMediaPhoto, Message, TelegramObject)
from aiogram.utils.chat_action import ChatActionSender

from .attachments import extract_text, guess_mime, is_image
from .config import load_config
from .keypool import fmt_wait
from .providers import ChatResult
from .render import extract_files, md_to_html, split_markdown
from .service import AIError, AIService, UserLimitExceeded

log = logging.getLogger(__name__)
router = Router()

TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024
MODELS_PER_PAGE = 8
ALBUM_WAIT = 1.2

_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_albums: dict[str, list[Message]] = {}


# ── доступ ────────────────────────────────────────────────────
class AccessMiddleware(BaseMiddleware):
    """Пускает только разрешённых. Незнакомцам предлагает запросить доступ у админов."""
    REQUEST_COOLDOWN = 6 * 3600

    def __init__(self, svc: AIService):
        self.svc = svc
        self._requested: dict[int, float] = {}

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict) -> Any:
        user = getattr(event, "from_user", None)
        if user is None:
            return None
        if self.svc.is_allowed(user.id):
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            await event.answer("Нет доступа", show_alert=True)
            return None
        if not isinstance(event, Message) or self.svc.is_blocked(user.id):
            return None  # заблокированным молчим
        now = time.time()
        if now - self._requested.get(user.id, 0) < self.REQUEST_COOLDOWN:
            await event.answer("⏳ Запрос на доступ уже отправлен администратору, жди ответа.")
            return None
        self._requested[user.id] = now
        await event.answer(f"⛔ Нет доступа. Запрос отправлен администратору.\nТвой ID: <code>{user.id}</code>")
        await notify_access_request(data["bot"], self.svc, user)
        return None


async def notify_access_request(bot: Bot, svc: AIService, user) -> None:
    name = html.escape(user.full_name or str(user.id))
    uname = f" (@{html.escape(user.username)})" if user.username else ""
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Пользователь", callback_data=f"acc:user:{user.id}"),
        InlineKeyboardButton(text="👑 Админ", callback_data=f"acc:admin:{user.id}"),
    ], [InlineKeyboardButton(text="🚫 Отклонить и заблокировать", callback_data=f"acc:blocked:{user.id}")]])
    for admin in svc.admin_ids():
        try:
            await bot.send_message(admin, f"🔔 <b>Запрос доступа</b>\n{name}{uname}\nID: <code>{user.id}</code>",
                                   reply_markup=kb)
        except Exception:  # noqa: BLE001 — админ мог не запускать бота
            log.warning("Не удалось уведомить админа %s", admin)


def is_admin(svc: AIService, user_id: int) -> bool:
    return svc.is_admin(user_id)


# ── отправка ответа ───────────────────────────────────────────
async def send_text(message: Message, text: str) -> None:
    chunks = split_markdown(text)
    if len(chunks) > 8:  # очень длинный ответ — первый кусок + целиком файлом
        await send_chunk(message, chunks[0] + "\n\n…полный ответ в файле ниже.")
        await message.answer_document(BufferedInputFile(text.encode(), "answer.md"))
        return
    for chunk in chunks:
        await send_chunk(message, chunk)


async def send_chunk(message: Message, md: str) -> None:
    try:
        await message.answer(md_to_html(md), disable_web_page_preview=True)
    except TelegramBadRequest as e:
        log.debug("HTML не прошёл (%s), шлю текстом", e)
        await message.answer(md, parse_mode=None, disable_web_page_preview=True)


async def send_result(message: Message, res: ChatResult) -> None:
    text, files = extract_files(res.text or "")
    if text.strip():
        await send_text(message, text)

    imgs = [(d, m) for d, m in res.images]
    small = [(d, m) for d, m in imgs if len(d) < 10 * 1024 * 1024 and m != "image/gif"]
    for d, m in imgs:
        if (d, m) not in small:
            ext = m.split("/")[-1]
            await message.answer_document(BufferedInputFile(d, f"image.{ext}"))
    for i in range(0, len(small), 10):
        batch = small[i:i + 10]
        try:
            if len(batch) == 1:
                d, m = batch[0]
                await message.answer_photo(BufferedInputFile(d, f"image.{m.split('/')[-1]}"))
            else:
                await message.answer_media_group([
                    InputMediaPhoto(media=BufferedInputFile(d, f"image{j}.{m.split('/')[-1]}"))
                    for j, (d, m) in enumerate(batch)])
        except TelegramBadRequest:
            for j, (d, m) in enumerate(batch):
                await message.answer_document(BufferedInputFile(d, f"image{j}.{m.split('/')[-1]}"))

    for name, data in files:
        await message.answer_document(BufferedInputFile(data, name))

    if not text.strip() and not imgs and not files:
        await message.answer("🤷 Модель вернула пустой ответ.")


async def report_error(message: Message, exc: Exception) -> None:
    if isinstance(exc, (AIError, UserLimitExceeded)):
        await message.answer(f"⚠️ {html.escape(str(exc))}")
    else:
        log.exception("Ошибка обработки")
        await message.answer(f"💥 Внутренняя ошибка: {html.escape(type(exc).__name__)}: "
                             f"{html.escape(str(exc)[:500])}")


# ── команды ───────────────────────────────────────────────────
HELP = (
    "🤖 <b>Клиент нейросетей</b>\n\n"
    "Просто пиши сообщения, кидай картинки (можно альбомом), файлы — PDF, DOCX, XLSX, код, логи — "
    "и голосовые 🎤.\n"
    "Нейросеть может присылать в ответ картинки и файлы.\n\n"
    "/model — выбрать провайдера и модель\n"
    "/new — новый диалог (очистить контекст)\n"
    "/img <i>описание</i> — сгенерировать картинку (можно ответом на фото — тогда правка)\n"
    "/system <i>текст</i> — свой системный промпт (<code>/system reset</code> — по умолчанию)\n"
    "/status — текущая модель и расход за сутки\n"
)
ADMIN_HELP = (
    "\n<b>Админ:</b>\n"
    "/users — кто имеет доступ\n"
    "/adduser <i>id [имя]</i> · /addadmin <i>id [имя]</i> · /deluser <i>id</i> · /block <i>id</i>\n"
    "/keys — состояние ключей · /resetkeys — снять паузы · /reload — перечитать конфиг"
)


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message, svc: AIService):
    await message.answer(HELP + (ADMIN_HELP if is_admin(svc, message.from_user.id) else ""))


@router.message(Command("new"))
async def cmd_new(message: Message, svc: AIService):
    await svc.reset(message.from_user.id)
    await message.answer("🧹 Контекст очищен. Начинаем новый диалог.")


@router.message(Command("system"))
async def cmd_system(message: Message, command: CommandObject, svc: AIService):
    uid = message.from_user.id
    arg = (command.args or "").strip()
    if not arg:
        _, _, system = await svc.resolve(uid)
        await message.answer(f"Текущий системный промпт:\n<blockquote>{html.escape(system or '(пусто)')}</blockquote>\n\n"
                             "<code>/system текст</code> — задать, <code>/system reset</code> — по умолчанию, "
                             "<code>/system off</code> — без промпта")
        return
    value = None if arg.lower() == "reset" else "" if arg.lower() == "off" else arg
    await svc.db.set_user(uid, system_prompt=value)
    await message.answer("✅ Системный промпт обновлён.")


@router.message(Command("status"))
async def cmd_status(message: Message, svc: AIService):
    uid = message.from_user.id
    try:
        prov, model, _ = await svc.resolve(uid)
    except AIError as e:
        await message.answer(f"⚠️ {html.escape(str(e))}")
        return
    u = await svc.db.get_usage(svc.pool.today(), "user", str(uid))
    lim = svc.cfg.limits_for(uid, svc.is_admin(uid))
    f = lambda used, cap: f"{used}/{cap}" if cap else f"{used}"
    lines = [f"🧠 <b>{html.escape(prov.title)}</b> · <code>{html.escape(model)}</code>",
             f"🖼 Генерация картинок: <code>{html.escape(prov.image_model or '—')}</code>",
             "", "<b>За сутки:</b>"]
    if lim is None:
        lines.append(f"сообщений {u.requests}, токенов {u.tokens}, картинок {u.images} (без лимитов)")
    else:
        lines.append(f"сообщений {f(u.requests, lim.messages_per_day)}, токенов {f(u.tokens, lim.tokens_per_day)}, "
                     f"картинок {f(u.images, lim.images_per_day)}")
        lines.append(f"Сброс через {fmt_wait(svc.pool.seconds_to_midnight())}")
    await message.answer("\n".join(lines))


# ── выбор модели ──────────────────────────────────────────────
def providers_kb(svc: AIService, current: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for name, p in svc.cfg.providers.items():
        row.append(InlineKeyboardButton(text=("✅ " if name == current else "") + p.title, callback_data=f"p:{name}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def models_kb(name: str, models: list[str], page: int, current: str | None) -> InlineKeyboardMarkup:
    pages = max(1, (len(models) + MODELS_PER_PAGE - 1) // MODELS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    rows = [[InlineKeyboardButton(text=("✅ " if m == current else "") + m[:60], callback_data=f"m:{name}:{i}")]
            for i, m in enumerate(models) if page * MODELS_PER_PAGE <= i < (page + 1) * MODELS_PER_PAGE]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"mp:{name}:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"mp:{name}:{page + 1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton(text="↻ Обновить список", callback_data=f"mr:{name}"),
                 InlineKeyboardButton(text="⬅️ Провайдеры", callback_data="pl")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("model"))
async def cmd_model(message: Message, command: CommandObject, svc: AIService):
    uid = message.from_user.id
    if command.args:  # /model имя-модели — вручную, без списка
        prov, _, _ = await svc.resolve(uid)
        await svc.db.set_user(uid, provider=prov.name, model=command.args.strip())
        await message.answer(f"✅ {html.escape(prov.title)} · <code>{html.escape(command.args.strip())}</code>")
        return
    s = await svc.db.get_user(uid)
    await message.answer("Выбери провайдера:\n<i>(или <code>/model имя</code>, чтобы ввести модель вручную)</i>",
                         reply_markup=providers_kb(svc, s.provider or svc.cfg.default_provider))


@router.callback_query(F.data == "pl")
async def cb_providers(call: CallbackQuery, svc: AIService):
    s = await svc.db.get_user(call.from_user.id)
    await call.message.edit_text("Выбери провайдера:",
                                 reply_markup=providers_kb(svc, s.provider or svc.cfg.default_provider))
    await call.answer()


async def _show_models(call: CallbackQuery, svc: AIService, name: str, page: int, force: bool = False):
    if name not in svc.cfg.providers:
        await call.answer("Провайдер пропал из конфига", show_alert=True)
        return
    await call.answer("Загружаю модели…" if force else None)
    models = await svc.get_models(name, force=force)
    if not models:
        await call.message.edit_text(
            f"Не удалось получить список моделей {html.escape(svc.cfg.providers[name].title)}.\n"
            f"Укажи <code>models</code> или <code>default_model</code> в конфиге, либо введи вручную: "
            f"<code>/model имя</code>", reply_markup=models_kb(name, [], 0, None))
        return
    s = await svc.db.get_user(call.from_user.id)
    current = s.model if s.provider == name else None
    if current in models and page < 0:
        page = models.index(current) // MODELS_PER_PAGE
    page = max(page, 0)
    title = html.escape(svc.cfg.providers[name].title)
    try:
        await call.message.edit_text(f"<b>{title}</b> — выбери модель ({len(models)}):",
                                     reply_markup=models_kb(name, models, page, current))
    except TelegramBadRequest:
        pass  # «message is not modified»


@router.callback_query(F.data.startswith("p:"))
async def cb_provider(call: CallbackQuery, svc: AIService):
    await _show_models(call, svc, call.data[2:], -1)


@router.callback_query(F.data.startswith("mp:"))
async def cb_models_page(call: CallbackQuery, svc: AIService):
    _, name, page = call.data.split(":")
    await _show_models(call, svc, name, int(page))


@router.callback_query(F.data.startswith("mr:"))
async def cb_models_refresh(call: CallbackQuery, svc: AIService):
    await _show_models(call, svc, call.data[3:], -1, force=True)


@router.callback_query(F.data.startswith("m:"))
async def cb_model(call: CallbackQuery, svc: AIService):
    _, name, idx = call.data.split(":")
    models = await svc.get_models(name)
    idx = int(idx)
    if name not in svc.cfg.providers or idx >= len(models):
        await call.answer("Список устарел, открой /model заново", show_alert=True)
        return
    model = models[idx]
    await svc.db.set_user(call.from_user.id, provider=name, model=model)
    title = html.escape(svc.cfg.providers[name].title)
    await call.message.edit_text(f"✅ Выбрано: <b>{title}</b> · <code>{html.escape(model)}</code>\n"
                                 f"Контекст сохранён — /new, чтобы начать заново.")
    await call.answer()


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery):
    await call.answer()


# ── админ ─────────────────────────────────────────────────────
@router.message(Command("keys"))
async def cmd_keys(message: Message, svc: AIService):
    if not is_admin(svc, message.from_user.id):
        return
    out = []
    for prov in svc.cfg.providers.values():
        out.append(f"<b>{html.escape(prov.title)}</b>")
        out += await svc.pool.report(prov)
        out.append("")
    for chunk in split_markdown("\n".join(out)):
        await message.answer(chunk)


@router.message(Command("resetkeys"))
async def cmd_resetkeys(message: Message, command: CommandObject, svc: AIService):
    if not is_admin(svc, message.from_user.id):
        return
    svc.pool.reset_cooldowns(command.args.strip() if command.args else None)
    await message.answer("✅ Паузы ключей сняты.")


@router.message(Command("reload"))
async def cmd_reload(message: Message, svc: AIService, config_path: Path):
    if not is_admin(svc, message.from_user.id):
        return
    try:
        svc.reload(load_config(config_path))
    except Exception as e:  # noqa: BLE001
        await message.answer(f"❌ Конфиг не загружен: {html.escape(str(e))}")
        return
    await message.answer(f"✅ Конфиг перечитан. Провайдеров: {len(svc.cfg.providers)}")


# ── входящие сообщения ────────────────────────────────────────
async def _download(bot: Bot, file_id: str) -> bytes:
    buf = io.BytesIO()
    await bot.download(file_id, destination=buf)
    return buf.getvalue()


async def _transcribe_message(m: Message, bot: Bot, svc: AIService) -> str | None:
    if m.voice:
        f, name, mime = m.voice, "voice.ogg", m.voice.mime_type or "audio/ogg"
    elif m.video_note:
        f, name, mime = m.video_note, "video_note.mp4", "video/mp4"
    else:
        f = m.audio or m.document
        name = f.file_name or "audio.mp3"
        mime = guess_mime(name, f.mime_type)
    if f.file_size and f.file_size > TG_DOWNLOAD_LIMIT:
        await m.answer("⚠️ Запись больше 20 МБ — Telegram не даёт ботам её скачать.")
        return None
    data = await _download(bot, f.file_id)
    async with ChatActionSender.typing(bot=bot, chat_id=m.chat.id):
        text = await svc.transcribe(m.from_user.id, data, name, mime)
    if svc.cfg.transcription.show_text:
        await m.reply(f"🎤 <blockquote expandable>{html.escape(text)}</blockquote>")
    return text


async def collect_parts(messages: list[Message], bot: Bot, svc: AIService) -> tuple[list[dict], list[str]]:
    """Сообщения → части для истории. Возвращает (parts, предупреждения)."""
    uid = messages[0].from_user.id
    parts: list[dict] = []
    warnings: list[str] = []
    texts: list[str] = []
    for m in messages:
        if m.text or m.caption:
            texts.append(m.text or m.caption)
        if m.photo:
            ph = m.photo[-1]
            data = await _download(bot, ph.file_id)
            parts.append({"type": "image", "path": svc.save_file(uid, data, ".jpg"), "mime": "image/jpeg"})
        elif m.voice or m.audio or m.video_note or (m.document and (m.document.mime_type or "").startswith("audio/")):
            text = await _transcribe_message(m, bot, svc)
            if text:
                texts.append(text)
        elif m.document or m.sticker or m.video or m.animation:
            doc = m.document
            if doc is None:
                warnings.append("Этот тип вложений не поддерживается — пришли как файл или фото.")
                continue
            if doc.file_size and doc.file_size > TG_DOWNLOAD_LIMIT:
                warnings.append(f"«{doc.file_name}» больше 20 МБ — Telegram не даёт ботам скачивать такие файлы.")
                continue
            name = doc.file_name or "file"
            mime = guess_mime(name, doc.mime_type)
            data = await _download(bot, doc.file_id)
            ext = Path(name).suffix or ""
            if is_image(mime):
                parts.append({"type": "image", "path": svc.save_file(uid, data, ext), "mime": mime})
                continue
            text = await asyncio.to_thread(extract_text, name, mime, data)
            if text is not None and len(text) > svc.cfg.max_file_chars:
                text = text[:svc.cfg.max_file_chars] + "\n…(обрезано)"
                warnings.append(f"«{name}» слишком большой — в контекст ушло начало файла.")
            if text is None and mime != "application/pdf":
                warnings.append(f"Не смог прочитать «{name}» — формат не поддерживается.")
                continue
            parts.append({"type": "file", "path": svc.save_file(uid, data, ext), "mime": mime,
                          "name": name, "text": text})
    if texts:
        parts.insert(0, {"type": "text", "text": "\n\n".join(texts)})
    return parts, warnings


async def handle_messages(messages: list[Message], bot: Bot, svc: AIService) -> None:
    first = messages[0]
    uid = first.from_user.id
    async with _locks[uid]:
        try:
            parts, warnings = await collect_parts(messages, bot, svc)
            for w in warnings:
                await first.answer(f"⚠️ {html.escape(w)}")
            if not parts:
                return
            if not any(p["type"] == "text" for p in parts):
                parts.insert(0, {"type": "text", "text": "Посмотри вложения."})
            async with ChatActionSender.typing(bot=bot, chat_id=first.chat.id):
                res = await svc.chat(uid, parts)
            await send_result(first, res)
        except Exception as e:  # noqa: BLE001
            await report_error(first, e)


@router.message(Command("img"))
async def cmd_img(message: Message, command: CommandObject, bot: Bot, svc: AIService):
    prompt = (command.args or "").strip()
    if not prompt:
        await message.answer("Напиши описание: <code>/img кот в скафандре, акварель</code>\n"
                             "Ответом на фото (или в подписи к фото) — отредактирует картинку.")
        return
    refs = []
    src = message if message.photo else message.reply_to_message
    if src and src.photo:
        refs.append((await _download(bot, src.photo[-1].file_id), "image/jpeg"))
    elif src and src.document and is_image(guess_mime(src.document.file_name or "", src.document.mime_type)):
        refs.append((await _download(bot, src.document.file_id),
                     guess_mime(src.document.file_name or "", src.document.mime_type)))
    async with _locks[message.from_user.id]:
        try:
            async with ChatActionSender.upload_photo(bot=bot, chat_id=message.chat.id):
                res = await svc.image(message.from_user.id, prompt, refs)
            await send_result(message, res)
        except Exception as e:  # noqa: BLE001
            await report_error(message, e)


@router.message(F.media_group_id)
async def on_album(message: Message, bot: Bot, svc: AIService):
    key = f"{message.chat.id}:{message.media_group_id}"
    if key in _albums:
        _albums[key].append(message)
        return
    _albums[key] = [message]
    await asyncio.sleep(ALBUM_WAIT)  # ждём остальные части альбома
    msgs = sorted(_albums.pop(key), key=lambda m: m.message_id)
    await handle_messages(msgs, bot, svc)


@router.message(F.caption.startswith("/img"))
async def on_photo_img(message: Message, bot: Bot, svc: AIService):
    args = message.caption.split(maxsplit=1)
    await cmd_img(message, CommandObject(prefix="/", command="img", args=args[1] if len(args) > 1 else None),
                  bot, svc)


@router.message()
async def on_message(message: Message, bot: Bot, svc: AIService):
    if message.text and message.text.startswith("/"):
        await message.answer("Неизвестная команда. /help")
        return
    await handle_messages([message], bot, svc)
