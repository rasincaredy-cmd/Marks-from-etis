"""
Bot configuration.
Токен читается из переменной окружения BOT_TOKEN.
На Railway: Settings -> Variables -> BOT_TOKEN = твой_токен
Локально: создай файл .env рядом с ботом со строкой BOT_TOKEN=твой_токен
"""

import os
from pathlib import Path

# Загружаем .env если есть (для локального запуска)
env_file = Path(__file__).parent / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")

if not BOT_TOKEN:
    raise ValueError(
        "Токен не найден!\n"
        "На Railway: добавь BOT_TOKEN в Variables\n"
        "Локально: создай .env файл со строкой BOT_TOKEN=твой_токен"
    )
