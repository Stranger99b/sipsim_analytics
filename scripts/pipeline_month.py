#!/usr/bin/env python3
"""
Пакетная обработка звонков за период: fetch → transcribe → analyze.
Не отправляет отчёты в Telegram — только подготавливает данные.

Запуск:
  python3 pipeline_month.py --month 2026-04
  python3 pipeline_month.py --start 2026-04-01 --end 2026-04-30
  python3 pipeline_month.py --month 2026-04 --skip-fetch   # если CDR уже скачаны
"""

import sys
import json
import argparse
from datetime import datetime, timedelta
from calendar import monthrange
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from fetch_calls import fetch_calls_for_date, save_calls, load_phones
from transcribe import transcribe_date
from analyze_calls import analyze_date


def get_dates(start: datetime, end: datetime) -> list:
    dates = []
    d = start
    while d <= end:
        dates.append(d)
        d += timedelta(days=1)
    return dates


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", help="YYYY-MM")
    parser.add_argument("--start", help="YYYY-MM-DD")
    parser.add_argument("--end", help="YYYY-MM-DD")
    parser.add_argument("--skip-fetch", action="store_true", help="Skip CDR fetch (already downloaded)")
    parser.add_argument("--skip-transcribe", action="store_true")
    parser.add_argument("--skip-analyze", action="store_true")
    args = parser.parse_args()

    if args.month:
        year, month = map(int, args.month.split("-"))
        _, days_in_month = monthrange(year, month)
        start = datetime(year, month, 1)
        end = datetime(year, month, days_in_month)
    elif args.start and args.end:
        start = datetime.strptime(args.start, "%Y-%m-%d")
        end = datetime.strptime(args.end, "%Y-%m-%d")
    else:
        print("Укажи --month YYYY-MM или --start/--end")
        sys.exit(1)

    dates = get_dates(start, end)
    print(f"\n{'='*60}")
    print(f"Pipeline: {start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')} ({len(dates)} дней)")
    print(f"{'='*60}\n")

    DATA_DIR = Path(__file__).parent.parent / "data"
    CALLS_DIR = DATA_DIR / "calls"

    # Step 1: Fetch CDR
    if not args.skip_fetch:
        print("=== STEP 1: FETCH CDR ===")
        load_phones()
        total_calls = 0
        for d in dates:
            print(f"\n[pipeline] Fetch {d.strftime('%Y-%m-%d')}...")
            calls = fetch_calls_for_date(d)
            save_calls(d, calls)
            total_calls += len(calls)
        print(f"\n[pipeline] Fetch done: {total_calls} calls across {len(dates)} days\n")
    else:
        print("=== STEP 1: SKIP FETCH ===\n")

    # Step 2: Transcribe
    if not args.skip_transcribe:
        print("=== STEP 2: TRANSCRIBE ===")
        total_tr = 0
        for d in dates:
            f = CALLS_DIR / f"{d.strftime('%Y-%m-%d')}.json"
            if not f.exists():
                print(f"[pipeline] No CDR for {d.strftime('%Y-%m-%d')}, skipping")
                continue
            calls = json.loads(f.read_text())
            answered = sum(1 for c in calls if c.get("sip_status") == "answer")
            if answered == 0:
                print(f"[pipeline] {d.strftime('%Y-%m-%d')}: no answered calls, skipping")
                continue
            print(f"\n[pipeline] Transcribe {d.strftime('%Y-%m-%d')} ({answered} calls)...")
            results = transcribe_date(d)
            total_tr += len(results)
        print(f"\n[pipeline] Transcribe done: {total_tr} transcripts\n")
    else:
        print("=== STEP 2: SKIP TRANSCRIBE ===\n")

    # Step 3: Analyze
    if not args.skip_analyze:
        print("=== STEP 3: ANALYZE ===")
        total_an = 0
        for d in dates:
            f = CALLS_DIR / f"{d.strftime('%Y-%m-%d')}.json"
            if not f.exists():
                continue
            calls = json.loads(f.read_text())
            answered = sum(1 for c in calls if c.get("sip_status") == "answer")
            if answered == 0:
                continue
            print(f"\n[pipeline] Analyze {d.strftime('%Y-%m-%d')} ({answered} calls)...")
            results = analyze_date(d)
            total_an += len(results)
        print(f"\n[pipeline] Analyze done: {total_an} analyses\n")
    else:
        print("=== STEP 3: SKIP ANALYZE ===\n")

    print(f"{'='*60}")
    print(f"Pipeline complete! Run: python3 run_monthly.py --month {start.strftime('%Y-%m')}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
