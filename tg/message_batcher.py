import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

FlushHandler = Callable[[int, str, int | None], Awaitable[None]]


@dataclass
class _BatchState:
    texts: list[str] = field(default_factory=list)
    message_ids: list[int] = field(default_factory=list)
    last_message_at: float = 0.0
    last_typing_at: float | None = None
    worker_task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    flush_in_progress: bool = False
    tail_mode: bool = False


class MessageBatcher:
    """Очередь сообщений: пауза после сообщений + typing, settle перед flush."""

    def __init__(
        self,
        *,
        batch_wait_sec: float,
        typing_idle_sec: float,
        max_wait_sec: float,
        settle_sec: float,
        tail_wait_sec: float,
        on_flush: FlushHandler,
        log_prefix: str = "",
    ):
        self.batch_wait_sec = batch_wait_sec
        self.typing_idle_sec = typing_idle_sec
        self.max_wait_sec = max_wait_sec
        self.settle_sec = settle_sec
        self.tail_wait_sec = tail_wait_sec
        self.on_flush = on_flush
        self.log_prefix = log_prefix
        self._states: dict[int, _BatchState] = {}

    async def enqueue(
        self,
        user_id: int,
        text: str,
        *,
        message_id: int | None = None,
    ) -> None:
        state = self._states.get(user_id)
        if state is None:
            state = _BatchState()
            self._states[user_id] = state

        async with state.lock:
            was_empty = not state.texts
            state.texts.append(text)
            if message_id is not None:
                state.message_ids.append(message_id)
            state.last_message_at = time.monotonic()
            if state.flush_in_progress:
                state.tail_mode = True
            elif was_empty:
                state.tail_mode = False

            if state.worker_task is None or state.worker_task.done():
                state.worker_task = asyncio.create_task(self._worker(user_id))

        logger.info(
            "%s batch +user=%s total=%s tail=%s: %s",
            self.log_prefix,
            user_id,
            len(state.texts),
            state.tail_mode,
            text[:50],
        )

    def note_typing(self, user_id: int) -> None:
        state = self._states.get(user_id)
        if not state or not state.texts:
            return
        state.last_typing_at = time.monotonic()

    def _wait_sec(self, state: _BatchState) -> float:
        return self.tail_wait_sec if state.tail_mode else self.batch_wait_sec

    def _is_ready(self, state: _BatchState, now: float) -> bool:
        since_msg = now - state.last_message_at
        since_typing = (
            now - state.last_typing_at
            if state.last_typing_at is not None
            else float("inf")
        )
        if since_msg >= self.max_wait_sec:
            return True
        wait_sec = self._wait_sec(state)
        return since_msg >= wait_sec and since_typing >= self.typing_idle_sec

    async def cancel(self, user_id: int) -> None:
        state = self._states.get(user_id)
        if state is None:
            return

        async with state.lock:
            state.texts.clear()
            state.message_ids.clear()
            state.last_typing_at = None
            state.tail_mode = False
            if state.worker_task and not state.worker_task.done():
                state.worker_task.cancel()
            state.worker_task = None

        logger.info("%s batch CANCEL user=%s", self.log_prefix, user_id)

    async def _worker(self, user_id: int) -> None:
        try:
            while True:
                await asyncio.sleep(0.2)
                state = self._states.get(user_id)
                if not state or not state.texts:
                    return
                if state.flush_in_progress:
                    continue

                now = time.monotonic()
                if not self._is_ready(state, now):
                    continue

                await asyncio.sleep(self.settle_sec)

                state = self._states.get(user_id)
                if not state or not state.texts or state.flush_in_progress:
                    continue
                now = time.monotonic()
                if not self._is_ready(state, now):
                    continue

                async with state.lock:
                    if not state.texts or state.flush_in_progress:
                        continue
                    now = time.monotonic()
                    if not self._is_ready(state, now):
                        continue
                    texts = list(state.texts)
                    message_ids = list(state.message_ids)
                    state.texts.clear()
                    state.message_ids.clear()
                    state.last_typing_at = None
                    state.flush_in_progress = True

                combined = "\n".join(texts)
                max_message_id = max(message_ids) if message_ids else None
                logger.info(
                    "%s batch FLUSH user=%s parts=%s tail=%s",
                    self.log_prefix,
                    user_id,
                    len(texts),
                    state.tail_mode,
                )
                try:
                    await self.on_flush(user_id, combined, max_message_id)
                finally:
                    async with state.lock:
                        state.flush_in_progress = False

                state = self._states.get(user_id)
                if not state or not state.texts:
                    return
                logger.info(
                    "%s batch user=%s ещё %s сообщ. — ждём (tail=%s)",
                    self.log_prefix,
                    user_id,
                    len(state.texts),
                    state.tail_mode,
                )

        except asyncio.CancelledError:
            return
