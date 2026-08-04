import asyncio
import json
import logging
import re
from datetime import datetime, timezone

from telethon import events
from telethon.tl.types import User

from config.settings import AppSettings
from db.database import Database
from services.catalog import load_catalog_json
from services.leads import build_lead_report, build_off_topic_report, extract_phone
from services.poe_client import PoeClient
from tg.client import ManagerClient
from tg.gatekeeper import classify
from tg.message_batcher import MessageBatcher

logger = logging.getLogger(__name__)

# Синтетическая классификация для дожима — код подставляет её сам, минуя
# классификатор (клиент ничего не писал, классифицировать нечего).
_FOLLOWUP_INTENTS = {1: "followup_2h", 2: "followup_24h"}


_CEILING_RE = re.compile(
    r"потолк|потолок|натяжн|глянц|матов|сатин|тканев|бесщел|теневой|"
    r"замер|монтаж|полотн|светильник|парящ",
    re.IGNORECASE,
)
_AFFIRM_RE = re.compile(r"^\s*(да|ага|верно|конечно|именно|точно|ок|окей)\b", re.IGNORECASE)
_NEGATION_RE = re.compile(r"\bне\b|\bнет\b", re.IGNORECASE)


def _contradicts_off_topic(text: str) -> bool:
    """Предохранитель перед НЕОБРАТИМЫМ отключением чата.

    Классификатор иногда ошибочно отдаёт off_topic_confirmed на согласие
    клиента («да, по потолкам»), а цена такой ошибки — молча потерянный
    живой клиент. Поэтому если в сообщении есть признак целевого («потолки»,
    «замер», «да») и при этом нет отрицания — отключение не выполняем.
    Отрицание («да не, я не по потолкам») снимает защиту: там классификатор
    прав.
    """
    t = (text or "").lower()
    if _NEGATION_RE.search(t):
        return False
    return bool(_CEILING_RE.search(t) or _AFFIRM_RE.match(t))


def _extract_intent(classifier_output: str | None) -> str | None:
    """intent из ответа классификатора. Тот оборачивает JSON в ```json-фенсы,
    поэтому чистим их перед разбором. Любой сбой парсинга -> None (диалог
    продолжается как обычно, а не рвётся из-за кривого JSON)."""
    if not classifier_output:
        return None
    cleaned = classifier_output.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(cleaned).get("intent")
    except (ValueError, AttributeError):
        logger.warning("Не удалось разобрать ответ классификатора: %s", cleaned[:200])
        return None


