"""
Bot configuration.
Put your token in .env or directly here (not recommended for public repos).
"""

import os
from pathlib import Path

BOT_TOKEN = "8308035396:AAH9KmLj8m3xiv3B0S9nOY581Snjc2i5BGs"

# Load .env if present
#env_file = Path(__file__).parent / ".env"
#if env_file.exists():
#    for line in env_file.read_text().splitlines():
#        line = line.strip()
#        if line and not line.startswith("#") and "=" in line:
#            k, v = line.split("=", 1)
#            os.environ.setdefault(k.strip(), v.strip())

#BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

#if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
#    raise ValueError(
#        "Укажите токен бота в .env файле:\n"
#        "BOT_TOKEN=123456:ABC-DEF..."
#    )
