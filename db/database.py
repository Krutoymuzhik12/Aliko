import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from config.settings import SETTINGS


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    """chats: статус 'new' (бот ведёт диалог) / 'existing' (переписка была
    до бота, ИЛИ владелец аккаунта сам написал в чат — бот молчит навсегда).
    messages: полная история диалога, целиком уходит в Poe как контекст на
    каждый ответ."""

    def __init__(self, path=None, account_id: int = 0):
        self.path = path or SETTINGS.db_path
        # id Telegram-аккаунта, под которым работает бот. Пишется в каждую
        # запись чата: user_id в Telegram глобальны, но решение «новый ли это
        # чат» принимается с точки зрения КОНКРЕТНОГО аккаунта. После смены
        # аккаунта старые записи недействительны.
        self.account_id = account_id
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    status TEXT NOT NULL CHECK (status IN ('new', 'existing')),
                    lead_notified_at TEXT,
                    followup_stage INTEGER NOT NULL DEFAULT 0,
                    followup_last_sent_at TEXT,
                    is_self_test INTEGER NOT NULL DEFAULT 0,
                    account_id INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'bot')),
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id);
                """
            )
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(chats)")}
            if "lead_notified_at" not in cols:
                conn.execute("ALTER TABLE chats ADD COLUMN lead_notified_at TEXT")
            if "followup_stage" not in cols:
                conn.execute(
                    "ALTER TABLE chats ADD COLUMN followup_stage INTEGER NOT NULL DEFAULT 0"
                )
            if "followup_last_sent_at" not in cols:
                conn.execute("ALTER TABLE chats ADD COLUMN followup_last_sent_at TEXT")
            if "is_self_test" not in cols:
                conn.execute(
                    "ALTER TABLE chats ADD COLUMN is_self_test INTEGER NOT NULL DEFAULT 0"
                )
            if "account_id" not in cols:
                conn.execute(
                    "ALTER TABLE chats ADD COLUMN account_id INTEGER NOT NULL DEFAULT 0"
                )

    def get_chat(self, user_id: int) -> sqlite3.Row | None:
        with self._conn() as conn:
            return conn.execute(
                "SELECT * FROM chats WHERE user_id = ?", (user_id,)
            ).fetchone()

    def set_chat_status(self, user_id: int, username: str | None, status: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO chats (user_id, username, status, account_id, created_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET username = excluded.username""",
                (user_id, username, status, self.account_id, _utc_now()),
            )

    def force_existing(self, user_id: int) -> None:
        """Владелец аккаунта сам написал в этот чат — бот молчит тут навсегда."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE chats SET status = 'existing' WHERE user_id = ?", (user_id,)
            )

    def mark_existing(self, user_id: int, username: str | None) -> None:
        """Пометить чат существующим независимо от того, есть ли уже запись.
        Нужен для знакомых из контактов: set_chat_status при конфликте
        обновляет только username и статус 'new' бы не перебил, а
        force_existing ничего не сделает, если строки ещё нет."""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO chats (user_id, username, status, account_id, created_at)
                   VALUES (?, ?, 'existing', ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET status = 'existing',
                                                      username = excluded.username""",
                (user_id, username, self.account_id, _utc_now()),
            )

    def force_new(self, user_id: int) -> None:
        """SELF_TEST: гарантированно 'new', даже если раньше стало 'existing'
        (например, тестировали сценарий ручного вмешательства на себе же).

        Запись помечается is_self_test=1: в боевом режиме такие строки
        игнорируются полностью. Иначе тестовый диалог переживал бы и
        переключение SELF_TEST=0, и смену аккаунта, а дожим потом писал бы
        реальному человеку с чужого уже аккаунта."""
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO chats (user_id, username, status, is_self_test, account_id, created_at)
                   VALUES (?, NULL, 'new', 1, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET status = 'new', is_self_test = 1,
                                                      account_id = excluded.account_id""",
                (user_id, self.account_id, _utc_now()),
            )

    def is_lead_notified(self, user_id: int) -> bool:
        row = self.get_chat(user_id)
        return bool(row and row["lead_notified_at"])

    def mark_lead_notified(self, user_id: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE chats SET lead_notified_at = ? WHERE user_id = ?",
                (_utc_now(), user_id),
            )

    def set_followup_stage(self, user_id: int, stage: int) -> None:
        """Сброс на 0 при реальной активности клиента — followup_last_sent_at
        не трогаем, следующий цикл дожима опять пойдёт от last_user_msg_at."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE chats SET followup_stage = ? WHERE user_id = ?", (stage, user_id)
            )

    def record_followup_sent(self, user_id: int, stage: int) -> None:
        """Дожим stage реально отправлен — второй дожим будет отсчитываться
        от ЭТОГО момента, а не от исходного молчания клиента."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE chats SET followup_stage = ?, followup_last_sent_at = ? WHERE user_id = ?",
                (stage, _utc_now(), user_id),
            )

    def candidates_for_followup(self, self_test: bool = False) -> list[sqlite3.Row]:
        """Чаты 'new' без заявки, для которых ещё не исчерпаны попытки дожима
        (followup_stage < 2), с временем последнего сообщения КЛИЕНТА и
        временем последнего отправленного дожима (для цепочки 2ч -> сутки).

        Обязательное условие: в чате уже есть И сообщение клиента, И ответ
        бота. Дожим — это возврат в НАЧАТЫЙ диалог, поэтому бот никогда не
        напишет первым в чат, где он сам ещё не говорил."""
        with self._conn() as conn:
            return conn.execute(
                """SELECT user_id, followup_stage, followup_last_sent_at,
                          (SELECT MAX(created_at) FROM messages
                           WHERE user_id = chats.user_id AND role = 'user') AS last_user_msg_at
                   FROM chats
                   WHERE status = 'new' AND lead_notified_at IS NULL AND followup_stage < 2
                     AND is_self_test = ? AND account_id = ?
                     AND EXISTS (SELECT 1 FROM messages
                                 WHERE user_id = chats.user_id AND role = 'user')
                     AND EXISTS (SELECT 1 FROM messages
                                 WHERE user_id = chats.user_id AND role = 'bot')""",
                (1 if self_test else 0, self.account_id),
            ).fetchall()

    def startup_summary(self, account_id: int) -> dict:
        """Сводка для лога при запуске: видно сразу, если в базе остались
        тестовые чаты или чаты от прежнего аккаунта."""
        with self._conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN status='new' AND is_self_test=0
                                        AND account_id=? THEN 1 ELSE 0 END) AS active_here,
                          SUM(CASE WHEN is_self_test=1 THEN 1 ELSE 0 END) AS self_test,
                          SUM(CASE WHEN account_id NOT IN (0, ?) THEN 1 ELSE 0 END)
                              AS foreign_account
                   FROM chats""",
                (account_id, account_id),
            ).fetchone()
            return {k: (row[k] or 0) for k in row.keys()}

    def append_message(self, user_id: int, role: str, text: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO messages (user_id, role, text, created_at) VALUES (?, ?, ?, ?)",
                (user_id, role, text, _utc_now()),
            )

    def history(self, user_id: int) -> list[sqlite3.Row]:
        with self._conn() as conn:
            return conn.execute(
                "SELECT role, text FROM messages WHERE user_id = ? ORDER BY id", (user_id,)
            ).fetchall()