class ReactiveWorker:
    """Отвечает ТОЛЬКО тем, у кого не было переписки до бота (см. gatekeeper).
    Всем остальным — молчание, бот никогда не пишет первым и не встревает
    в чаты с историей.

    Персона и правила поведения — на стороне двух вынесенных ботов Poe
    (POE_CLASSIFIER_MODEL, POE_CHAT_MODEL), код не хранит промптов и не
    принимает решений по содержимому ответа — только собирает и
    пересылает сопутствующие данные (историю диалога, вывод классификатора)."""

    def __init__(self, settings: AppSettings, tg: ManagerClient, db: Database, poe: PoeClient):
        self.settings = settings
        self.tg = tg
        self.db = db
        self.poe = poe
        self._user_locks: dict[int, asyncio.Lock] = {}
        # dict как упорядоченное множество: вытеснять надо СТАРЫЕ id, а не
        # произвольные. set() для этого не годится — у него нет порядка, и
        # list(set)[-1000:] выбрасывал в том числе только что добавленный id,
        # из-за чего on_outgoing принимал собственное сообщение бота за ручное
        # и навсегда отключал бота в живом клиентском чате.
        self._bot_sent_ids: dict[int, None] = {}
        self._catalog_json = load_catalog_json()  # knowledge/catalog.json, читается один раз

        self._batcher = MessageBatcher(
            batch_wait_sec=settings.message_batch_wait_sec,
            typing_idle_sec=settings.typing_idle_sec,
            max_wait_sec=settings.message_batch_max_wait_sec,
            settle_sec=settings.message_batch_settle_sec,
            tail_wait_sec=settings.message_batch_tail_wait_sec,
            on_flush=self._flush_batch,
            log_prefix="[karina]",
        )

    def _user_lock(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._user_locks:
            self._user_locks[user_id] = asyncio.Lock()
        return self._user_locks[user_id]

    def _remember_bot_message(self, msg_id: int) -> None:
        """Запоминаем id отправленного ботом сообщения, чтобы on_outgoing не
        принял его за ручное сообщение владельца. Вытесняем самые старые."""
        self._bot_sent_ids[msg_id] = None
        while len(self._bot_sent_ids) > 2000:
            self._bot_sent_ids.pop(next(iter(self._bot_sent_ids)))

    def _is_bot_owned(self, user_id: int) -> bool:
        row = self.db.get_chat(user_id)
        return bool(row and row["status"] == "new")

    def _build_messages(self, user_id: int) -> list[dict]:
        """Только данные: сопутствующая инфа о компании/менеджере (если
        заполнена), каталог цен (knowledge/catalog.json, целиком как JSON)
        + вся история диалога. Ни промпта, ни правил, ни подсказок по
        режиму ответа — это настраивается в самих ботах Poe."""
        messages: list[dict] = []
        if self.settings.company_name or self.settings.manager_name:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"[ДАННЫЕ] company_name={self.settings.company_name}; "
                        f"manager_name={self.settings.manager_name}"
                    ),
                }
            )
        if self._catalog_json:
            messages.append(
                {"role": "user", "content": f"[КАТАЛОГ JSON]\n{self._catalog_json}"}
            )
        for row in self.db.history(user_id):
            role = "assistant" if row["role"] == "bot" else "user"
            messages.append({"role": role, "content": row["text"]})
        return messages

    async def _flush_batch(self, user_id: int, combined_text: str, _max_message_id) -> None:
        async with self._user_lock(user_id):
            if not self._is_bot_owned(user_id):
                return  # владелец успел написать сам, пока копился батч
            for part in (p.strip() for p in combined_text.split("\n")):
                if part:
                    self.db.append_message(user_id, "user", part)
            self.db.set_followup_stage(user_id, 0)  # клиент снова активен — счётчик дожима с нуля

        if not self._is_bot_owned(user_id):
            return

        messages = self._build_messages(user_id)

        classifier_output = None
        if self.settings.poe_classifier_model:
            try:
                classifier_output = await self.poe.complete(
                    self.settings.poe_classifier_model, messages
                )
            except Exception:
                logger.exception("Poe classifier failed user=%s", user_id)

        if _extract_intent(classifier_output) == "off_topic_confirmed":
            if _contradicts_off_topic(combined_text):
                logger.warning(
                    "off_topic_confirmed для user=%s, но сообщение похоже на целевое "
                    "(%s) — отключение отменено, продолжаем диалог",
                    user_id,
                    combined_text[:80],
                )
            else:
                # Не по теме — молча уходим, диалоговый бот вообще не вызывается,
                # клиенту ничего не пишем, только отчёт в группу.
                await self._shutdown_off_topic(user_id)
                return

        dialog_messages = list(messages)
        if classifier_output:
            dialog_messages.append(
                {"role": "user", "content": f"[КЛАССИФИКАЦИЯ]\n{classifier_output}"}
            )

        try:
            reply = await self.poe.complete(self.settings.poe_chat_model, dialog_messages)
        except Exception:
            logger.exception("Poe dialog failed user=%s", user_id)
            return

        if not self._is_bot_owned(user_id):
            return  # владелец написал сам, пока ждали ответ от Poe

        msg_id = await self.tg.send(user_id, reply)
        self._remember_bot_message(msg_id)

        self.db.append_message(user_id, "bot", reply)

        phone = extract_phone(combined_text)
        if phone and not self.db.is_lead_notified(user_id):
            await self._notify_lead(user_id, phone)

    async def _shutdown_off_topic(self, user_id: int) -> None:
        """Человек обратился не по потолкам — бот отключается в этом чате
        НАВСЕГДА (тот же необратимый статус 'existing', что и при ручном
        вмешательстве владельца) и передаёт диалог живому человеку."""
        await self._batcher.cancel(user_id)
        self.db.force_existing(user_id)
        logger.info("user=%s не по теме — бот отключён в этом чате навсегда", user_id)
        row = self.db.get_chat(user_id)
        username = row["username"] if row else None
        try:
            await self.tg.notify_leads_group(
                build_off_topic_report(username, user_id, self.db.history(user_id))
            )
        except Exception:
            logger.exception("Не удалось отправить уведомление о нецелевом user=%s", user_id)

    async def _notify_lead(self, user_id: int, phone: str) -> None:
        row = self.db.get_chat(user_id)
        username = row["username"] if row else None
        report = build_lead_report(username, user_id, phone, self.db.history(user_id))
        try:
            await self.tg.notify_leads_group(report)
            self.db.mark_lead_notified(user_id)
        except Exception:
            logger.exception("Не удалось отправить заявку в группу лидов user=%s", user_id)

    # ------------------------------------------------------------------ #
    # Дожим: клиент замолчал, не оставив телефон — бот сам напоминает о
    # себе через FOLLOWUP_1_DELAY_SEC (по умолчанию 2 ч), и ещё раз через
    # FOLLOWUP_2_DELAY_SEC (по умолчанию сутки), потом больше не пишет.
    # ------------------------------------------------------------------ #

    async def run_followup_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.followup_poll_sec)
            try:
                await self._check_followups()
            except Exception:
                logger.exception("Ошибка в цикле дожима")

    async def _check_followups(self) -> None:
        now = datetime.now(timezone.utc)
        for row in self.db.candidates_for_followup():
            stage = row["followup_stage"]
            if stage == 0:
                last_raw = row["last_user_msg_at"]  # молчание клиента
                delay = self.settings.followup_1_delay_sec
                next_stage = 1
            else:
                last_raw = row["followup_last_sent_at"]  # от первого дожима, не от клиента
                delay = self.settings.followup_2_delay_sec
                next_stage = 2
            if not last_raw:
                continue
            elapsed = (now - datetime.fromisoformat(last_raw)).total_seconds()
            if elapsed >= delay:
                await self._send_followup(row["user_id"], next_stage)

    async def _send_followup(self, user_id: int, stage: int) -> None:
        async with self._user_lock(user_id):
            if not self._is_bot_owned(user_id) or self.db.is_lead_notified(user_id):
                return  # клиент вернулся / заявка уже ушла, пока копился дожим

            classification = json.dumps(
                {
                    "intent": _FOLLOWUP_INTENTS[stage],
                    "extracted_answer": None,
                    "extracted_question": None,
                    "unclear_reason": None,
                },
                ensure_ascii=False,
            )
            messages = self._build_messages(user_id)
            messages.append({"role": "user", "content": f"[КЛАССИФИКАЦИЯ]\n{classification}"})

            try:
                reply = await self.poe.complete(self.settings.poe_chat_model, messages)
            except Exception:
                logger.exception("Poe followup failed user=%s stage=%s", user_id, stage)
                return

            if not self._is_bot_owned(user_id):
                return

            msg_id = await self.tg.send(user_id, reply)
            self._remember_bot_message(msg_id)
            self.db.append_message(user_id, "bot", reply)
            self.db.record_followup_sent(user_id, stage)
            logger.info("Дожим stage=%s отправлен user=%s", stage, user_id)

    def register(self) -> None:
        client = self.tg.client

        @client.on(events.NewMessage(incoming=True))
        async def on_incoming(event):
            if self.settings.self_test:
                return  # SELF_TEST: реальные входящие не обрабатываем вообще
            if not event.is_private:
                return
            sender = await event.get_sender()
            if not isinstance(sender, User) or sender.bot or sender.is_self:
                return
            text = (event.raw_text or "").strip()
            if not text:
                return

            user_id = sender.id
            if sender.contact or sender.mutual_contact:
                # Человек записан в контакты аккаунта — это знакомый, а не
                # холодный лид, даже если переписки в Telegram ещё не было.
                # Помечаем 'existing' навсегда и молчим.
                self.db.mark_existing(user_id, sender.username)
                logger.info("user=%s есть в контактах аккаунта — бот не вмешивается", user_id)
                return

            async with self._user_lock(user_id):
                # Лок нужен, чтобы 2 быстрых сообщения от подлинно нового
                # контакта не race'ились: без него второй вызов classify()
                # мог бы увидеть первое (уже реально отправленное) сообщение
                # как "предыдущее" и ошибочно пометить чат existing.
                status = await classify(client, self.db, user_id, sender.username, event.id)
            if status == "existing":
                return  # переписка была до бота — не вмешиваемся

            await self.tg.mark_read(user_id)
            await self._batcher.enqueue(user_id, text, message_id=event.id)

        @client.on(events.NewMessage(outgoing=True))
        async def on_outgoing(event):
            if not event.is_private or event.id in self._bot_sent_ids:
                return
            peer = await event.get_chat()
            if not isinstance(peer, User):
                return

            if self.settings.self_test and peer.is_self:
                await self._handle_self_test_message(event)
                return

            if peer.is_self:
                return  # self-test выключен — свои сообщения в Избранном не трогаем

            # Владелец аккаунта написал вручную в чат, который ведёт бот, —
            # бот замолкает в этом чате НАВСЕГДА (чат помечается 'existing',
            # как будто переписка была всегда).
            user_id = peer.id
            if not self._is_bot_owned(user_id):
                return  # чат, который бот не ведёт — не вмешиваемся
            await self._batcher.cancel(user_id)
            self.db.force_existing(user_id)
            logger.info(
                "Владелец сам написал user=%s — бот больше НИКОГДА не ответит в этом чате",
                user_id,
            )

    async def _handle_self_test_message(self, event) -> None:
        """SELF_TEST=1: сообщение в Избранное (без метки ManagerClient.SELF_TEST_MARKER
        — это ответ бота) обрабатывается как входящее от «клиента». Гарантированно
        'new' — тестовый чат не подчиняется правилу «не пишем в существующие чаты»."""
        text = (event.raw_text or "").strip()
        if not text or text.startswith(self.tg.SELF_TEST_MARKER.strip()):
            return  # это наш же ответ, а не тестовый ввод
        user_id = self.tg.me_id
        self.db.force_new(user_id)
        await self._batcher.enqueue(user_id, text, message_id=event.id)
