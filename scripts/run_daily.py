#!/usr/bin/env python3
"""
Ежедневный пайплайн SIPSIM аналитики.

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
from manager_stats import get_daily_manager_block
from direction_stats import get_daily_direction_block
from send_telegram import send_message

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

    # 5. Отправить в Telegram
    print("\n[run_daily] Step 5: Sending to Telegram...")
    send_message(full_report, parse_mode="HTML")
    print("\n[run_daily] Done!")


if __name__ == "__main__":
    main()
