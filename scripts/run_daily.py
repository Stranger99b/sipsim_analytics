#!/usr/bin/env python3
"""
Ежедневный пайплайн SIPSIM аналитики.

Каждый день: fetch CDR → транскрибация (Deepgram) → AI-анализ (Claude) → отчёт + Excel.
Дневной отчёт РОПу в Telegram ОТКЛЮЧЁН (сохраняется локально в data/daily_reports/);
в Telegram остаётся только недельный отчёт (run_weekly.py, Пн). Ежедневная обработка
малыми порциями нужна, чтобы данные для недельного отчёта готовились заранее.

Запуск:
  python3 run_daily.py                   # вчера
  python3 run_daily.py --date 2026-05-10
  python3 run_daily.py --no-transcribe   # только отчёт из уже готовых транскриптов
"""

import os
import sys
import json
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

from fetch_calls import fetch_calls_for_date, save_calls, load_phones
from transcribe import transcribe_date
from analyze_calls import analyze_date
from manager_stats import get_daily_manager_block, load_phones_map
from direction_stats import get_daily_direction_block
from export_excel import export_problem_calls_excel
# send_telegram больше не используется в дневном прогоне: дневной отчёт в Telegram
# отключён (остаётся только недельный). Транскрибация/анализ считаются ежедневно.

DATA_DIR = Path(__file__).parent.parent / "data"
CALLS_DIR = DATA_DIR / "calls"
ANALYSIS_DIR = DATA_DIR / "analysis"


def build_summary_block(date: datetime) -> str:
    calls_file = CALLS_DIR / f"{date.strftime('%Y-%m-%d')}.json"
    if not calls_file.exists():
        return ""

    calls = json.loads(calls_file.read_text())
    total = len(calls)
    answered = sum(1 for c in calls if c.get("sip_status") == "answer")
    missed = sum(1 for c in calls if c.get("sip_status") in ("no_answer", "cancel"))
    busy = sum(1 for c in calls if c.get("sip_status") == "busy")
    inbound = sum(1 for c in calls if c.get("call_type") == "inbound")
    outbound = sum(1 for c in calls if c.get("call_type") == "outbound")
    answer_rate = round(answered / total * 100) if total else 0

    durations = [c.get("duration_sec", 0) for c in calls if c.get("sip_status") == "answer"]
    avg_dur = round(sum(durations) / len(durations)) if durations else 0
    avg_dur_str = f"{avg_dur // 60}:{avg_dur % 60:02d}" if avg_dur else "—"

    wait_times = [c.get("wait_duration_sec", 0) for c in calls if c.get("sip_status") == "answer" and c.get("wait_duration_sec")]
    avg_wait = round(sum(wait_times) / len(wait_times)) if wait_times else 0

    lines = [
        f"<b>📞 ЗВОНКИ GOTRIPS: {date.strftime('%d.%m.%Y')}</b>",
        "",
        f"Всего: {total}  |  Отвечено: {answered} ({answer_rate}%)",
        f"Входящих: {inbound}  |  Исходящих: {outbound}",
        f"Пропущено: {missed}  |  Занято: {busy}",
        f"Ср. длит: {avg_dur_str}  |  Ср. ожидание: {avg_wait}с",
    ]

    # Проблемные звонки из анализа
    bad_calls = []
    for call in calls:
        pid = call.get("public_id")
        f = ANALYSIS_DIR / f"{pid}.json"
        if f and f.exists():
            a = json.loads(f.read_text())
            if a.get("quality_score") and a["quality_score"] <= 2:
                bad_calls.append((call, a))

    if bad_calls:
        lines.append("")
        lines.append(f"<b>🔴 Проблемные звонки ({len(bad_calls)}):</b>")
        for call, a in bad_calls[:5]:
            dur = call.get("duration_sec", 0)
            highlight = a.get("manager_highlights", "")[:80]
            lines.append(f"  • {call.get('start_time', '')[:16]} — {highlight}")

    return "\n".join(lines)


