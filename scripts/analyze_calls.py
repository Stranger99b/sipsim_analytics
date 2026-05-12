#!/usr/bin/env python3
"""
AI-анализ транскриптов звонков через Claude.

Для каждого транскрипта определяет:
- Направление тура
- Качество работы менеджера (1-5)
- Исход звонка
- Проблемы и возражения клиента

Запуск:
  python3 analyze_calls.py                   # вчерашний день
  python3 analyze_calls.py --date 2026-05-10
  python3 analyze_calls.py --force
"""

import os
import sys
import json
import shutil
import argparse
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

DATA_DIR = Path(__file__).parent.parent / "data"
CALLS_DIR = DATA_DIR / "calls"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
ANALYSIS_DIR = DATA_DIR / "analysis"

ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)

CLAUDE_BIN = shutil.which("claude") or "/home/user/.local/bin/claude"

ANALYSIS_PROMPT = """Ты — аналитик колл-центра туристической компании GoTrips (Беларусь). Туры по России и СНГ.
Тебе дана расшифровка телефонного разговора с диаризацией (speaker 0, speaker 1).

ТИП ЗВОНКА: {call_type_ru}
{call_type_hint}

Ответ СТРОГО только JSON без markdown-блоков и пояснений:
{
  "direction": "название тура/направления (Питер, Карелия, Дагестан, Мурманск, ...) или null если не упоминалось",
  "quality_score": 3,
  "outcome": "interested|not_interested|booked|callback|client_unavailable|info_only|complaint|wrong_number",
  "issues": [],
  "objections": [],
  "manager_highlights": "1-2 предложения об оценке работы менеджера"
}

Критерии quality_score:
1 — грубость, игнор вопросов, бросил трубку, потерял клиента
2 — формально ответил, без вовлечения, упустил возможность
3 — норма: ответил на вопросы, дал информацию
4 — хорошо: выявил потребности, предложил конкретные варианты
5 — отлично: выявил потребности, обработал возражения, договорился о следующем шаге

ВАЖНО для исходящих звонков: если клиент сказал что занят, недоступен, просит перезвонить —
это не вина менеджера. Исход = client_unavailable, оценка не ниже 3.
ВАЖНО: не штрафуй менеджера за "не уточнил детали" если клиент отказался говорить.

Возможные issues: "грубость", "не перезвонил", "долгое ожидание", "не предложил альтернативу",
"не уточнил даты/количество человек", "бросил трубку", "не взял контакт"

ТРАНСКРИПТ:
{transcript}"""


def analyze_call(transcript_data: dict, force: bool = False) -> dict | None:
    public_id = transcript_data.get("public_id")
    transcript = transcript_data.get("transcript", "")
    call_type = transcript_data.get("call_type", "inbound")

    out_file = ANALYSIS_DIR / f"{public_id}.json"
    if out_file.exists() and not force:
        return json.loads(out_file.read_text())

    if not transcript or len(transcript.strip()) < 20:
        result = {
            "public_id": public_id,
            "direction": None,
            "quality_score": None,
            "outcome": "no_transcript",
            "issues": [],
            "objections": [],
            "manager_highlights": "Транскрипт отсутствует или слишком короткий",
        }
        out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    # Форматируем транскрипт с диаризацией
    if transcript_data.get("utterances"):
        formatted = "\n".join(
            f"[Speaker {u['speaker']}] {u['text']}"
            for u in transcript_data["utterances"]
        )
    else:
        formatted = transcript

    if call_type == "outbound":
        call_type_ru = "ИСХОДЯЩИЙ (менеджер звонит клиенту)"
        call_type_hint = "Speaker 0 = менеджер, Speaker 1 = клиент (менеджер звонит первым)."
    else:
        call_type_ru = "ВХОДЯЩИЙ (клиент звонит в компанию)"
        call_type_hint = "Обычно Speaker 0 = менеджер (отвечает), Speaker 1 = клиент."

    prompt = (ANALYSIS_PROMPT
              .replace("{call_type_ru}", call_type_ru)
              .replace("{call_type_hint}", call_type_hint)
              .replace("{transcript}", formatted))

    for attempt in range(3):
        try:
            proc = subprocess.run(
                [CLAUDE_BIN, "--print", "--dangerously-skip-permissions", "-p", prompt],
                capture_output=True, text=True, timeout=120,
            )
            raw = proc.stdout.strip() if proc.returncode == 0 else ""

            # Чистим возможный markdown
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]

            if not raw:
                raise ValueError(f"Empty response from Claude (exit={proc.returncode})")

            parsed = json.loads(raw)
            parsed["public_id"] = public_id
            out_file.write_text(json.dumps(parsed, ensure_ascii=False, indent=2))
            return parsed

        except Exception as e:
            if attempt < 2:
                wait = (attempt + 1) * 10
                print(f"[analyze] retry {attempt+1}/2 for {public_id} ({e}), wait {wait}s...", flush=True)
                time.sleep(wait)
            else:
                print(f"[analyze] ERROR {public_id}: {e}")
                # Сохраняем заглушку только если нет уже рабочего результата
                if not out_file.exists():
                    fallback = {
                        "public_id": public_id,
                        "direction": None,
                        "quality_score": None,
                        "outcome": "unknown",
                        "issues": [],
                        "objections": [],
                        "manager_highlights": f"Анализ не удался: {str(e)[:60]}",
                    }
                    out_file.write_text(json.dumps(fallback, ensure_ascii=False, indent=2))
                return None


def analyze_date(date: datetime, force: bool = False) -> list[dict]:
    calls_file = CALLS_DIR / f"{date.strftime('%Y-%m-%d')}.json"
    if not calls_file.exists():
        print(f"[analyze] No calls file for {date.strftime('%Y-%m-%d')}")
        sys.exit(1)

    calls = json.loads(calls_file.read_text())
    call_ids = {c.get("public_id") for c in calls if c.get("sip_status") == "answer"}

    transcripts = []
    for pid in call_ids:
        f = TRANSCRIPTS_DIR / f"{pid}.json"
        if f.exists():
            transcripts.append(json.loads(f.read_text()))

    print(f"[analyze] {len(transcripts)} transcripts to analyze")

    results = []
    for i, t in enumerate(transcripts, 1):
        pid = t.get("public_id", "?")
        print(f"[analyze] [{i}/{len(transcripts)}] {pid}...", end=" ", flush=True)
        result = analyze_call(t, force=force)
        if result:
            print(f"score={result.get('quality_score')} outcome={result.get('outcome')}")
            results.append(result)
        else:
            print("failed")

    print(f"[analyze] Done: {len(results)} analyzed")
    return results


def load_analysis_for_date(date: datetime) -> list[dict]:
    calls_file = CALLS_DIR / f"{date.strftime('%Y-%m-%d')}.json"
    if not calls_file.exists():
        return []
    calls = json.loads(calls_file.read_text())
    results = []
    for call in calls:
        pid = call.get("public_id")
        f = ANALYSIS_DIR / f"{pid}.json"
        if f.exists():
            a = json.loads(f.read_text())
            a["_call"] = call
            results.append(a)
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    target_date = (
        datetime.strptime(args.date, "%Y-%m-%d") if args.date
        else datetime.now() - timedelta(days=1)
    )
    analyze_date(target_date, force=args.force)


if __name__ == "__main__":
    main()
