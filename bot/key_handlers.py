"""Управление API-ключами из бота: /keys, /addkey, кнопки добавления, удаления и лимитов.

Смотреть состояние ключей может любой админ, менять — только главные админы из config.yaml.
Сообщения с ключами бот сразу удаляет из чата.
"""
from __future__ import annotations

import html
import re
import time

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .errors import ErrorKind
from .service import AIService

key_router = Router()

STATE_TTL = 300  # через 5 минут ожидание ключа сбрасывается само
_KEY_RX = re.compile(r"^[\x21-\x7e]{8,}$")   # ключ — одна «печатная» строка без пробелов
_PARAM_RX = re.compile(r"^(name|rpm|rpd|tpd)=(.+)$", re.I)


class KeyStates(StatesGroup):
    adding = State()
    limits = State()


def can_view(svc: AIService, uid: int) -> bool:
    return svc.is_admin(uid)


def can_edit(svc: AIService, uid: int) -> bool:
    return svc.is_superadmin(uid)


# ── разбор ввода ──────────────────────────────────────────────
def parse_key_line(line: str) -> tuple[str, dict] | None:
    """`KEY [name=...] [rpm=N] [rpd=N] [tpd=N]` → (key, params) или None."""
    tokens = line.split()
    if not tokens or not _KEY_RX.match(tokens[0]):
        return None
    params: dict = {}
    for t in tokens[1:]:
        m = _PARAM_RX.match(t)
        if not m:
            return None
        k, v = m.group(1).lower(), m.group(2)
        if k == "name":
            params[k] = v
        elif v.isdigit():
            params[k] = int(v)
        else:
            return None
    return tokens[0], params


def parse_limits(text: str) -> tuple[int, int, int] | None:
    """`10 250 0` или `rpm=10 rpd=250` → (rpm, rpd, tpd)."""
    text = text.strip()
    if re.fullmatch(r"\d+(\s+\d+){0,2}", text):
        nums = [int(x) for x in text.split()] + [0, 0]
        return nums[0], nums[1], nums[2]
    vals = {"rpm": 0, "rpd": 0, "tpd": 0}
    for t in text.split():
        m = re.fullmatch(r"(rpm|rpd|tpd)=(\d+)", t, re.I)
        if not m:
            return None
        vals[m.group(1).lower()] = int(m.group(2))
    return vals["rpm"], vals["rpd"], vals["tpd"]


