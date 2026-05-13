#!/usr/bin/env python3
"""Отправка сообщений в Telegram."""

import os
import re
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
MAX_MSG_LEN = 4096


def _strip_html(text: str) -> str:
    return re.sub(r'<[^>]+>', '', text)


def _split_safe(text: str, max_len: int) -> list:
    """Split text at paragraph boundaries to avoid cutting through HTML tags."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + max_len
        if end >= len(text):
            chunks.append(text[start:])
            break
        # Prefer splitting at double newline (between blocks)
        split_at = text.rfind('\n\n', start, end)
        if split_at <= start:
            split_at = text.rfind('\n', start, end)
        if split_at <= start:
            split_at = end
        chunks.append(text[start:split_at])
        # Skip the separator newlines
        start = split_at
        while start < len(text) and text[start] == '\n':
            start += 1
    return [c for c in chunks if c.strip()]


def send_message(text: str, chat_id: str = CHAT_ID, parse_mode: str = "HTML"):
    chunks = _split_safe(text, MAX_MSG_LEN)
    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": parse_mode}
        r = requests.post(f"{API_URL}/sendMessage", json=payload, timeout=30)
        if r.status_code == 400 and parse_mode == "HTML":
            desc = r.json().get("description", "")
            print(f"[telegram] HTML error on chunk {i+1}: {desc} — retrying as plain text")
            payload = {"chat_id": chat_id, "text": _strip_html(chunk)}
            r = requests.post(f"{API_URL}/sendMessage", json=payload, timeout=30)
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