_SCORE_EMOJI = {1: "🔴", 2: "🟠", 3: "🟡", 4: "🟢", 5: "💚"}
_OUTCOME_RU = {
    "booked": "Бронь ✅", "interested": "Интерес", "callback": "Перезвон",
    "info_only": "Информация", "not_interested": "Отказ", "complaint": "Жалоба",
    "wrong_number": "Ошибка номера", "client_unavailable": "Агент занят",
    "unknown": "—", "no_transcript": "Нет транскрипта",
}


def build_agent_calls_block(date: datetime) -> str:
    calls_file = CALLS_DIR / f"{date.strftime('%Y-%m-%d')}.json"
    if not calls_file.exists():
        return ""

    calls = json.loads(calls_file.read_text())
    phones_map = load_phones_map()

    agent_calls = []
    for call in calls:
        pid = call.get("public_id")
        af = ANALYSIS_DIR / f"{pid}.json"
        if af and af.exists():
            a = json.loads(af.read_text())
            if a.get("is_agent"):
                agent_calls.append((call, a))

    if not agent_calls:
        return ""

    lines = [f"<b>🤝 ЗВОНКИ АГЕНТОВ — {date.strftime('%d.%m.%Y')}</b>"]

    for call, a in agent_calls:
        start_raw = call.get("start_time", "")
        try:
            start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            time_str = start_dt.strftime("%H:%M")
        except Exception:
            time_str = start_raw[:16] if start_raw else "—"

        call_type = call.get("call_type", "")
        if call_type == "outbound":
            manager_num = call.get("caller_number", "")
            agent_phone = call.get("target_number", "")
        else:
            manager_num = call.get("answered_phone_number") or ""
            agent_phone = call.get("caller_number", "")
        manager_name = phones_map.get(manager_num, manager_num or "—")

        score = a.get("quality_score")
        score_emoji = _SCORE_EMOJI.get(score, "⚪")
        outcome = _OUTCOME_RU.get(a.get("outcome", ""), a.get("outcome", "") or "—")
        highlights = (a.get("manager_highlights") or "")[:120]
        issues = ", ".join(a.get("issues", []))
        agent_name = a.get("agent_name", "Агент")
        is_answered = call.get("sip_status") == "answer"
        agent_label = f"{agent_name} ({agent_phone})" if agent_phone else agent_name

        call_line = f"\n{score_emoji} <b>{time_str}</b> — {agent_label}  →  {manager_name}  |  {outcome}"
        if score:
            call_line += f"  |  Оценка: {score}/5"
        if not is_answered:
            call_line += "  |  ⚠️ Не отвечен"
        lines.append(call_line)
        if highlights:
            lines.append(f"   💬 {highlights}")
        if issues:
            lines.append(f"   ⚠️ {issues}")

    return "\n".join(lines)


