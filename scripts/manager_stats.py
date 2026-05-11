#!/usr/bin/env python3
"""
Агрегирует статистику по менеджерам из CDR + анализа.

Возвращает форматированный блок для Telegram.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path(__file__).parent.parent / "data"
CALLS_DIR = DATA_DIR / "calls"
ANALYSIS_DIR = DATA_DIR / "analysis"
PHONES_FILE = DATA_DIR / "phones_cache.json"


def load_phones_map() -> dict:
    """phone_id → имя менеджера."""
    if not PHONES_FILE.exists():
        return {}
    data = json.loads(PHONES_FILE.read_text())
    mapping = {}
    phones_list = data if isinstance(data, list) else data.get("phones", data.get("data", []))
    for p in phones_list:
        pid = str(p.get("id") or p.get("phone_id") or "")
        name = p.get("name") or p.get("username") or p.get("display_name") or pid
        if pid:
            mapping[pid] = name
    return mapping


def get_manager_name(call: dict, phones_map: dict) -> str:
    pid = str(call.get("phone_id") or call.get("phone") or call.get("answerer") or "")
    return phones_map.get(pid, pid or "Неизвестный")


def compute_stats(calls: list, analyses: dict, phones_map: dict) -> dict:
    """
    analyses: {public_id: analysis_dict}
    Возвращает {manager_name: stats_dict}
    """
    stats = defaultdict(lambda: {
        "total": 0,
        "answered": 0,
        "missed": 0,
        "busy": 0,
        "inbound": 0,
        "outbound": 0,
        "wait_times": [],
        "durations": [],
        "quality_scores": [],
        "outcomes": defaultdict(int),
        "issues": [],
        "directions": defaultdict(int),
    })

    for call in calls:
        manager = get_manager_name(call, phones_map)
        s = stats[manager]
        s["total"] += 1

        status = call.get("sip_status", "")
        if status == "answer":
            s["answered"] += 1
        elif status in ("no_answer", "cancel"):
            s["missed"] += 1
        elif status == "busy":
            s["busy"] += 1

        if call.get("call_type") == "inbound":
            s["inbound"] += 1
        else:
            s["outbound"] += 1

        wait = call.get("wait_duration_sec")
        if wait is not None and status == "answer":
            s["wait_times"].append(int(wait))

        dur = call.get("duration_sec")
        if dur and status == "answer":
            s["durations"].append(int(dur))

        pid = call.get("public_id")
        if pid and pid in analyses:
            a = analyses[pid]
            score = a.get("quality_score")
            if score:
                s["quality_scores"].append(score)
            outcome = a.get("outcome")
            if outcome:
                s["outcomes"][outcome] += 1
            for issue in a.get("issues", []):
                s["issues"].append(issue)
            direction = a.get("direction")
            if direction:
                s["directions"][direction] += 1

    return dict(stats)


def format_manager_block(stats: dict, date_label: str) -> str:
    lines = [f"<b>👥 МЕНЕДЖЕРЫ — {date_label}</b>"]

    sorted_managers = sorted(
        stats.items(),
        key=lambda x: x[1]["answered"],
        reverse=True,
    )

    for manager, s in sorted_managers:
        total = s["total"]
        answered = s["answered"]
        missed = s["missed"]
        answer_rate = round(answered / total * 100) if total else 0

        avg_wait = round(sum(s["wait_times"]) / len(s["wait_times"])) if s["wait_times"] else None
        avg_dur = round(sum(s["durations"]) / len(s["durations"])) if s["durations"] else None
        avg_score = (
            round(sum(s["quality_scores"]) / len(s["quality_scores"]), 1)
            if s["quality_scores"] else None
        )

        wait_str = f"{avg_wait}с" if avg_wait is not None else "—"
        dur_str = f"{avg_dur // 60}:{avg_dur % 60:02d}" if avg_dur is not None else "—"
        score_str = f"{avg_score}/5" if avg_score is not None else "—"

        score_emoji = ""
        if avg_score:
            if avg_score >= 4.5:
                score_emoji = "🟢"
            elif avg_score >= 3.5:
                score_emoji = "🟡"
            else:
                score_emoji = "🔴"

        lines.append(
            f"\n<b>{manager}</b>\n"
            f"  Звонков: {total} (отв: {answered}/{total}, {answer_rate}%)\n"
            f"  Ожидание: {wait_str} | Длит: {dur_str} | Оценка: {score_emoji}{score_str}"
        )

        if s["outcomes"]:
            top_outcomes = sorted(s["outcomes"].items(), key=lambda x: -x[1])[:3]
            outcomes_str = ", ".join(f"{k}: {v}" for k, v in top_outcomes)
            lines.append(f"  Исходы: {outcomes_str}")

        if missed > 0:
            lines.append(f"  ⚠️ Пропущено: {missed}")

        freq_issues = {}
        for issue in s["issues"]:
            freq_issues[issue] = freq_issues.get(issue, 0) + 1
        if freq_issues:
            top = sorted(freq_issues.items(), key=lambda x: -x[1])[:2]
            lines.append(f"  ⚡ Проблемы: {', '.join(i for i, _ in top)}")

    return "\n".join(lines)


def load_analyses_for_date(date: datetime) -> dict:
    """public_id → analysis"""
    calls_file = CALLS_DIR / f"{date.strftime('%Y-%m-%d')}.json"
    if not calls_file.exists():
        return {}
    calls = json.loads(calls_file.read_text())
    result = {}
    for call in calls:
        pid = call.get("public_id")
        f = ANALYSIS_DIR / f"{pid}.json"
        if f and f.exists():
            result[pid] = json.loads(f.read_text())
    return result


def get_daily_manager_block(date: datetime) -> str:
    calls_file = CALLS_DIR / f"{date.strftime('%Y-%m-%d')}.json"
    if not calls_file.exists():
        return ""
    calls = json.loads(calls_file.read_text())
    analyses = load_analyses_for_date(date)
    phones_map = load_phones_map()
    stats = compute_stats(calls, analyses, phones_map)
    return format_manager_block(stats, date.strftime("%d.%m.%Y"))


def get_weekly_manager_block(end_date: datetime) -> str:
    """Агрегат за 7 дней до end_date включительно."""
    all_calls = []
    all_analyses = {}
    phones_map = load_phones_map()

    for i in range(7):
        d = end_date - timedelta(days=i)
        f = CALLS_DIR / f"{d.strftime('%Y-%m-%d')}.json"
        if f.exists():
            calls = json.loads(f.read_text())
            all_calls.extend(calls)
            all_analyses.update(load_analyses_for_date(d))

    if not all_calls:
        return "Нет данных за неделю"

    start_date = end_date - timedelta(days=6)
    label = f"{start_date.strftime('%d.%m')}–{end_date.strftime('%d.%m.%Y')}"
    stats = compute_stats(all_calls, all_analyses, phones_map)
    return format_manager_block(stats, label)
