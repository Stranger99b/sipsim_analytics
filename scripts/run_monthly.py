#!/usr/bin/env python3
"""
Месячный отчёт SIPSIM Analytics для РОПа.

Запуск:
  python3 run_monthly.py --month 2026-04
  python3 run_monthly.py              # прошлый месяц
  python3 run_monthly.py --month 2026-04 --no-ai   # без AI-анализа
"""

import os
import sys
import json
import shutil
import subprocess
import time
import argparse
from datetime import datetime, timedelta
from calendar import monthrange
from pathlib import Path
from collections import Counter, defaultdict
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

from manager_stats import (
    format_manager_scorecard, format_manager_block,
    compute_callback_recovery, load_phones_map,
    compute_stats, load_analyses_for_date,
)
from direction_stats import format_direction_block, get_direction_stats
from export_excel import export_period_excel
from send_telegram import send_message, send_document

DATA_DIR = Path(__file__).parent.parent / "data"
CALLS_DIR = DATA_DIR / "calls"
ANALYSIS_DIR = DATA_DIR / "analysis"
REPORTS_DIR = DATA_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

MONTHS_RU = {
    1: "ЯНВАРЬ", 2: "ФЕВРАЛЬ", 3: "МАРТ", 4: "АПРЕЛЬ",
    5: "МАЙ", 6: "ИЮНЬ", 7: "ИЮЛЬ", 8: "АВГУСТ",
    9: "СЕНТЯБРЬ", 10: "ОКТЯБРЬ", 11: "НОЯБРЬ", 12: "ДЕКАБРЬ",
}

MONTHLY_AI_PROMPT = """Ты — бизнес-аналитик для РОПа (руководителя отдела продаж) туристической компании GoTrips.
Проанализируй данные по телефонным звонкам за ПОЛНЫЙ МЕСЯЦ.

Задача: помочь РОПу понять:
1. Кто из менеджеров продаёт хорошо, кому нужна помощь
2. Какие направления туров наиболее востребованы
3. Системные проблемы (пропущенные звонки, низкое качество, частые возражения)
4. Конкретный план на следующий месяц

Ответ строго на русском. Структура:

<b>ИТОГИ МЕСЯЦА</b>
[2-3 предложения: общий уровень, объём, тренд по качеству]

<b>МЕНЕДЖЕРЫ — ЛИДЕРЫ И ОТСТАЮЩИЕ</b>
[по каждому кто выделяется — конкретно: что хорошо, что нужно улучшить]

<b>НАПРАВЛЕНИЯ ЗА МЕСЯЦ</b>
[топ-3 направления, что это значит для стратегии продаж следующего месяца]

<b>СИСТЕМНЫЕ ПРОБЛЕМЫ</b>
[частые паттерны, типичные возражения, пропущенные звонки]

<b>ПЛАН НА СЛЕДУЮЩИЙ МЕСЯЦ</b>
[3-5 конкретных действий для РОПа с привязкой к данным]

ДАННЫЕ:
{data}"""


def get_month_dates(year: int, month: int) -> list:
    _, days_in_month = monthrange(year, month)
    return [datetime(year, month, d) for d in range(1, days_in_month + 1)]


def collect_monthly_calls_and_analyses(dates: list) -> tuple:
    """Returns (all_calls, all_analyses_dict)."""
    all_calls = []
    all_analyses = {}
    for d in dates:
        f = CALLS_DIR / f"{d.strftime('%Y-%m-%d')}.json"
        if f.exists():
            calls = json.loads(f.read_text())
            all_calls.extend(calls)
            all_analyses.update(load_analyses_for_date(d))
    return all_calls, all_analyses


def build_monthly_direction_block(dates: list, label: str) -> str:
    combined = defaultdict(lambda: {
        "calls": 0, "interested": 0, "booked": 0, "callback": 0,
        "other": 0, "avg_score": [],
    })
    for d in dates:
        for direction, s in get_direction_stats(d).items():
            c = combined[direction]
            c["calls"] += s["calls"]
            c["interested"] += s["interested"]
            c["booked"] += s["booked"]
            c["callback"] += s["callback"]
            c["other"] += s["other"]
            c["avg_score"].extend(s["avg_score"])
    return format_direction_block(dict(combined), label)


