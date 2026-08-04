import httpx

from config.settings import SETTINGS


class PoeClient:
    """Тонкий клиент к Poe (OpenAI-совместимый /v1/chat/completions).

    Никакой персоны/промпта/бизнес-логики здесь нет — только пересылка
    данных (messages) конкретному вынесенному боту (model). Сама персона и
    правила поведения настраиваются на стороне Poe при создании бота.
    """

    URL = "https://api.poe.com/v1/chat/completions"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or SETTINGS.poe_api_key

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def complete(self, model: str, messages: list[dict]) -> str:
        if not self.api_key:
            raise RuntimeError("POE_API_KEY не задан в .env")
        if not model:
            raise RuntimeError("Имя бота на Poe не задано в .env")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {"model": model, "messages": messages}
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(self.URL, headers=headers, json=payload)
            if resp.status_code == 401:
                raise RuntimeError("POE: неверный API key (401)")
            if resp.status_code == 404:
                raise RuntimeError(f"POE: бот «{model}» не найден (404)")
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