# ── экраны ────────────────────────────────────────────────────
async def overview(svc: AIService, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["🔑 <b>API-ключи</b>", ""]
    buttons = []
    for name, prov in svc.cfg.providers.items():
        total, blocked = await svc.pool.summary(prov)
        if total:
            state = f"{total} шт." + (f", на паузе {blocked}" if blocked else "")
            lines.append(f"{'🟢' if blocked < total else '🔴'} {html.escape(prov.title)} — {state}")
        else:
            lines.append(f"▫️ {html.escape(prov.title)} — нет ключей")
        buttons.append(InlineKeyboardButton(text=f"{prov.title} ({total})", callback_data=f"kp:{name}"))
    if can_edit(svc, uid):
        lines += ["", "Выбери провайдера, чтобы добавить или удалить ключи и задать лимиты."]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="kv"),
                 InlineKeyboardButton(text="⏯ Снять все паузы", callback_data="kr:*")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def provider_view(svc: AIService, name: str, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    prov = svc.cfg.providers[name]
    lines = [f"🔑 <b>{html.escape(prov.title)}</b>"]
    if prov.base_url:
        lines.append(f"<code>{html.escape(prov.base_url)}</code>")
    lines.append("")
    report = await svc.pool.report(prov)
    lines += report or ["Ключей нет."]
    rows = []
    if can_edit(svc, uid):
        for i, k in enumerate(prov.keys, 1):
            if k.db_id is None:
                continue  # ключи из config.yaml меняются только в конфиге
            rows.append([InlineKeyboardButton(text=f"⚙️ Лимиты {i}", callback_data=f"kl:{name}:{k.db_id}"),
                         InlineKeyboardButton(text=f"🗑 Удалить {i}", callback_data=f"kd:{name}:{k.db_id}")])
        rows.append([InlineKeyboardButton(text="➕ Добавить ключ", callback_data=f"ka:{name}")])
    rows.append([InlineKeyboardButton(text="⏯ Снять паузы", callback_data=f"kr:{name}"),
                 InlineKeyboardButton(text="⬅️ Назад", callback_data="kv")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _edit(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await call.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass  # «message is not modified»


# ── команды ───────────────────────────────────────────────────
@key_router.message(Command("keys"))
async def cmd_keys(message: Message, svc: AIService, state: FSMContext):
    if not can_view(svc, message.from_user.id):
        return
    await state.clear()
    text, kb = await overview(svc, message.from_user.id)
    await message.answer(text, reply_markup=kb)


@key_router.message(Command("addkey"))
async def cmd_addkey(message: Message, command: CommandObject, svc: AIService):
    uid = message.from_user.id
    if not can_edit(svc, uid):
        return
    await _safe_delete(message)  # в сообщении ключ — убираем из чата сразу
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) < 2 or parts[0] not in svc.cfg.providers:
        names = ", ".join(f"<code>{n}</code>" for n in svc.cfg.providers)
        await message.answer("Формат: <code>/addkey провайдер КЛЮЧ [name=имя] [rpm=10] [rpd=250] [tpd=0]</code>\n"
                             f"Провайдеры: {names}\nУдобнее через кнопки: /keys")
        return
    await message.answer(await add_keys(svc, parts[0], parts[1], uid))


@key_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        raise SkipHandler
    await state.clear()
    await message.answer("Отменено.")


# ── добавление ────────────────────────────────────────────────
async def _safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except TelegramBadRequest:
        pass


async def add_keys(svc: AIService, provider: str, text: str, uid: int) -> str:
    """Добавляет ключи (по одному на строку) с проверкой. Возвращает отчёт."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    parsed = [parse_key_line(ln) for ln in lines]
    if not lines or any(p is None for p in parsed):
        bad = next((ln for ln, p in zip(lines, parsed) if p is None), "")
        return (f"❌ Не похоже на ключ: <code>{html.escape(bad[:6])}…</code>\n"
                "Формат строки: <code>КЛЮЧ [name=имя] [rpm=10] [rpd=250] [tpd=0]</code>")
    prov = svc.cfg.providers[provider]
    out = [f"<b>{html.escape(prov.title)}</b>:"]
    for key, p in parsed:
        masked = f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "***"
        if any(k.key == key for k in prov.keys):
            out.append(f"• <code>{masked}</code> — уже есть")
            continue
        err = await svc.test_key(provider, key)
        if err and err[0] == ErrorKind.AUTH:  # заведомо нерабочий ключ в ротацию не берём
            out.append(f"• <code>{masked}</code> — ❌ API отклонил ключ: {html.escape(err[1][:200])}")
            continue
        added = await svc.add_key(provider, key, p.get("name"), p.get("rpm", 0), p.get("rpd", 0),
                                  p.get("tpd", 0), uid)
        if not added:
            out.append(f"• <code>{masked}</code> — уже есть")
        elif err:
            out.append(f"• <code>{masked}</code> — ⚠️ добавлен, но проверить не удалось: "
                       f"{html.escape(err[1][:200])}")
        else:
            out.append(f"• <code>{masked}</code> — ✅ добавлен, ключ рабочий")
    out.append("\nСообщение с ключом удалено из чата. /keys — список.")
    return "\n".join(out)


@key_router.callback_query(F.data.startswith("ka:"))
async def cb_add(call: CallbackQuery, svc: AIService, state: FSMContext):
    if not can_edit(svc, call.from_user.id):
        await call.answer("Ключи меняют только главные админы", show_alert=True)
        return
    name = call.data[3:]
    await state.set_state(KeyStates.adding)
    await state.update_data(provider=name, since=time.time())
    await call.message.answer(
        f"Пришли ключ для <b>{html.escape(svc.cfg.providers[name].title)}</b>.\n"
        "Можно несколько — по одному на строку. Лимиты по желанию:\n"
        "<code>КЛЮЧ name=основной rpm=10 rpd=250 tpd=0</code>\n\n"
        "Сообщение с ключом я сразу удалю. /cancel — отмена.")
    await call.answer()


@key_router.message(StateFilter(KeyStates.adding), F.text, ~F.text.startswith("/"))
async def on_key_input(message: Message, svc: AIService, state: FSMContext):
    data = await state.get_data()
    if time.time() - data.get("since", 0) > STATE_TTL or not can_edit(svc, message.from_user.id):
        await state.clear()
        raise SkipHandler  # ожидание истекло — это обычное сообщение нейросети
    await _safe_delete(message)
    report = await add_keys(svc, data["provider"], message.text, message.from_user.id)
    if report.startswith("❌"):
        await message.answer(report + "\nПопробуй ещё раз или /cancel.")
        return
    await state.clear()
    text, kb = await provider_view(svc, data["provider"], message.from_user.id)
    await message.answer(report)
    await message.answer(text, reply_markup=kb)


# ── лимиты ────────────────────────────────────────────────────
@key_router.callback_query(F.data.startswith("kl:"))
async def cb_limits(call: CallbackQuery, svc: AIService, state: FSMContext):
    if not can_edit(svc, call.from_user.id):
        await call.answer("Ключи меняют только главные админы", show_alert=True)
        return
    _, name, key_id = call.data.split(":")
    key = next((k for k in svc.cfg.providers[name].keys if k.db_id == int(key_id)), None)
    if key is None:
        await call.answer("Ключ уже удалён", show_alert=True)
        return
    await state.set_state(KeyStates.limits)
    await state.update_data(provider=name, key_id=int(key_id), since=time.time())
    await call.message.answer(
        f"Лимиты ключа <b>{html.escape(key.name)}</b> <code>{html.escape(key.masked)}</code>\n"
        f"Сейчас: rpm={key.rpm} rpd={key.rpd} tpd={key.tpd}\n\n"
        "Пришли три числа: <b>запросов в минуту, запросов в сутки, токенов в сутки</b> (0 — без лимита).\n"
        "Например <code>10 250 0</code> или <code>rpm=10 rpd=250</code>. /cancel — отмена.")
    await call.answer()


@key_router.message(StateFilter(KeyStates.limits), F.text, ~F.text.startswith("/"))
async def on_limits_input(message: Message, svc: AIService, state: FSMContext):
    data = await state.get_data()
    if time.time() - data.get("since", 0) > STATE_TTL or not can_edit(svc, message.from_user.id):
        await state.clear()
        raise SkipHandler
    lim = parse_limits(message.text)
    if lim is None:
        await message.answer("Не понял. Пример: <code>10 250 0</code>. /cancel — отмена.")
        return
    await svc.set_key_limits(data["key_id"], *lim)
    await state.clear()
    await message.answer(f"✅ Лимиты: rpm={lim[0]} rpd={lim[1]} tpd={lim[2]}")
    text, kb = await provider_view(svc, data["provider"], message.from_user.id)
    await message.answer(text, reply_markup=kb)


# ── кнопки просмотра / удаления / пауз ───────────────────────
@key_router.callback_query(F.data == "kv")
async def cb_overview(call: CallbackQuery, svc: AIService):
    if not can_view(svc, call.from_user.id):
        return await call.answer()
    await _edit(call, *await overview(svc, call.from_user.id))
    await call.answer()


@key_router.callback_query(F.data.startswith("kp:"))
async def cb_provider(call: CallbackQuery, svc: AIService):
    name = call.data[3:]
    if not can_view(svc, call.from_user.id) or name not in svc.cfg.providers:
        return await call.answer()
    await _edit(call, *await provider_view(svc, name, call.from_user.id))
    await call.answer()


@key_router.callback_query(F.data.startswith("kd:"))
async def cb_delete(call: CallbackQuery, svc: AIService):
    if not can_edit(svc, call.from_user.id):
        await call.answer("Ключи меняют только главные админы", show_alert=True)
        return
    _, name, key_id = call.data.split(":")
    ok = await svc.delete_key(int(key_id))
    await call.answer("🗑 Ключ удалён" if ok else "Ключ уже удалён")
    await _edit(call, *await provider_view(svc, name, call.from_user.id))


@key_router.callback_query(F.data.startswith("kr:"))
async def cb_reset(call: CallbackQuery, svc: AIService):
    if not can_view(svc, call.from_user.id):
        return await call.answer()
    name = call.data[3:]
    svc.pool.reset_cooldowns(None if name == "*" else name)
    await call.answer("Паузы сняты")
    if name == "*":
        await _edit(call, *await overview(svc, call.from_user.id))
    else:
        await _edit(call, *await provider_view(svc, name, call.from_user.id))

