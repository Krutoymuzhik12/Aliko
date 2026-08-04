"""Решает, можно ли боту вести диалог с этим юзером.

Правило: бот НИКОГДА не пишет тем, с кем в этом Telegram-аккаунте уже была
переписка. «Уже была» проверяется не разовым снимком при старте (он не
переживёт перезапуск: юзер, которому бот ответил вчера, сегодня уже имеет
историю и ошибочно попал бы в «старые»), а по истории САМОГО чата в момент
первого сообщения от юзера: если до этого входящего сообщения в чате нет
вообще ничего (ни входящих, ни исходящих) — это подлинно первый контакт,
и дальше бот ведёт этот диалог как «новый» без повторных проверок.
Если история есть — юзер помечается 'existing' навсегда, бот в этом чате
молчит.
"""

import logging

from telethon import TelegramClient

from db.database import Database

logger = logging.getLogger(__name__)


async def classify(
    client: TelegramClient,
    db: Database,
    user_id: int,
    username: str | None,
    incoming_message_id: int,
) -> str:
    row = db.get_chat(user_id)
    if row:
        return row["status"]

    prior = await client.get_messages(user_id, limit=1, offset_id=incoming_message_id)
    status = "existing" if prior else "new"
    db.set_chat_status(user_id, username, status)
    if status == "existing":
        logger.info(
            "user=%s уже есть переписка в Telegram (не бот заводил) — бот молчит навсегда",
            user_id,
        )
    else:
        logger.info("user=%s первый контакт — бот берёт диалог", user_id)
    return status
