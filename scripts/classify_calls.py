#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Классификация ТЕМЫ звонка + продажный/нет (для дашборда менеджеров).

Проблема: базовый analyze_calls рассматривает все звонки как продажные (outcome booked/
interested/info_only…), поэтому сервисные/юридические/посторонние звонки мислейблятся и
портят продажные метрики и попадают в «косяки». Этот проход добавляет is_sales + theme,
НЕ трогая базовый анализ. Продажные метрики/косяки затем считаются только по is_sales=true.

Пишет в data/topics/<public_id>.json: {"is_sales": bool, "theme": str}.
Идемпотентно. claude --print, параллелизм (workers=2 стабильно).

Запуск:
  python3 classify_calls.py                 # текущий месяц, входящие отвеченные
  python3 classify_calls.py --month 2026-07 [--workers 2] [--force]
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from calendar import monthrange
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from manager_stats import load_phones_map, get_manager_name
from export_excel import build_transcript_text

DATA_DIR = Path(__file__).parent.parent / "data"
CALLS_DIR = DATA_DIR / "calls"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
TOPICS_DIR = DATA_DIR / "topics"
TOPICS_DIR.mkdir(parents=True, exist_ok=True)
CLAUDE_BIN = shutil.which("claude") or "/home/user/.local/bin/claude"

MANAGERS = {"Фёдорова Анастасия", "Рогачевская Карина", "Яршевич Екатерина", "Лазарчук Кристина"}
THEMES = {"продажа", "сервис_по_туру", "отмена_возврат", "оплата",
          "юридический_риск", "не_клиент", "жалоба", "другое"}

PROMPT = """Определи ТЕМУ входящего телефонного звонка в турагентство GoTrips и ПРОДАЖНЫЙ ли он.

ПРОДАЖНЫЙ (is_sales=true) = новый интерес к ПОКУПКЕ тура: клиент выбирает/спрашивает про тур,
который ещё НЕ купил, сравнивает варианты, узнаёт цену/даты с намерением поехать.
НЕ ПРОДАЖНЫЙ (is_sales=false) = сервис по уже купленному/бронированному туру, отмена/возврат,
вопрос оплаты существующей брони, юридический вопрос/ЧП/страховка/следователь, звонок
постороннего или не по адресу, жалоба без нового запроса на тур.

Ответ СТРОГО JSON без markdown:
{"is_sales": true, "theme": "продажа|сервис_по_туру|отмена_возврат|оплата|юридический_риск|не_клиент|жалоба|другое"}

ТРАНСКРИПТ:
{transcript}"""


def classify(public_id, force=False):
    out = TOPICS_DIR / f"{public_id}.json"
    if out.exists() and not force:
        return json.loads(out.read_text())
    tf = TRANSCRIPTS_DIR / f"{public_id}.json"
    if not tf.exists():
        return None
    text = build_transcript_text(json.loads(tf.read_text()))
    if not text or len(text.strip()) < 20:
        return None
    prompt = PROMPT.replace("{transcript}", text)
    for attempt in range(3):
        try:
            proc = subprocess.run(
                [CLAUDE_BIN, "--print", "--dangerously-skip-permissions", "-p", prompt],
                capture_output=True, text=True, timeout=120)
            raw = proc.stdout.strip() if proc.returncode == 0 else ""
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            if not raw:
                raise ValueError(f"empty (exit={proc.returncode})")
            parsed = json.loads(raw)
            theme = parsed.get("theme")
            parsed = {"public_id": public_id,
                      "is_sales": bool(parsed.get("is_sales")),
                      "theme": theme if theme in THEMES else "другое"}
            out.write_text(json.dumps(parsed, ensure_ascii=False, indent=2))
            return parsed
        except Exception as e:
            if attempt < 2:
                time.sleep((attempt + 1) * 8)
            else:
                print(f"[classify] ERROR {public_id}: {e}", flush=True)
                return None


def find_inbound(start_dt, end_dt):
    pm = load_phones_map()
    ids, d = [], start_dt
    while d <= end_dt:
        f = CALLS_DIR / f"{d.strftime('%Y-%m-%d')}.json"
        if f.exists():
            for c in json.loads(f.read_text()):
                if c.get("call_type") == "inbound" and c.get("sip_status") == "answer" \
                        and get_manager_name(c, pm) in MANAGERS:
                    ids.append(c.get("public_id"))
        d += timedelta(days=1)
    return ids


def backfill(ids, workers=2, force=False):
    todo = [i for i in ids if force or not (TOPICS_DIR / f"{i}.json").exists()]
    print(f"[classify] входящих: {len(ids)}, к классификации: {len(todo)}, воркеров: {workers}", flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(classify, i, force): i for i in todo}
        for _ in as_completed(futs):
            done += 1
            if done % 10 == 0 or done == len(todo):
                print(f"[classify] {done}/{len(todo)}", flush=True)
    print(f"[classify] готово. Файлов в data/topics: {len(list(TOPICS_DIR.glob('*.json')))}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month"); ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    today = datetime.now()
    if a.month:
        y, m = map(int, a.month.split("-"))
    else:
        y, m = today.year, today.month
    start = datetime(y, m, 1)
    end_day = today.day if (y, m) == (today.year, today.month) else monthrange(y, m)[1]
    end = datetime(y, m, end_day, 23, 59, 59)
    backfill(find_inbound(start, end), workers=a.workers, force=a.force)


if __name__ == "__main__":
    main()