def build_ai_audit_block(date: datetime) -> str:
    """AI-резюме дня через Claude."""
    import shutil
    import subprocess

    calls_file = CALLS_DIR / f"{date.strftime('%Y-%m-%d')}.json"
    if not calls_file.exists():
        return ""

    calls = json.loads(calls_file.read_text())
    analyses = []
    for call in calls:
        pid = call.get("public_id")
        f = ANALYSIS_DIR / f"{pid}.json"
        if f and f.exists():
            a = json.loads(f.read_text())
            a["_meta"] = {
                "duration_sec": call.get("duration_sec"),
                "call_type": call.get("call_type"),
                "sip_status": call.get("sip_status"),
            }
            analyses.append(a)

    if not analyses:
        return ""

    scores = [a["quality_score"] for a in analyses if a.get("quality_score")]
    directions = Counter(a["direction"] for a in analyses if a.get("direction"))
    outcomes = Counter(a["outcome"] for a in analyses if a.get("outcome"))
    all_issues = [i for a in analyses for i in a.get("issues", [])]

    summary = {
        "date": date.strftime("%Y-%m-%d"),
        "total_analyzed": len(analyses),
        "avg_quality": round(sum(scores) / len(scores), 2) if scores else None,
        "score_dist": {str(i): scores.count(i) for i in range(1, 6)},
        "top_directions": dict(directions.most_common(10)),
        "outcomes": dict(outcomes),
        "top_issues": dict(Counter(all_issues).most_common(5)),
    }

    prompt = f"""Ты — бизнес-аналитик для РОПа (руководителя отдела продаж) туристической компании GoTrips.
Проанализируй данные по звонкам за {date.strftime('%d.%m.%Y')} и дай краткий вывод.

Данные:
{json.dumps(summary, ensure_ascii=False, indent=2)}

Ответ строго на русском, 3-5 предложений. Формат:
<b>🤖 AI-ВЫВОД:</b>
[твой анализ — что хорошо, что плохо, 1-2 конкретных действия для РОПа]"""

    claude_bin = shutil.which("claude") or "/home/user/.local/bin/claude"
    try:
        proc = subprocess.run(
            [claude_bin, "--print", "--dangerously-skip-permissions", "-p", prompt],
            capture_output=True, text=True, timeout=120,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--no-transcribe", action="store_true", help="Skip fetch+transcribe")
    parser.add_argument("--no-analyze", action="store_true", help="Skip AI analysis")
    args = parser.parse_args()

    target_date = (
        datetime.strptime(args.date, "%Y-%m-%d") if args.date
        else datetime.now() - timedelta(days=1)
    )

    print(f"\n{'='*50}")
    print(f"[run_daily] SIPSIM pipeline for {target_date.strftime('%Y-%m-%d')}")
    print(f"{'='*50}\n")

    if not args.no_transcribe:
        # 1. Получить CDR
        print("[run_daily] Step 1: Fetching calls...")
        load_phones()
        calls = fetch_calls_for_date(target_date)
        save_calls(target_date, calls)

        # 2. Транскрибировать
        print("\n[run_daily] Step 2: Transcribing...")
        transcribe_date(target_date)
    else:
        print("[run_daily] Skipping fetch+transcribe (--no-transcribe)")

    if not args.no_analyze:
        # 3. Анализ
        print("\n[run_daily] Step 3: Analyzing calls...")
        analyze_date(target_date)
    else:
        print("[run_daily] Skipping analysis (--no-analyze)")

    # 4. Формировать отчёт
    print("\n[run_daily] Step 4: Building report...")
    blocks = []

    summary = build_summary_block(target_date)
    if summary:
        blocks.append(summary)

    agent_block = build_agent_calls_block(target_date)
    if agent_block:
        blocks.append(agent_block)

    manager_block = get_daily_manager_block(target_date)
    if manager_block:
        blocks.append(manager_block)

    direction_block = get_daily_direction_block(target_date)
    if direction_block:
        blocks.append(direction_block)

    ai_block = build_ai_audit_block(target_date)
    if ai_block:
        blocks.append(ai_block)

    if not blocks:
        print("[run_daily] No data to report")
        return

    full_report = "\n\n".join(blocks)

    # 5. Экспорт Excel
    print("\n[run_daily] Step 5: Exporting Excel...")
    phones_map = load_phones_map()
    excel_path = export_problem_calls_excel(target_date, phones_map)
    if excel_path:
        print(f"[run_daily] Excel saved: {excel_path}")

    # 6. Ежедневный отчёт РОПу в Telegram ОТКЛЮЧЁН — в Telegram остаётся только
    #    недельный отчёт. Данные (fetch → transcribe → analyze) обрабатываются
    #    каждый день малыми порциями и агрегируются в недельном отчёте. Сам дневной
    #    отчёт считаем и сохраняем локально, но НЕ отправляем.
    import re as _re
    print("\n[run_daily] Step 6: Saving daily report locally (Telegram disabled)...")
    reports_dir = DATA_DIR / "daily_reports"
    reports_dir.mkdir(exist_ok=True)
    report_path = reports_dir / f"{target_date.strftime('%Y-%m-%d')}.txt"
    report_path.write_text(_re.sub(r"<[^>]+>", "", full_report), encoding="utf-8")
    print(f"[run_daily] Report saved (not sent to Telegram): {report_path}")
    if excel_path:
        print(f"[run_daily] Excel saved (not sent): {excel_path}")
    print("\n[run_daily] Done!")


if __name__ == "__main__":
    main()
