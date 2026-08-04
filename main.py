"""Точка входа: python main.py

Первый запуск: Telethon спросит код подтверждения Telegram в консоли
(номер +79805247203) — дальше работает файл сессии data/karina.session.
"""

import asyncio
import logging
import sys

from config.settings import DATA_DIR, SETTINGS
from db.database import Database
from services.poe_client import PoeClient
from tg.client import ManagerClient
from tg.worker import ReactiveWorker

LOG_FILE = DATA_DIR / "bot.log"
logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task] = set()


async def run() -> None:
    if not SETTINGS.poe_ready():
        logger.warning("POE_API_KEY/POE_CHAT_MODEL не заданы в .env — бот не сможет отвечать")

    tg = ManagerClient(SETTINGS)
    await tg.start()

    db = Database()
    poe = PoeClient()

    worker = ReactiveWorker(SETTINGS, tg, db, poe)
    worker.register()
    # Ссылку держим явно: event loop хранит только слабые ссылки на задачи,
    # без этого фоновый дожим может быть собран сборщиком мусора.
    followup_task = asyncio.create_task(worker.run_followup_loop())
    _background_tasks.add(followup_task)
    followup_task.add_done_callback(_background_tasks.discard)
    logger.info(
        "Реактивный контур запущен. POE_CHAT_MODEL=%s, дожим через %ss/%ss",
        SETTINGS.poe_chat_model,
        SETTINGS.followup_1_delay_sec,
        SETTINGS.followup_2_delay_sec,
    )

    await tg.run_until_disconnected()


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ],
    )
    logger.info("Логи: %s", LOG_FILE)
    asyncio.run(run())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
