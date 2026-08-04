import re

from config.settings import SETTINGS

_PHONE_RE = re.compile(r"(?<!\d)(?:(?:\+7|8|7)[\s\-\(\)]*)?9[\s\-\(\)]*(?:\d[\s\-\(\)]*){8}\d(?!\d)")


def extract_phone(text: str) -> str | None:
    """Российский номер телефона в свободном тексте (с пробелами/скобками/
    дефисами или без) -> нормализованный +7XXXXXXXXXX, либо None."""
    match = _PHONE_RE.search(text or "")
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(0))
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    return None


def _handle(username: str | None, user_id: int) -> str:
    return f"@{username}" if username else f"id{user_id} (без username)"


def _transcript(history: list) -> str:
    bot_name = SETTINGS.manager_name or "Бот"
    return "\n".join(
        f"{'Клиент' if row['role'] == 'user' else bot_name}: {row['text']}" for row in history
    )


def build_lead_report(username: str | None, user_id: int, phone: str, history: list) -> str:
    return (
        f"🔔 Новая заявка (Алико потолки)\n"
        f"Telegram: {_handle(username, user_id)}\n"
        f"Телефон: {phone}\n\n"
        f"Конспект переписки:\n{_transcript(history)}"
    )


def build_off_topic_report(username: str | None, user_id: int, history: list) -> str:
    """Написал не клиент — бот отключился в этом чате навсегда, дальше
    отвечать (или не отвечать) человеку нужно вручную."""
    return (
        f"⚠️ Написали не по потолкам (Алико потолки)\n"
        f"Telegram: {_handle(username, user_id)}\n"
        f"Бот отключён в этом чате, дальше отвечайте вручную.\n\n"
        f"Переписка:\n{_transcript(history)}"
    )
