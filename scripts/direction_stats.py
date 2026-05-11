#!/usr/bin/env python3
"""
Статистика спроса по направлениям туров из звонков.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path(__file__).parent.parent / "data"
CALLS_DIR = DATA_DIR / "calls"
ANALYSIS_DIR = DATA_DIR / "analysis"


def get_direction_stats(date: datetime) -> dict:
    calls_file = CALLS_DIR / f"{date.strftime('%Y-%m-%d')}.json"
    if not calls_file.exists():
        return {}

    calls = json.loads(calls_file.read_text())
    stats = defaultdict(lambda: {
        "calls": 0,
        "interested": 0,
        "booked": 0,
        "callback": 0,
        "other": 0,
        "avg_score": [],
    })

    for call in calls:
        pid = call.get("public_id")
        f = ANALYSIS_DIR / f"{pid}.json"
        if not f or not f.exists():
            continue
        a = json.loads(f.read_text())
        direction = a.get("direction") or "Не определено"
        s = stats[direction]
        s["calls"] += 1
        outcome = a.get("outcome", "")
        if outcome == "interested":
            s["interested"] += 1
        elif outcome == "booked":
            s["booked"] += 1
        elif outcome == "callback":
            s["callback"] += 1
        else:
            s["other"] += 1
        score = a.get("quality_score")
        if score:
            s["avg_score"].append(score)

    return dict(stats)


def get_weekly_direction_stats(end_date: datetime) -> dict:
    combined = defaultdict(lambda: {
        "calls": 0,
        "interested": 0,
        "booked": 0,
        "callback": 0,
        "other": 0,
        "avg_score": [],
    })
    for i in range(7):
        d = end_date - timedelta(days=i)
        day_stats = get_direction_stats(d)
        for direction, s in day_stats.items():
            c = combined[direction]
            c["calls"] += s["calls"]
            c["interested"] += s["interested"]
            c["booked"] += s["booked"]
            c["callback"] += s["callback"]
            c["other"] += s["other"]
            c["avg_score"].extend(s["avg_score"])
    return dict(combined)


def format_direction_block(stats: dict, label: str) -> str:
    if not stats:
        return ""

    lines = [f"<b>🗺 НАПРАВЛЕНИЯ — {label}</b>"]

    # Убираем "Не определено" в конец
    sorted_dirs = sorted(
        stats.items(),
        key=lambda x: (x[0] == "Не определено", -x[1]["calls"]),
    )

    for direction, s in sorted_dirs:
        total = s["calls"]
        if total == 0:
            continue

        conversion = s["interested"] + s["booked"] + s["callback"]
        conv_pct = round(conversion / total * 100) if total else 0
        avg_s = (
            round(sum(s["avg_score"]) / len(s["avg_score"]), 1)
            if s["avg_score"] else None
        )

        parts = [f"обращений: {total}"]
        if s["booked"]:
            parts.append(f"бронь: {s['booked']}")
        if s["interested"]:
            parts.append(f"интерес: {s['interested']}")
        if s["callback"]:
            parts.append(f"перезвон: {s['callback']}")
        if avg_s:
            parts.append(f"оценка: {avg_s}")

        lines.append(f"  <b>{direction}</b> — {', '.join(parts)}")

    return "\n".join(lines)


def get_daily_direction_block(date: datetime) -> str:
    stats = get_direction_stats(date)
    return format_direction_block(stats, date.strftime("%d.%m.%Y"))


def get_weekly_direction_block(end_date: datetime) -> str:
    stats = get_weekly_direction_stats(end_date)
    start = end_date - timedelta(days=6)
    label = f"{start.strftime('%d.%m')}–{end_date.strftime('%d.%m.%Y')}"
    return format_direction_block(stats, label)
