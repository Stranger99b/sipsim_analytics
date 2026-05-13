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


def get_manager_name(call: dict, phones_map: dict) -> str | None:
    """Определяет менеджера по номеру телефона из phones_map.
    Возвращает None если номер не найден в phones_map — такие звонки не учитываются в статистике менеджеров.
    Для входящих пропущенных (answered_phone_number пуст) вернёт None, т.к. неизвестно кто должен был ответить.
    """
    call_type = call.get("call_type", "")
    if call_type == "outbound":
        num = call.get("caller_number", "")
    else:
        num = call.get("answered_phone_number") or ""
    return phones_map.get(num)  # None если не менеджер


PRODUCTIVE_OUTBOUND = {"booked", "interested"}


def compute_callback_recovery(calls: list) -> dict:
    """Анализирует пропущенные входящие и восстановленные перезвоном в тот же день."""
    outbound_answered_targets = set()
    for c in calls:
        if c.get("call_type") == "outbound" and c.get("sip_status") == "answer":
            t = c.get("target_number") or c.get("answered_phone_number")
            if t:
                outbound_answered_targets.add(t)

    missed_inbound = recovered = truly_lost = outbound_unanswered = 0
    for c in calls:
        status = c.get("sip_status")
        if status not in ("no_answer", "cancel"):
            continue
        if c.get("call_type") == "inbound":
            missed_inbound += 1
            if c.get("caller_number") in outbound_answered_targets:
                recovered += 1
            else:
                truly_lost += 1
        else:
            outbound_unanswered += 1

    return {
        "missed_inbound": missed_inbound,
        "recovered": recovered,
        "truly_lost": truly_lost,
        "outbound_unanswered": outbound_unanswered,
    }


def compute_stats(calls: list, analyses: dict, phones_map: dict) -> dict:
    """
    analyses: {public_id: analysis_dict}
    Возвращает {manager_name: stats_dict}
    Рейтинг строится только по входящим звонкам.
    Исходящие с продуктивным исходом (booked/interested) учитываются в качестве.
    """
    stats = defaultdict(lambda: {
        # Общие счётчики (для информации)
        "total": 0,
        "answered": 0,
        "missed": 0,
        "busy": 0,
        "inbound": 0,
        "outbound": 0,
        "durations": [],
        "outcomes": defaultdict(int),
        "issues": [],
        "directions": defaultdict(int),
        # Для рейтинга — только входящие
        "inbound_total": 0,
        "inbound_answered": 0,
        "inbound_wait_times": [],
        "inbound_quality_scores": [],
        # Исходящие с результатом (доп. вклад в качество)
        "outbound_productive_scores": [],
        # Совместимость
        "wait_times": [],
        "quality_scores": [],
    })

    for call in calls:
        manager = get_manager_name(call, phones_map)
        if not manager:
            continue
        s = stats[manager]
        s["total"] += 1

        status = call.get("sip_status", "")
        call_type = call.get("call_type", "")
        is_inbound = call_type == "inbound"

        if status == "answer":
            s["answered"] += 1
        elif status in ("no_answer", "cancel"):
            s["missed"] += 1
        elif status == "busy":
            s["busy"] += 1

        if is_inbound:
            s["inbound"] += 1
            s["inbound_total"] += 1
            if status == "answer":
                s["inbound_answered"] += 1
                wait = call.get("wait_duration_sec")
                if wait is not None:
                    s["inbound_wait_times"].append(int(wait))
                    s["wait_times"].append(int(wait))
        else:
            s["outbound"] += 1

        dur = call.get("duration_sec")
        if dur and status == "answer":
            s["durations"].append(int(dur))

        pid = call.get("public_id")
        if pid and pid in analyses:
            a = analyses[pid]
            score = a.get("quality_score")
            outcome = a.get("outcome")
            if outcome:
                s["outcomes"][outcome] += 1
            for issue in a.get("issues", []):
                s["issues"].append(issue)
            direction = a.get("direction")
            if direction:
                s["directions"][direction] += 1
            if score:
                s["quality_scores"].append(score)
                if is_inbound:
                    s["inbound_quality_scores"].append(score)
                elif outcome in PRODUCTIVE_OUTBOUND:
                    s["outbound_productive_scores"].append(score)

    return dict(stats)


PRODUCTIVE_OUTCOMES = {"booked", "interested"}

SCORE_MEDALS = ["🥇", "🥈", "🥉"]


def compute_manager_score(s: dict) -> dict:
    """100-балльный скоринг по входящим звонкам:
    качество(70) + ответы на входящие(15) + ожидание входящих(15).
    Исходящие сервисные звонки в рейтинг не входят.
    Исходящие с продуктивным исходом (booked/interested) добавляют балл в качество.
    """
    # 1. Качество AI (70): входящие + исходящие с результатом
    q_scores = s["inbound_quality_scores"] + s["outbound_productive_scores"]
    if q_scores:
        avg_q = sum(q_scores) / len(q_scores)
        q_pts = round(avg_q / 5 * 70)
    else:
        avg_q = None
        q_pts = 0

    # 2. Доступность — только входящие (15)
    inb_total = s["inbound_total"]
    inb_answered = s["inbound_answered"]
    a_pts = round((inb_answered / inb_total) * 15) if inb_total else 0

    # 3. Время ожидания — только входящие (15)
    wait_times = s["inbound_wait_times"]
    if wait_times:
        avg_wait = sum(wait_times) / len(wait_times)
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
        avg_wait = None
        w_pts = 8

    total_score = q_pts + a_pts + w_pts
    return {
        "total": total_score,
        "q_pts": q_pts, "a_pts": a_pts, "w_pts": w_pts,
        "avg_quality": avg_q,
        "avg_wait": avg_wait,
        "avg_dur": (sum(s["durations"]) / len(s["durations"])) if s["durations"] else None,
        "inbound_total": inb_total,
        "inbound_answered": inb_answered,
        "outbound_productive": len(s["outbound_productive_scores"]),
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
        inb_total = sc["inbound_total"]
        inb_ans = sc["inbound_answered"]
        inb_rate = round(inb_ans / inb_total * 100) if inb_total else 0
        outb_prod = sc["outbound_productive"]

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

        outb_str = f"  📤 Исх.продажи: {outb_prod}" if outb_prod else ""
        lines.append(
            f"\n{medal} <b>{manager}</b> — {level} <b>{total}/100</b>{suffix}\n"
            f"   {bar}\n"
            f"   ⭐ Качество вход.: {avg_q} ({sc['q_pts']}/70)  "
            f"📞 Вход.: {inb_ans}/{inb_total} {inb_rate}% ({sc['a_pts']}/15)\n"
            f"   ⏱ Ожидание: {avg_wait} ({sc['w_pts']}/15)  "
            f"🕐 Ср.длит: {avg_dur}{outb_str}"
        )

        inb_missed = inb_total - inb_ans
        if inb_missed > 0:
            lines.append(f"   ⚠️ Пропущено вход.: {inb_missed} зв.")

    lines.append(
        "\n<i>📐 Как считается рейтинг (только входящие звонки):\n"
        "⭐ Качество AI — 70 баллов: средняя оценка Claude (1–5) × 14\n"
        "📞 Ответы на входящие — 15 баллов: % отвеченных входящих\n"
        "⏱ Скорость ответа — 15 баллов: ≤15с=15, ≤25с=12, ≤35с=9, ≤50с=6, >50с=3\n"
        "📤 Исх.продажи — информационно (booked/interested), в балл не входят\n"
        "🟢 ≥80  🟡 60–79  🔴 &lt;60</i>"
    )

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
