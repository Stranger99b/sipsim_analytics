#!/usr/bin/env python3
"""Отправка сообщений в Telegram."""

import os
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
MAX_MSG_LEN = 4096


def send_message(text: str, chat_id: str = CHAT_ID, parse_mode: str = "HTML"):
    chunks = [text[i:i + MAX_MSG_LEN] for i in range(0, len(text), MAX_MSG_LEN)]
    for chunk in chunks:
        r = requests.post(
            f"{API_URL}/sendMessage",
            json={"chat_id": chat_id, "text": chunk, "parse_mode": parse_mode},
            timeout=30,
        )
        r.raise_for_status()
    print(f"[telegram] Sent {len(chunks)} message(s)")


def send_document(file_path: str, caption: str = "", chat_id: str = CHAT_ID):
    with open(file_path, "rb") as f:
        r = requests.post(
            f"{API_URL}/sendDocument",
            data={"chat_id": chat_id, "caption": caption},
            files={"document": f},
            timeout=60,
        )
    r.raise_for_status()
    print(f"[telegram] Document sent: {file_path}")


if __name__ == "__main__":
    send_message("✅ SIPSIM аналитика работает!")
