from pathlib import Path

from config.settings import BASE_DIR

CATALOG_PATH = BASE_DIR / "knowledge" / "catalog.json"


def load_catalog_json() -> str | None:
    """Сырой JSON-текст каталога (knowledge/catalog.json), как есть —
    без парсинга/интерпретации в коде. None, если файла нет."""
    if not CATALOG_PATH.exists():
        return None
    text = CATALOG_PATH.read_text(encoding="utf-8").strip()
    return text or None
