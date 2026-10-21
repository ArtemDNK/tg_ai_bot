"""Управление доступом прямо из бота: /users, /adduser, /addadmin, /deluser, /block и заявки."""
from __future__ import annotations

import html

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from .service import AIError, AIService

admin_router = Router()

ROLE_TITLES = {"user": "✅ пользователь", "admin": "👑 админ", "blocked": "🚫 заблокирован"}


def _parse_target(args: str | None) -> tuple[int, str | None] | None:
    if not args:
        return None
    head, *rest = args.split(maxsplit=1)
    if not head.lstrip("-").isdigit():
        return None
    return int(head), (rest[0].strip() if rest else None)


async def _notify(bot: Bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text)
    except Exception:  # noqa: BLE001 — пользователь мог не запускать бота
        pass


async def _apply(svc: AIService, bot: Bot, actor: int, target: int, role: str, name: str | None) -> str:
    if role == "admin" and not svc.is_superadmin(actor):
        raise AIError("Назначать админов могут только администраторы из config.yaml")
    current = svc.access.get(target)
    if current and current.role == "admin" and not svc.is_superadmin(actor):
        raise AIError("Менять права другого админа может только администратор из config.yaml")
    await svc.set_role(target, role, name, actor)
    if role == "user":
        await _notify(bot, target, "✅ Доступ к боту открыт. /help — что я умею.")
    elif role == "admin":
        await _notify(bot, target, "👑 Тебе выданы права администратора бота. /help")
    label = html.escape(name or (svc.access[target].name or "")) if svc.access.get(target) else ""
    return f"{ROLE_TITLES[role]}: <code>{target}</code> {label}".strip()


@admin_router.message(Command("users"))
async def cmd_users(message: Message, svc: AIService):
    if not svc.is_admin(message.from_user.id):
        return
    lines = ["<b>Админы из config.yaml:</b>"]
    lines += [f"• <code>{uid}</code>" for uid in sorted(svc.cfg.admins)]
    if svc.cfg.allowed_users:
        lines += ["", "<b>allowed_users из config.yaml:</b>"]
        lines += [f"• <code>{uid}</code>" for uid in sorted(svc.cfg.allowed_users)]
    groups = {"admin": [], "user": [], "blocked": []}
    for uid, e in svc.access.items():
        groups.setdefault(e.role, []).append(f"• <code>{uid}</code> {html.escape(e.name or '')}".rstrip())
    for role, title in (("admin", "Админы"), ("user", "Пользователи"), ("blocked", "Заблокированы")):
        if groups[role]:
            lines += ["", f"<b>{title}:</b>", *groups[role]]
    if svc.cfg.allow_all:
        lines += ["", "⚠️ В конфиге allow_all: true — бот открыт всем, кроме заблокированных."]
    lines += ["", "/adduser <i>id [имя]</i> · /addadmin <i>id [имя]</i> · /deluser <i>id</i> · /block <i>id</i>"]
    await message.answer("\n".join(lines))


async def _role_command(message: Message, command: CommandObject, svc: AIService, bot: Bot, role: str):
    if not svc.is_admin(message.from_user.id):
        return
    target = _parse_target(command.args)
    if target is None:
        await message.answer(f"Формат: <code>/{command.command} 123456789 Имя</code>\n"
                             "ID человек узнает, написав боту — или придёт заявка с кнопками.")
        return
    try:
        text = await _apply(svc, bot, message.from_user.id, target[0], role, target[1])
    except AIError as e:
        await message.answer(f"⚠️ {html.escape(str(e))}")
        return
    await message.answer(text)


@admin_router.message(Command("adduser"))
async def cmd_adduser(message: Message, command: CommandObject, svc: AIService, bot: Bot):
    await _role_command(message, command, svc, bot, "user")


@admin_router.message(Command("addadmin"))
async def cmd_addadmin(message: Message, command: CommandObject, svc: AIService, bot: Bot):
    await _role_command(message, command, svc, bot, "admin")


@admin_router.message(Command("block"))
async def cmd_block(message: Message, command: CommandObject, svc: AIService, bot: Bot):
    await _role_command(message, command, svc, bot, "blocked")


@admin_router.message(Command("deluser"))
async def cmd_deluser(message: Message, command: CommandObject, svc: AIService):
    actor = message.from_user.id
    if not svc.is_admin(actor):
        return
    target = _parse_target(command.args)
    if target is None:
        await message.answer("Формат: <code>/deluser 123456789</code>")
        return
    uid = target[0]
    e = svc.access.get(uid)
    try:
        if e and e.role == "admin" and not svc.is_superadmin(actor):
            raise AIError("Снимать админов может только администратор из config.yaml")
        removed = await svc.revoke(uid)
    except AIError as err:
        await message.answer(f"⚠️ {html.escape(str(err))}")
        return
    note = ""
    if uid in svc.cfg.allowed_users or svc.cfg.allow_all:
        note = "\n⚠️ Доступ всё ещё есть через config.yaml (allowed_users / allow_all) — используй /block."
    await message.answer((f"🗑 Доступ <code>{uid}</code> отозван." if removed else
                          f"У <code>{uid}</code> не было доступа, выданного через бота.") + note)


@admin_router.callback_query(F.data.startswith("acc:"))
async def cb_access(call: CallbackQuery, svc: AIService, bot: Bot):
    if not svc.is_admin(call.from_user.id):
        await call.answer("Только для админов", show_alert=True)
        return
    _, role, uid = call.data.split(":")
    # имя берём из текста заявки: вторая строка «Имя (@username)»
    lines = (call.message.text or "").split("\n")
    name = lines[1] if len(lines) > 1 else None
    try:
        text = await _apply(svc, bot, call.from_user.id, int(uid), role, name)
    except AIError as e:
        await call.answer(str(e), show_alert=True)
        return
    by = html.escape(call.from_user.full_name)
    await call.message.edit_text(f"{call.message.html_text}\n\n{text} — решил(а) {by}")
    await call.answer()
