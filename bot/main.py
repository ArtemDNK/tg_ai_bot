from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from .config import load_config
from .db import Database
from .access_handlers import admin_router
from .handlers import AccessMiddleware, router
from .service import AIService

BASE = Path(__file__).resolve().parent.parent


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config_path = Path(sys.argv[1] if len(sys.argv) > 1 else os.getenv("BOT_CONFIG", BASE / "config.yaml"))
    cfg = load_config(config_path)

    data_dir = Path(os.getenv("BOT_DATA", BASE / "data"))
    (data_dir / "files").mkdir(parents=True, exist_ok=True)
    db = Database(str(data_dir / "bot.sqlite3"))
    await db.open()
    svc = AIService(cfg, db, data_dir / "files")
    await svc.load_access()

    session = AiohttpSession(proxy=cfg.tg_proxy) if cfg.tg_proxy else None
    bot = Bot(cfg.tg_token, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    async def notify_admins(text: str) -> None:
        for admin in svc.admin_ids():
            try:
                await bot.send_message(admin, text, parse_mode=None)
            except Exception:  # noqa: BLE001
                pass

    svc.on_key_auth_error = notify_admins

    dp = Dispatcher(svc=svc, config_path=config_path)
    dp.message.outer_middleware(AccessMiddleware(svc))
    dp.callback_query.outer_middleware(AccessMiddleware(svc))
    dp.include_router(admin_router)
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="model", description="Выбрать нейросеть и модель"),
        BotCommand(command="new", description="Новый диалог"),
        BotCommand(command="img", description="Сгенерировать картинку"),
        BotCommand(command="system", description="Системный промпт"),
        BotCommand(command="status", description="Модель и расход"),
        BotCommand(command="help", description="Справка"),
    ])
    logging.info("Бот запущен, провайдеры: %s", ", ".join(cfg.providers))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await svc.factory.close()
        await db.close()
        await bot.session.close()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