def build_monthly_agent_block(dates: list, phones_map: dict, label: str) -> str:
    SCORE_EMOJI = {1: "🔴", 2: "🟠", 3: "🟡", 4: "🟢", 5: "💚"}
    OUTCOME_RU = {
        "booked": "Бронь ✅", "interested": "Интерес", "callback": "Перезвон",
        "info_only": "Информация", "not_interested": "Отказ", "complaint": "Жалоба",
    }

    agent_calls = []
    for d in dates:
        f = CALLS_DIR / f"{d.strftime('%Y-%m-%d')}.json"
        if not f.exists():
            continue
        for call in json.loads(f.read_text()):
            pid = call.get("public_id")
            af = ANALYSIS_DIR / f"{pid}.json"
            if af and af.exists():
                a = json.loads(af.read_text())
                if a.get("is_agent"):
                    agent_calls.append((call, a))

    if not agent_calls:
        return ""

    total = len(agent_calls)
    answered = sum(1 for call, _ in agent_calls if call.get("sip_status") == "answer")
    scores = [a.get("quality_score") for _, a in agent_calls if a.get("quality_score")]
    avg_score = round(sum(scores) / len(scores), 1) if scores else None

    summary = f"Всего: {total}  |  Отвечено: {answered}"
    if avg_score:
        summary += f"  |  Ср. оценка: {avg_score}/5"

    lines = [f"<b>🤝 ЗВОНКИ АГЕНТОВ — {label}</b>", summary]

    for call, a in sorted(agent_calls, key=lambda x: x[0].get("start_time", "")):
        start_raw = call.get("start_time", "")
        try:
            start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            date_str = start_dt.strftime("%d.%m %H:%M")
        except Exception:
            date_str = start_raw[:16] if start_raw else "—"

        call_type = call.get("call_type", "")
        manager_num = call.get("caller_number", "") if call_type == "outbound" else (call.get("answered_phone_number") or "")
        manager_name = phones_map.get(manager_num, manager_num or "—")

        agent_name = a.get("agent_name", "Агент")
        agent_phone = call.get("target_number", "") if call_type == "outbound" else call.get("caller_number", "")
        agent_label = f"{agent_name} ({agent_phone})" if agent_phone else agent_name

        score = a.get("quality_score")
        score_emoji = SCORE_EMOJI.get(score, "⚪")
        outcome = OUTCOME_RU.get(a.get("outcome", ""), a.get("outcome", "") or "—")
        issues = ", ".join(a.get("issues", []))

        line = f"\n{score_emoji} {date_str} — {agent_label}  →  {manager_name}  |  {outcome}"
        if score:
            line += f"  |  {score}/5"
        lines.append(line)
        if issues:
            lines.append(f"   ⚠️ {issues}")

    return "\n".join(lines)


def collect_ai_data(dates: list, phones_map: dict) -> dict:
    """Prepare compact data dict for AI analysis."""
    all_calls = []
    all_analyses = []
    for d in dates:
        f = CALLS_DIR / f"{d.strftime('%Y-%m-%d')}.json"
        if not f.exists():
            continue
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

    monthly_stats = compute_stats(all_calls, {a.get("public_id"): a for a in all_analyses}, phones_map)
    mgr_summary = {}
    for mgr, s in monthly_stats.items():
        sc = s["quality_scores"]
        mgr_summary[mgr] = {
            "total": s["total"],
            "inbound": s["inbound"],
            "answered": s["answered"],
            "missed": s["missed"],
            "avg_score": round(sum(sc) / len(sc), 2) if sc else None,
            "outcomes": dict(s["outcomes"]),
        }

    return {
        "period": f"{dates[0].strftime('%d.%m')}–{dates[-1].strftime('%d.%m.%Y')}",
        "total_calls": total,
        "answered": answered,
        "missed": missed,
        "answer_rate_pct": round(answered / total * 100) if total else 0,
        "analyzed_calls": len(all_analyses),
        "avg_quality": round(sum(scores) / len(scores), 2) if scores else None,
        "score_distribution": {str(i): scores.count(i) for i in range(1, 6)},
        "top_directions": dict(directions.most_common(10)),
        "outcomes": dict(outcomes),
        "top_issues": dict(Counter(all_issues).most_common(10)),
        "managers": mgr_summary,
    }


