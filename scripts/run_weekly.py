#!/usr/bin/env python3
"""
Еженедельный отчёт SIPSIM для РОПа. Запускается каждый понедельник.

Запуск:
  python3 run_weekly.py           # неделя до вчера
  python3 run_weekly.py --date 2026-05-11  # неделя до этой даты
"""

import os
import sys
import json
import shutil
import subprocess
import argparse
import time
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

from manager_stats import get_weekly_manager_block, format_manager_scorecard, compute_callback_recovery, load_phones_map, compute_stats, load_analyses_for_date
from direction_stats import get_weekly_direction_block
from send_telegram import send_message

DATA_DIR = Path(__file__).parent.parent / "data"
CALLS_DIR = DATA_DIR / "calls"
ANALYSIS_DIR = DATA_DIR / "analysis"

WEEKLY_AI_PROMPT = """Ты — бизнес-аналитик для РОПа (руководителя отдела продаж) туристической компании GoTrips.
Проанализируй данные по телефонным звонкам за неделю.

Задача: помочь РОПу понять:
1. Кто из менеджеров продаёт хорошо, кому нужна помощь
2. Какие направления туров наиболее востребованы по телефону
3. Системные проблемы (пропущенные звонки, низкое качество)
4. Конкретные действия на следующую неделю

Ответ строго на русском. Структура:

<b>ОЦЕНКА НЕДЕЛИ</b>
[2-3 предложения: общий уровень, тренд]

<b>МЕНЕДЖЕРЫ</b>
[по каждому кто выделяется — хорошо или плохо, конкретно]

<b>НАПРАВЛЕНИЯ</b>
[какие туры чаще спрашивают, что это значит для продаж]

<b>СИСТЕМНЫЕ ПРОБЛЕМЫ</b>
[пропущенные звонки, частые возражения, паттерны]

<b>ДЕЙСТВИЯ НА НЕДЕЛЮ</b>
[3-5 конкретных пунктов для РОПа]

ДАННЫЕ:
{data}"""


def collect_weekly_data(end_date: datetime) -> dict:
    all_calls = []
    all_analyses = []
    phones_map = load_phones_map()

    for i in range(7):
        d = end_date - timedelta(days=i)
        f = CALLS_DIR / f"{d.strftime('%Y-%m-%d')}.json"
        if f.exists():
            calls = json.loads(f.read_text())
            all_calls.extend(calls)
            analyses = load_analyses_for_date(d)
            all_analyses.extend(analyses.values())

    if not all_calls:
        return {}

    total = len(all_calls)
    answered = sum(1 for c in all_calls if c.get("sip_status") == "answer")
    missed = sum(1 for c in all_calls if c.get("sip_status") in ("no_answer", "cancel"))

    scores = [a.get("quality_score") for a in all_analyses if a.get("quality_score")]
    directions = Counter(a.get("direction") for a in all_analyses if a.get("direction"))
    outcomes = Counter(a.get("outcome") for a in all_analyses if a.get("outcome"))
    all_issues = [i for a in all_analyses for i in a.get("issues", [])]

    # Per-manager summary
    manager_stats = compute_stats(all_calls, {a.get("public_id"): a for a in all_analyses}, phones_map)
    mgr_summary = {}
    for mgr, s in manager_stats.items():
        sc = s["quality_scores"]
        mgr_summary[mgr] = {
            "total": s["total"],
            "answered": s["answered"],
            "missed": s["missed"],
            "avg_score": round(sum(sc) / len(sc), 2) if sc else None,
            "outcomes": dict(s["outcomes"]),
        }

    return {
        "period": f"{(end_date - timedelta(days=6)).strftime('%d.%m')}–{end_date.strftime('%d.%m.%Y')}",
        "total_calls": total,
        "answered": answered,
        "missed": missed,
        "answer_rate_pct": round(answered / total * 100) if total else 0,
        "analyzed_calls": len(all_analyses),
        "avg_quality": round(sum(scores) / len(scores), 2) if scores else None,
        "score_distribution": {str(i): scores.count(i) for i in range(1, 6)},
        "top_directions": dict(directions.most_common(10)),
        "outcomes": dict(outcomes),
        "top_issues": dict(Counter(all_issues).most_common(8)),
        "managers": mgr_summary,
    }


