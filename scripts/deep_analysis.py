#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Глубокий («наставнический») AI-разбор ВХОДЯЩИХ звонков для дашборда менеджеров.

Дополняет базовый analyze_calls.py: для «косячных» звонков (низкое качество / есть issues /
жалоба / info_only) вытаскивает из транскрипта КОНКРЕТНЫЙ разбор для менеджера
(что не так + как правильно + цитата) и механики звонка (валидация клиента, альтернативы,
удержание/следующий шаг, попытка продажи).

Пишет в data/deep/<public_id>.json (идемпотентно). Модель — та же, что в analyze_calls
(claude --print), с параллелизмом.

Запуск:
  python3 deep_analysis.py --month 2026-07            # разобрать косяки месяца
  python3 deep_analysis.py --start 2026-07-01 --end 2026-07-21 [--workers 5] [--force]
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
from manager_stats import load_phones_map, load_analyses_for_date, get_manager_name

DATA_DIR = Path(__file__).parent.parent / "data"
CALLS_DIR = DATA_DIR / "calls"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
DEEP_DIR = DATA_DIR / "deep"
DEEP_DIR.mkdir(parents=True, exist_ok=True)
CLAUDE_BIN = shutil.which("claude") or "/home/user/.local/bin/claude"
# claude --print без --model берёт модель сессии (Opus, дорого). Haiku 4.5 ~5× дешевле;
# для наставнического разбора её достаточно. Переопределить: --model claude-sonnet-5.
MODEL = "claude-haiku-4-5"

# Менеджеры (SIPSIM-имя → короткое). РОП/агенты/общий отдел не разбираем.
MANAGERS = {"Фёдорова Анастасия", "Рогачевская Карина", "Яршевич Екатерина", "Лазарчук Кристина"}

PROMPT = """Ты — наставник отдела продаж турагентства GoTrips (туры по России и СНГ).
Разбери ВХОДЯЩИЙ телефонный разговор менеджера с клиентом и оцени работу МЕНЕДЖЕРА как тренер —
чтобы менеджер понял, что сделал не так и как надо. Speaker 0 обычно менеджер, Speaker 1 клиент.

Ответ СТРОГО в виде JSON без markdown и пояснений:
{
  "problem": "1 короткая фраза: главная ошибка менеджера в этом звонке",
  "advice": "1-2 фразы: что нужно было сделать (конкретно, как речевой приём/шаг)",
  "quote": "короткая цитата из разговора (1-2 реплики), иллюстрирующая ошибку",
  "category": "инфо-агентство|не выявил потребность|не предложил альтернативу|не назначил следующий шаг|не взял контакт|слабая отработка возражения|невежливость|не дожал|другое",
  "offered_alternatives": true,
  "qualified_client": true,
  "client_ready": true,
  "set_next_contact": true,
  "sold_attempt": true
}

Определения булевых полей:
- qualified_client: менеджер уточнил ключевые параметры тура (даты И/ИЛИ число человек И/ИЛИ программа/экскурсии).
- client_ready: клиент уже определился (назвал даты и состав), а не просто «прицениться» (зевака).
- set_next_contact: договорились о конкретном следующем шаге или времени перезвона.
- sold_attempt: менеджер предлагал/подводил к покупке, а не только консультировал.

ТРАНСКРИПТ:
{transcript}"""


def _format_transcript(tr: dict) -> str:
    if tr.get("utterances"):
        return "\n".join(f"[Speaker {u['speaker']}] {u['text']}" for u in tr["utterances"])
    return tr.get("transcript", "")


def analyze_deep(public_id: str, force: bool = False, model: str = MODEL) -> dict | None:
    out_file = DEEP_DIR / f"{public_id}.json"
    if out_file.exists() and not force:
        return json.loads(out_file.read_text())
    tf = TRANSCRIPTS_DIR / f"{public_id}.json"
    if not tf.exists():
        return None
    tr = json.loads(tf.read_text())
    text = _format_transcript(tr)
    if not text or len(text.strip()) < 20:
        return None
    prompt = PROMPT.replace("{transcript}", text)
    for attempt in range(3):
        try:
            proc = subprocess.run(
                [CLAUDE_BIN, "--print", "--model", model,
                 "--dangerously-skip-permissions", "-p", prompt],
                capture_output=True, text=True, timeout=150,
            )
            raw = proc.stdout.strip() if proc.returncode == 0 else ""
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            if not raw:
                raise ValueError(f"empty (exit={proc.returncode})")
            parsed = json.loads(raw)
            parsed["public_id"] = public_id
            out_file.write_text(json.dumps(parsed, ensure_ascii=False, indent=2))
            return parsed
        except Exception as e:
            if attempt < 2:
                time.sleep((attempt + 1) * 8)
            else:
                print(f"[deep] ERROR {public_id}: {e}", flush=True)
                return None


def find_candidates(start_dt, end_dt):
    """Косячные входящие: качество≤2 / есть issues / жалоба. Возвращает список public_id."""
    pm = load_phones_map()
    ids, d = [], start_dt
    while d <= end_dt:
        f = CALLS_DIR / f"{d.strftime('%Y-%m-%d')}.json"
        if f.exists():
            calls = json.loads(f.read_text())
            an = load_analyses_for_date(d)
            for c in calls:
                if c.get("call_type") != "inbound" or c.get("sip_status") != "answer":
                    continue
                if get_manager_name(c, pm) not in MANAGERS:
                    continue
                a = an.get(c.get("public_id"), {})
                if not a:
                    continue
                q = a.get("quality_score")
                if (q is not None and q <= 2) or a.get("issues") or a.get("outcome") == "complaint":
                    ids.append(c.get("public_id"))
        d += timedelta(days=1)
    return ids


def backfill(ids, workers=5, force=False, model=MODEL):
    todo = [i for i in ids if force or not (DEEP_DIR / f"{i}.json").exists()]
    print(f"[deep] кандидатов: {len(ids)}, к разбору: {len(todo)}, "
          f"воркеров: {workers}, модель: {model}", flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(analyze_deep, i, force, model): i for i in todo}
        for fut in as_completed(futs):
            done += 1
            r = fut.result()
            if done % 5 == 0 or done == len(todo):
                print(f"[deep] {done}/{len(todo)}", flush=True)
    print(f"[deep] готово. Файлов в data/deep: {len(list(DEEP_DIR.glob('*.json')))}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", help="YYYY-MM")
    ap.add_argument("--start"); ap.add_argument("--end")
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--model", default=MODEL, help=f"по умолчанию {MODEL} (дёшево)")
    a = ap.parse_args()
    if a.start:
        start = datetime.strptime(a.start, "%Y-%m-%d")
        end = datetime.strptime(a.end, "%Y-%m-%d") + timedelta(hours=23, minutes=59)
    else:
        today = datetime.now()
        if a.month:
            y, m = map(int, a.month.split("-"))
        else:                       # по умолчанию — текущий месяц (для cron)
            y, m = today.year, today.month
        start = datetime(y, m, 1)
        end_day = today.day if (y, m) == (today.year, today.month) else monthrange(y, m)[1]
        end = datetime(y, m, end_day, 23, 59, 59)
    ids = find_candidates(start, end)
    backfill(ids, workers=a.workers, force=a.force, model=a.model)


if __name__ == "__main__":
    main()