def build_ai_block(data: dict) -> str:
    claude_bin = shutil.which("claude") or "/home/user/.local/bin/claude"
    prompt = MONTHLY_AI_PROMPT.replace("{data}", json.dumps(data, ensure_ascii=False, indent=2))
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
            print(f"[run_monthly] AI timeout attempt {attempt+1}/3")
            if attempt < 2:
                time.sleep(15)
        except Exception as e:
            return f"[AI анализ: ошибка — {e}]"
    return "[AI анализ недоступен]"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", help="YYYY-MM (default: last month)")
    parser.add_argument("--start", help="YYYY-MM-DD — начало произвольного периода")
    parser.add_argument("--end", help="YYYY-MM-DD — конец периода")
    parser.add_argument("--no-ai", action="store_true", help="Skip AI analysis block")
    args = parser.parse_args()

    if args.start and args.end:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d")
        end_dt = datetime.strptime(args.end, "%Y-%m-%d")
        all_dates = []
        d = start_dt
        while d <= end_dt:
            all_dates.append(d)
            d += timedelta(days=1)
        label = f"{start_dt.strftime('%d.%m')}–{end_dt.strftime('%d.%m.%Y')}"
    elif args.month:
        year, month = map(int, args.month.split("-"))
        all_dates = get_month_dates(year, month)
        label = f"{MONTHS_RU[month]} {year}"
    else:
        today = datetime.now()
        month = today.month - 1 if today.month > 1 else 12
        year = today.year if today.month > 1 else today.year - 1
        all_dates = get_month_dates(year, month)
        label = f"{MONTHS_RU[month]} {year}"

    available_dates = [d for d in all_dates if (CALLS_DIR / f"{d.strftime('%Y-%m-%d')}.json").exists()]

    if not available_dates:
        print(f"[run_monthly] No data for the specified period")
        return

    print(f"[run_monthly] Period report: {label} ({len(available_dates)} days with data)")

    phones_map = load_phones_map()

    # Aggregate all data
    all_calls, all_analyses = collect_monthly_calls_and_analyses(available_dates)
    if not all_calls:
        print("[run_monthly] No calls found")
        return

    total = len(all_calls)
    answered = sum(1 for c in all_calls if c.get("sip_status") == "answer")
    analyzed = len(all_analyses)
    scores = [a.get("quality_score") for a in all_analyses.values() if a.get("quality_score")]

    # Header
    header = (
        f"<b>📊 МЕСЯЧНЫЙ ОТЧЁТ ЗВОНКОВ: {label}</b>\n"
        f"Период: {available_dates[0].strftime('%d.%m')}–{available_dates[-1].strftime('%d.%m.%Y')} "
        f"({len(available_dates)} дн.)\n\n"
        f"Всего звонков: {total}\n"
        f"Отвечено: {answered} ({round(answered/total*100) if total else 0}%)\n"
        f"Проанализировано AI: {analyzed}"
    )
    if scores:
        header += f"\nСр. качество: {round(sum(scores)/len(scores), 2)}/5"

    # Recovery stats
    recovery_total = {"missed_inbound": 0, "recovered": 0, "truly_lost": 0, "outbound_unanswered": 0}
    for d in available_dates:
        f = CALLS_DIR / f"{d.strftime('%Y-%m-%d')}.json"
        if f.exists():
            dr = compute_callback_recovery(json.loads(f.read_text()))
            for k in recovery_total:
                recovery_total[k] += dr[k]

    r = recovery_total
    recovery_block = (
        f"<b>📵 ПРОПУЩЕННЫЕ ЗВОНКИ ЗА МЕСЯЦ</b>\n"
        f"Входящих пропущено: {r['missed_inbound']}\n"
        f"  ✅ Перезвонили в тот же день: {r['recovered']}\n"
        f"  ❌ Не перезвонили (потеря): {r['truly_lost']}\n"
        f"Исходящих не дозвонились: {r['outbound_unanswered']} (сервисные)"
    )

    # Agent block
    agent_block = build_monthly_agent_block(available_dates, phones_map, label)

    # Manager scorecard (100-point rating for the month)
    monthly_stats = compute_stats(all_calls, all_analyses, phones_map)
    scorecard_block = format_manager_scorecard(monthly_stats, label)

    # Manager detailed block
    manager_block = format_manager_block(monthly_stats, label)

    # Direction block
    direction_block = build_monthly_direction_block(available_dates, label)

    # AI analysis
    ai_block = ""
    if not args.no_ai:
        print("[run_monthly] Requesting AI analysis...")
        ai_data = collect_ai_data(available_dates, phones_map)
        ai_block = build_ai_block(ai_data)

    full_report = "\n\n".join(filter(None, [
        header, recovery_block, agent_block,
        scorecard_block, manager_block, direction_block, ai_block,
    ]))

    print("[run_monthly] Sending report to Telegram...")
    send_message(full_report, parse_mode="HTML")

    # Monthly Excel
    print("[run_monthly] Exporting monthly Excel (all calls)...")
    safe_label = label.replace(" ", "_").replace("–", "-").replace(".", "")
    out_path = REPORTS_DIR / f"calls_{safe_label}.xlsx"
    excel_path = export_period_excel(available_dates, phones_map, label, out_path)
    if excel_path:
        print(f"[run_monthly] Excel saved: {excel_path}")
        send_document(
            str(excel_path),
            caption=f"📋 Все звонки {label} — полная таблица с транскриптами",
        )

    print("[run_monthly] Done!")


if __name__ == "__main__":
    main()