def build_ai_block(data: dict) -> str:
    claude_bin = shutil.which("claude") or "/home/user/.local/bin/claude"
    prompt = WEEKLY_AI_PROMPT.replace("{data}", json.dumps(data, ensure_ascii=False, indent=2))
    for attempt in range(3):
        try:
            proc = subprocess.run(
                [claude_bin, "--print", "--dangerously-skip-permissions", "-p", prompt],
                capture_output=True, text=True, timeout=300,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
            if attempt < 2:
                time.sleep((attempt + 1) * 15)
        except subprocess.TimeoutExpired:
            print(f"[run_weekly] AI timeout attempt {attempt+1}/3")
            if attempt < 2:
                time.sleep(15)
        except Exception as e:
            return f"[AI анализ: ошибка — {e}]"
    return "[AI анализ недоступен]"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD — конец периода (default: вчера)")
    args = parser.parse_args()

    end_date = (
        datetime.strptime(args.date, "%Y-%m-%d") if args.date
        else datetime.now() - timedelta(days=1)
    )
    start_date = end_date - timedelta(days=6)

    print(f"[run_weekly] Weekly report {start_date.strftime('%Y-%m-%d')} → {end_date.strftime('%Y-%m-%d')}")

    data = collect_weekly_data(end_date)
    if not data:
        print("[run_weekly] No data for this week")
        return

    # Шапка
    period = data["period"]
    header = (
        f"<b>📊 ЕЖЕНЕДЕЛЬНЫЙ ОТЧЁТ ЗВОНКОВ: {period}</b>\n\n"
        f"Всего звонков: {data['total_calls']}\n"
        f"Отвечено: {data['answered']} ({data['answer_rate_pct']}%)\n"
        f"Проанализировано: {data['analyzed_calls']}"
    )
    if data.get("avg_quality"):
        header += f"\nСр. качество вход. зв.: {data['avg_quality']}/5"

    # Блоки
    manager_block = get_weekly_manager_block(end_date)
    direction_block = get_weekly_direction_block(end_date)

    # Скоркард менеджеров + статистика пропущенных
    all_calls = []
    all_analyses = {}
    phones_map = load_phones_map()
    recovery_total = {"missed_inbound": 0, "recovered": 0, "truly_lost": 0, "outbound_unanswered": 0}
    for i in range(7):
        d = end_date - timedelta(days=i)
        f = CALLS_DIR / f"{d.strftime('%Y-%m-%d')}.json"
        if f.exists():
            day_calls = json.loads(f.read_text())
            all_calls.extend(day_calls)
            all_analyses.update(load_analyses_for_date(d))
            dr = compute_callback_recovery(day_calls)
            for k in recovery_total:
                recovery_total[k] += dr[k]

    weekly_stats = compute_stats(all_calls, all_analyses, phones_map)
    scorecard_block = format_manager_scorecard(weekly_stats, data["period"])

    # Блок пропущенных звонков
    r = recovery_total
    recovery_block = (
        f"<b>📵 ПРОПУЩЕННЫЕ ЗВОНКИ</b>\n"
        f"Входящих пропущено: {r['missed_inbound']}\n"
        f"  ✅ Перезвонили в тот же день: {r['recovered']}\n"
        f"  ❌ Не перезвонили (потеря): {r['truly_lost']}\n"
        f"Исходящих не дозвонились: {r['outbound_unanswered']} "
        f"(сервисные, в рейтинг не входят)"
    )

    print("[run_weekly] Requesting AI analysis...")
    ai_block = build_ai_block(data)

    full_report = "\n\n".join(filter(None, [header, recovery_block, scorecard_block, manager_block, direction_block, ai_block]))

    send_message(full_report, parse_mode="HTML")
    print("[run_weekly] Done!")


if __name__ == "__main__":
    main()
