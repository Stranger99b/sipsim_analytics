#!/usr/bin/env python3
"""
Забирает CDR-записи звонков из SIPSIM API и кэширует телефонный справочник.

Запуск:
  python3 fetch_calls.py                  # вчера
  python3 fetch_calls.py --date 2026-05-10
  python3 fetch_calls.py --phones          # только обновить справочник менеджеров
"""

import os
import sys
import json
import argparse
import requests
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

SIPSIM_TOKEN = os.environ["SIPSIM_TOKEN"]
BASE_URL = "https://app.sipsim.by/api/v1"
DATA_DIR = Path(__file__).parent.parent / "data"
CALLS_DIR = DATA_DIR / "calls"
PHONES_FILE = DATA_DIR / "phones_cache.json"

CALLS_DIR.mkdir(parents=True, exist_ok=True)


def fetch_phones() -> dict:
    """Загружает справочник телефонов/менеджеров и кэширует локально."""
    resp = requests.get(f"{BASE_URL}/phones", params={"token": SIPSIM_TOKEN}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    phones_list = data.get("phones", data) if isinstance(data, dict) else data
    PHONES_FILE.write_text(json.dumps(phones_list, ensure_ascii=False, indent=2))
    print(f"[fetch_calls] Phones cached: {len(phones_list)} phones → {PHONES_FILE}")
    return phones_list


def load_phones() -> dict:
    if PHONES_FILE.exists():
        return json.loads(PHONES_FILE.read_text())
    return fetch_phones()


def fetch_calls_for_date(date: datetime) -> list:
    """Получает все звонки за дату с пагинацией."""
    date_str = date.strftime("%d.%m.%Y")
    all_calls = []
    page = 0

    while True:
        params = {
            "token": SIPSIM_TOKEN,
            "start_date": date_str,
            "end_date": date_str,
            "page": page,
        }
        resp = requests.get(f"{BASE_URL}/calls", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, list):
            calls = data
        else:
            calls = data.get("calls", data.get("data", []))

        if not calls:
            break

        all_calls.extend(calls)
        total = data.get("total", len(all_calls)) if isinstance(data, dict) else len(all_calls)
        limit = data.get("limit", 100) if isinstance(data, dict) else 100
        print(f"[fetch_calls] Page {page}: got {len(calls)} calls (total reported: {total})")

        if len(all_calls) >= total:
            break
        page += 1

    return all_calls


def save_calls(date: datetime, calls: list) -> Path:
    out_file = CALLS_DIR / f"{date.strftime('%Y-%m-%d')}.json"
    out_file.write_text(json.dumps(calls, ensure_ascii=False, indent=2))
    print(f"[fetch_calls] Saved {len(calls)} calls → {out_file}")
    return out_file


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--phones", action="store_true", help="Update phones cache only")
    args = parser.parse_args()

    if args.phones:
        fetch_phones()
        return

    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d")
    else:
        target_date = datetime.now() - timedelta(days=1)

    print(f"[fetch_calls] Fetching calls for {target_date.strftime('%Y-%m-%d')}...")
    calls = fetch_calls_for_date(target_date)
    save_calls(target_date, calls)

    answered = sum(1 for c in calls if c.get("sip_status") == "answer")
    print(f"[fetch_calls] Done: {len(calls)} total, {answered} answered")


if __name__ == "__main__":
    main()
