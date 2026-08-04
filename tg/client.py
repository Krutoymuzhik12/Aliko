"""Обёртка Telethon: личный аккаунт (+79805247203), отдельная сессия.

Первый запуск интерактивный: Telegram пришлёт код подтверждения, ввести в
консоли один раз — дальше работает session-файл (data/karina.session).
"""

import asyncio
import logging
import random

from telethon import TelegramClient

from config.settings import AppSettings

logger = logging.getLogger(__name__)


class ManagerClient:
    def __init__(self, settings: AppSettings):
        if not settings.tg_ready():
            raise RuntimeError("Задай TG_API_ID и TG_API_HASH в .env (my.telegram.org)")
        self.settings = settings
        self.client = TelegramClient(
            settings.tg_session, settings.tg_api_id, settings.tg_api_hash
        )
        self.me_id: int = 0

    async def start(self) -> None:
        await self.client.start(phone=self.settings.tg_phone or None)
        me = await self.client.get_me()
        self.me_id = me.id
        logger.info("Telethon: вошли как %s (@%s id=%s)", me.first_name, me.username, me.id)

    SELF_TEST_MARKER = "[BOT] "

    async def send(self, user_id: int, text: str) -> int:
        """В SELF_TEST всё уходит в Избранное с текстовой меткой — реальный
        собеседник ничего не получает, метка отличает ответы бота от
        «клиентских» сообщений в той же переписке с самим собой."""
        target = "me" if self.settings.self_test else user_id
        out_text = f"{self.SELF_TEST_MARKER}{text}" if self.settings.self_test else text
        try:
            async with self.client.action(target, "typing"):
                await asyncio.sleep(random.uniform(1.0, 2.0))
        except Exception:
            pass  # action не критичен
        msg = await self.client.send_message(target, out_text)
        return msg.id

    async def mark_read(self, user_id: int) -> None:
        try:
            await self.client.send_read_acknowledge(user_id)
        except Exception:
            logger.warning("Не удалось отметить прочитанным user=%s", user_id)

    async def notify_manager(self, text: str) -> None:
        target = self.settings.manager_chat or "me"
        await self.client.send_message(target, text)

    async def notify_leads_group(self, text: str) -> None:
        if not self.settings.leads_group_chat_id:
            return
        await self.client.send_message(self.settings.leads_group_chat_id, text)

    async def run_until_disconnected(self) -> None:
        await self.client.run_until_disconnected()
