#!/usr/bin/env python3
"""
Транскрибирует записи звонков через Deepgram API (nova-2, ru).

Запуск:
  python3 transcribe.py                   # файл вчерашнего дня
  python3 transcribe.py --date 2026-05-10
  python3 transcribe.py --force           # перезаписать уже транскрибированные
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

DEEPGRAM_API_KEY = os.environ["DEEPGRAM_API_KEY"]
SIPSIM_TOKEN = os.environ["SIPSIM_TOKEN"]
DATA_DIR = Path(__file__).parent.parent / "data"
CALLS_DIR = DATA_DIR / "calls"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"

TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
DEEPGRAM_PARAMS = {
    "model": "nova-2",
    "language": "ru",
    "punctuate": "true",
    "diarize": "true",
    "smart_format": "true",
    "utterances": "true",
}


def transcribe_call(call: dict, force: bool = False) -> dict | None:
    public_id = call.get("public_id")
    audio_url = call.get("play_record_link", "")

    if not public_id or not audio_url:
        return None

    out_file = TRANSCRIPTS_DIR / f"{public_id}.json"
    if out_file.exists() and not force:
        return json.loads(out_file.read_text())

    # Добавляем токен к URL, если нужна авторизация
    if "token=" not in audio_url:
        sep = "&" if "?" in audio_url else "?"
        audio_url = f"{audio_url}{sep}token={SIPSIM_TOKEN}"

    try:
        headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "application/json"}
        resp = requests.post(
            DEEPGRAM_URL,
            headers=headers,
            params=DEEPGRAM_PARAMS,
            json={"url": audio_url},
            timeout=300,
        )
        resp.raise_for_status()
        data = resp.json()

        channels = data.get("results", {}).get("channels", [])
        alternatives = channels[0].get("alternatives", []) if channels else []
        transcript_text = alternatives[0].get("transcript", "") if alternatives else ""

        utterances = []
        for u in data.get("results", {}).get("utterances", []):
            utterances.append({
                "speaker": u.get("speaker", 0),
                "start": round(u.get("start", 0), 2),
                "end": round(u.get("end", 0), 2),
                "text": u.get("transcript", ""),
            })

        result = {
            "public_id": public_id,
            "call_type": call.get("call_type"),
            "sip_status": call.get("sip_status"),
            "duration_sec": call.get("duration_sec"),
            "wait_duration_sec": call.get("wait_duration_sec"),
            "start_time": call.get("start_time"),
            "answered_time": call.get("answered_time"),
            "end_time": call.get("end_time"),
            "manager_number": call.get("caller_number"),
            "client_number": call.get("target_number") if call.get("call_type") == "outbound" else call.get("caller_number"),
            "transcript": transcript_text,
            "utterances": utterances,
        }

        out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    except Exception as e:
        print(f"[transcribe] ERROR {public_id}: {e}")
        return None


def transcribe_date(date: datetime, force: bool = False) -> list[dict]:
    calls_file = CALLS_DIR / f"{date.strftime('%Y-%m-%d')}.json"
    if not calls_file.exists():
        print(f"[transcribe] No calls file for {date.strftime('%Y-%m-%d')}, run fetch_calls.py first")
        sys.exit(1)

    calls = json.loads(calls_file.read_text())
    to_transcribe = [c for c in calls if c.get("sip_status") == "answer" and c.get("play_record_link")]
    print(f"[transcribe] {len(to_transcribe)} answered calls with recordings out of {len(calls)} total")

    results = []
    for i, call in enumerate(to_transcribe, 1):
        pid = call.get("public_id", "?")
        dur = call.get("duration_sec", 0)
        print(f"[transcribe] [{i}/{len(to_transcribe)}] {pid} ({dur}s)...", end=" ", flush=True)
        result = transcribe_call(call, force=force)
        if result:
            print("ok")
            results.append(result)
        else:
            print("skip")

    print(f"[transcribe] Done: {len(results)} transcribed")
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD (default: yesterday)")
    parser.add_argument("--force", action="store_true", help="Re-transcribe existing")
    args = parser.parse_args()

    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d")
    else:
        target_date = datetime.now() - timedelta(days=1)

    transcribe_date(target_date, force=args.force)


if __name__ == "__main__":
    main()
