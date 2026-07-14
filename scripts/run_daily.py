#!/usr/bin/env python3
"""
Ежедневная обработка SIPSIM: fetch CDR → транскрибация (Deepgram) → AI-анализ (Claude).

Отчёт РОПу НЕ строится и в Telegram НЕ отправляется — в Telegram остаётся только
недельный отчёт (run_weekly.py, Пн, с AI-аудитом). Дневная обработка малыми порциями
каждый день готовит данные (data/analysis/*.json), которые агрегирует недельный отчёт.

Запуск:
  python3 run_daily.py                   # вчера
  python3 run_daily.py --date 2026-05-10
  python3 run_daily.py --no-transcribe   # пропустить fetch+transcribe
  python3 run_daily.py --no-analyze      # пропустить AI-анализ
"""

import sys
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent))

from fetch_calls import fetch_calls_for_date, save_calls, load_phones
from transcribe import transcribe_date
from analyze_calls import analyze_date


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
    print(f"[run_daily] SIPSIM processing for {target_date.strftime('%Y-%m-%d')}")
    print(f"{'='*50}\n")

    # 1. Получить CDR + 2. Транскрибировать (Deepgram)
    if not args.no_transcribe:
        print("[run_daily] Step 1: Fetching calls...")
        load_phones()
        calls = fetch_calls_for_date(target_date)
        save_calls(target_date, calls)

        print("\n[run_daily] Step 2: Transcribing...")
        transcribe_date(target_date)
    else:
        print("[run_daily] Skipping fetch+transcribe (--no-transcribe)")

    # 3. AI-анализ каждого звонка (Claude) → data/analysis/*.json
    if not args.no_analyze:
        print("\n[run_daily] Step 3: Analyzing calls...")
        analyze_date(target_date)
    else:
        print("[run_daily] Skipping analysis (--no-analyze)")

    # Дневной отчёт РОПу НЕ строится и в Telegram НЕ шлётся — только обработка данных.
    # Отчёт с AI-аудитом отправляется еженедельно (run_weekly.py).
    print("\n[run_daily] Done! (обработка завершена; отчёт — в недельном run_weekly.py)")


if __name__ == "__main__":
    main()
