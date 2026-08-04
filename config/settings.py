import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=True)

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _bool_env(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class AppSettings:
    tg_api_id: int
    tg_api_hash: str
    tg_phone: str
    tg_session: str
    manager_chat: str

    poe_api_key: str
    poe_classifier_model: str  # вынесенный бот на Poe — классификация (данные, без логики в коде)
    poe_chat_model: str  # вынесенный бот на Poe — генерация ответа

    company_name: str
    manager_name: str

    message_batch_wait_sec: float
    message_batch_settle_sec: float
    message_batch_tail_wait_sec: float
    message_batch_max_wait_sec: float
    typing_idle_sec: float

    self_test: bool  # тестовый режим: диалог целиком в Избранном ("me")

    leads_group_chat_id: int | None  # группа, куда падают заявки с номером

    followup_1_delay_sec: float  # молчание клиента -> первый дожим
    followup_2_delay_sec: float  # молчание клиента -> второй (последний) дожим
    followup_poll_sec: float  # как часто проверять кандидатов на дожим

    db_path: Path

    @classmethod
    def load(cls) -> "AppSettings":
        return cls(
            tg_api_id=int(os.getenv("TG_API_ID", "0") or 0),
            tg_api_hash=os.getenv("TG_API_HASH", "").strip(),
            tg_phone=os.getenv("TG_PHONE", "").strip(),
            tg_session=os.getenv("TG_SESSION", str(DATA_DIR / "karina")).strip(),
            manager_chat=os.getenv("MANAGER_CHAT", "me").strip(),
            poe_api_key=os.getenv("POE_API_KEY", "").strip(),
            poe_classifier_model=os.getenv("POE_CLASSIFIER_MODEL", "").strip(),
            poe_chat_model=os.getenv("POE_CHAT_MODEL", "Claude-Sonnet-4.5").strip(),
            company_name=os.getenv("COMPANY_NAME", "").strip(),
            manager_name=os.getenv("MANAGER_NAME", "Карина").strip(),
            message_batch_wait_sec=float(os.getenv("MESSAGE_BATCH_WAIT_SEC", "7")),
            message_batch_settle_sec=float(os.getenv("MESSAGE_BATCH_SETTLE_SEC", "1.5")),
            message_batch_tail_wait_sec=float(os.getenv("MESSAGE_BATCH_TAIL_WAIT_SEC", "3")),
            message_batch_max_wait_sec=float(os.getenv("MESSAGE_BATCH_MAX_WAIT_SEC", "20")),
            typing_idle_sec=float(os.getenv("TYPING_IDLE_SEC", "3")),
            self_test=_bool_env("SELF_TEST"),
            leads_group_chat_id=(
                int(os.getenv("LEADS_GROUP_CHAT_ID"))
                if os.getenv("LEADS_GROUP_CHAT_ID", "").strip()
                else None
            ),
            followup_1_delay_sec=float(os.getenv("FOLLOWUP_1_DELAY_SEC", "7200")),
            followup_2_delay_sec=float(os.getenv("FOLLOWUP_2_DELAY_SEC", "86400")),
            followup_poll_sec=float(os.getenv("FOLLOWUP_POLL_SEC", "300")),
            db_path=DATA_DIR / "karina.db",
        )

    def tg_ready(self) -> bool:
        return bool(self.tg_api_id and self.tg_api_hash)

    def poe_ready(self) -> bool:
        return bool(self.poe_api_key and self.poe_chat_model)


SETTINGS = AppSettings.load()
