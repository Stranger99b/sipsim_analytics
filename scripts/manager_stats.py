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


MANAGERS_CONFIG_FILE = DATA_DIR / "managers.json"


def load_phones_map() -> dict:
    """
    number (+375...) → имя менеджера.
    Берёт из managers.json (пользовательская настройка) или генерирует из phones_cache.json.
    """
    # Сначала пользовательский конфиг
    if MANAGERS_CONFIG_FILE.exists():
        return json.loads(MANAGERS_CONFIG_FILE.read_text())

    # Иначе: internal_number → number
    if not PHONES_FILE.exists():
        return {}
    phones_list = json.loads(PHONES_FILE.read_text())
    mapping = {}
    for p in phones_list:
        num = p.get("number", "")
        internal = p.get("internal_number", "")
        phone_type = p.get("type", "regular")
        if num and phone_type == "regular":
            mapping[num] = f"Менеджер {internal}"
    return mapping


def get_manager_name(call: dict, phones_map: dict) -> str:
    """Определяет менеджера: для исходящих — caller_number, для входящих — answered_phone_number."""
    call_type = call.get("call_type", "")
    if call_type == "outbound":
        num = call.get("caller_number", "")
    else:
        num = call.get("answered_phone_number") or call.get("caller_number", "")
    return phones_map.get(num, num or "Неизвестный")


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


PRODUCTIVE_OUTCOMES = {"booked", "interested"}

SCORE_MEDALS = ["🥇", "🥈", "🥉"]


def compute_manager_score(s: dict) -> dict:
    """100-балльный скоринг: качество(60) + ответы(15) + ожидание(15) + длительность(10)."""
    # 1. Качество AI (60)
    if s["quality_scores"]:
        avg_q = sum(s["quality_scores"]) / len(s["quality_scores"])
        q_pts = round(avg_q / 5 * 60)
    else:
        avg_q = None
        q_pts = 0

    # 2. Доступность — answer rate (15)
    total = s["total"]
    answered = s["answered"]
    a_pts = round((answered / total) * 15) if total else 0

    # 3. Время ожидания — меньше лучше (15)
    if s["wait_times"]:
        avg_wait = sum(s["wait_times"]) / len(s["wait_times"])
        if avg_wait <= 15:
            w_pts = 15
        elif avg_wait <= 25:
            w_pts = 12
        elif avg_wait <= 35:
            w_pts = 9
        elif avg_wait <= 50:
            w_pts = 6
        else:
            w_pts = 3
    else:
        w_pts = 8  # нет данных — нейтрально

    # 4. Средняя длительность — 60-300с оптимально (10)
    if s["durations"]:
        avg_dur = sum(s["durations"]) / len(s["durations"])
        if 60 <= avg_dur <= 300:
            d_pts = 10
        elif avg_dur < 60:
            d_pts = 4
        elif avg_dur <= 480:
            d_pts = 7
        else:
            d_pts = 5
    else:
        avg_dur = None
        d_pts = 5

    total_score = q_pts + a_pts + w_pts + d_pts
    return {
        "total": total_score,
        "q_pts": q_pts, "a_pts": a_pts, "w_pts": w_pts, "d_pts": d_pts,
        "avg_quality": avg_q,
        "avg_wait": (sum(s["wait_times"]) / len(s["wait_times"])) if s["wait_times"] else None,
        "avg_dur": (sum(s["durations"]) / len(s["durations"])) if s["durations"] else None,
    }


def _score_bar(score: int) -> str:
    filled = round(score / 10)
    return "█" * filled + "░" * (10 - filled)


def format_manager_scorecard(stats: dict, period_label: str) -> str:
    """Рейтинговая таблица менеджеров для еженедельного отчёта."""
    scored = []
    for manager, s in stats.items():
        sc = compute_manager_score(s)
        scored.append((manager, s, sc))
    scored.sort(key=lambda x: x[2]["total"], reverse=True)

    lines = [f"<b>🏆 РЕЙТИНГ МЕНЕДЖЕРОВ — {period_label}</b>"]

    for rank, (manager, s, sc) in enumerate(scored):
        medal = SCORE_MEDALS[rank] if rank < 3 else f"{rank + 1}."
        total = sc["total"]
        bar = _score_bar(total)

        avg_q = f"{sc['avg_quality']:.1f}/5" if sc["avg_quality"] else "—"
        avg_wait = f"{round(sc['avg_wait'])}с" if sc["avg_wait"] else "—"
        avg_dur_sec = sc["avg_dur"]
        avg_dur = f"{int(avg_dur_sec)//60}:{int(avg_dur_sec)%60:02d}" if avg_dur_sec else "—"
        answer_rate = round(s["answered"] / s["total"] * 100) if s["total"] else 0

        if total >= 80:
            level = "🟢"
        elif total >= 60:
            level = "🟡"
        else:
            level = "🔴"

        suffix = ""
        if rank == 0:
            suffix = " ← лидер"
        elif rank == len(scored) - 1 and len(scored) > 1:
            suffix = " ← требует внимания"

        lines.append(
            f"\n{medal} <b>{manager}</b> — {level} <b>{total}/100</b>{suffix}\n"
            f"   {bar}\n"
            f"   ⭐ Качество: {avg_q} ({sc['q_pts']}/60)  "
            f"📞 Ответы: {s['answered']}/{s['total']} {answer_rate}% ({sc['a_pts']}/15)\n"
            f"   ⏱ Ожидание: {avg_wait} ({sc['w_pts']}/15)  "
            f"🕐 Длит: {avg_dur} ({sc['d_pts']}/10)"
        )

        missed = s["missed"]
        if missed > 0:
            lines.append(f"   ⚠️ Пропущено: {missed} зв.")

    return "\n".join(lines)


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
