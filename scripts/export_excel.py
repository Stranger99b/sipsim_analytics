#!/usr/bin/env python3
"""
Экспорт проблемных звонков в Excel с полным текстом транскрипта.
"""

import json
from datetime import datetime
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import (
    Font, PatternFill, Alignment, Border, Side, GradientFill
)
from openpyxl.utils import get_column_letter

DATA_DIR = Path(__file__).parent.parent / "data"
CALLS_DIR = DATA_DIR / "calls"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
ANALYSIS_DIR = DATA_DIR / "analysis"
REPORTS_DIR = DATA_DIR / "reports"

REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# Цветовая схема
COLOR_RED = "FFCCCC"       # score 1-2
COLOR_YELLOW = "FFF3CC"    # score 3
COLOR_GREEN = "CCFFCC"     # score 4-5
COLOR_HEADER = "2E4057"    # тёмно-синий заголовок
COLOR_HEADER_FONT = "FFFFFF"
COLOR_SUBHEADER = "D9E8F5" # светло-голубой подзаголовок
COLOR_BORDER = "CCCCCC"

SCORE_COLORS = {
    1: "FF4444",
    2: "FF8C00",
    3: "FFD700",
    4: "5DBB5D",
    5: "2E8B57",
}

OUTCOME_RU = {
    "booked": "Бронь",
    "interested": "Интерес",
    "callback": "Перезвон",
    "info_only": "Информация",
    "not_interested": "Отказ",
    "complaint": "Жалоба",
    "wrong_number": "Ошибка",
    "unknown": "—",
    "no_transcript": "Нет транскрипта",
}


def _thin_border():
    side = Side(style="thin", color=COLOR_BORDER)
    return Border(left=side, right=side, top=side, bottom=side)


def _header_fill():
    return PatternFill("solid", fgColor=COLOR_HEADER)


def _score_fill(score):
    if score is None:
        return PatternFill("solid", fgColor="F0F0F0")
    color = SCORE_COLORS.get(int(score), "FFFFFF")
    return PatternFill("solid", fgColor=color)


def _row_fill(score):
    if score is None:
        return None
    if score <= 2:
        return PatternFill("solid", fgColor=COLOR_RED)
    if score == 3:
        return PatternFill("solid", fgColor=COLOR_YELLOW)
    return PatternFill("solid", fgColor=COLOR_GREEN)


def build_transcript_text(transcript_data: dict) -> str:
    """Форматирует транскрипт с диаризацией в читаемый текст."""
    utterances = transcript_data.get("utterances", [])
    if utterances:
        lines = []
        for u in utterances:
            speaker_num = u.get("speaker", 0)
            speaker_label = "Менеджер" if speaker_num == 0 else "Клиент"
            start = u.get("start", 0)
            minutes = int(start) // 60
            seconds = int(start) % 60
            time_str = f"{minutes}:{seconds:02d}"
            lines.append(f"[{time_str}] {speaker_label}: {u.get('text', '')}")
        return "\n".join(lines)
    return transcript_data.get("transcript", "")


def export_problem_calls_excel(
    date: datetime,
    phones_map: dict,
    min_score_threshold: int = 3,
    out_path: Path = None,
) -> Path | None:
    """
    Создаёт Excel со всеми проанализированными звонками, сортируя проблемные наверх.
    min_score_threshold: звонки с оценкой <= этого значения считаются проблемными.
    """
    calls_file = CALLS_DIR / f"{date.strftime('%Y-%m-%d')}.json"
    if not calls_file.exists():
        return None

    calls = json.loads(calls_file.read_text())
    calls_by_id = {c.get("public_id"): c for c in calls}

    # Собираем все звонки с анализом
    rows = []
    for call in calls:
        pid = call.get("public_id")
        analysis_file = ANALYSIS_DIR / f"{pid}.json"
        transcript_file = TRANSCRIPTS_DIR / f"{pid}.json"

        analysis = json.loads(analysis_file.read_text()) if analysis_file.exists() else {}
        transcript_data = json.loads(transcript_file.read_text()) if transcript_file.exists() else {}

        # Определяем менеджера
        call_type = call.get("call_type", "")
        if call_type == "outbound":
            manager_num = call.get("caller_number", "")
        else:
            manager_num = call.get("answered_phone_number") or call.get("caller_number", "")
        manager_name = phones_map.get(manager_num, manager_num or "—")

        # Клиент
        if call_type == "outbound":
            client_num = call.get("target_number", "")
        else:
            client_num = call.get("caller_number", "")

        # Время
        start_raw = call.get("start_time") or call.get("registration_time") or ""
        try:
            start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            time_str = start_dt.strftime("%H:%M:%S")
        except Exception:
            time_str = start_raw[:19] if start_raw else "—"

        dur = call.get("duration_sec", 0) or 0
        dur_str = f"{dur // 60}:{dur % 60:02d}"

        wait = call.get("wait_duration_sec", 0) or 0

        score = analysis.get("quality_score")
        outcome = OUTCOME_RU.get(analysis.get("outcome", ""), "—")
        direction = analysis.get("direction") or "—"
        issues = ", ".join(analysis.get("issues", [])) or "—"
        objections = ", ".join(analysis.get("objections", [])) or "—"
        highlights = analysis.get("manager_highlights", "—")
        transcript_text = build_transcript_text(transcript_data) if transcript_data else "Нет записи"

        sip_status = call.get("sip_status", "")
        status_ru = {
            "answer": "Отвечен",
            "no_answer": "Не отвечен",
            "cancel": "Отменён",
            "busy": "Занято",
        }.get(sip_status, sip_status)

        rows.append({
            "time": time_str,
            "manager": manager_name,
            "call_type": "Входящий" if call_type == "inbound" else "Исходящий",
            "status": status_ru,
            "client": client_num,
            "duration": dur_str,
            "wait": f"{wait}с",
            "score": score,
            "outcome": outcome,
            "direction": direction,
            "issues": issues,
            "objections": objections,
            "highlights": highlights,
            "transcript": transcript_text,
            "_score_num": score or 99,
        })

    if not rows:
        return None

    # Сортировка: проблемные сначала (score 1→5→None), потом по времени
    rows.sort(key=lambda r: (r["_score_num"], r["time"]))

    # Создаём Excel
    wb = Workbook()
    ws = wb.active
    ws.title = f"Звонки {date.strftime('%d.%m.%Y')}"

    # --- Заголовок файла ---
    ws.merge_cells("A1:N1")
    title_cell = ws["A1"]
    title_cell.value = f"📞 Аналитика звонков GoTrips — {date.strftime('%d.%m.%Y')}"
    title_cell.font = Font(name="Calibri", size=14, bold=True, color=COLOR_HEADER_FONT)
    title_cell.fill = _header_fill()
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 30

    # Статистика в строке 2
    total = len(rows)
    answered = sum(1 for r in rows if r["status"] == "Отвечен")
    problematic = sum(1 for r in rows if r["_score_num"] <= min_score_threshold)
    ws.merge_cells("A2:N2")
    stats_cell = ws["A2"]
    stats_cell.value = (
        f"Всего: {total}  |  Отвечено: {answered}  |  "
        f"Проблемных (оценка ≤{min_score_threshold}): {problematic}  |  "
        f"Для РОПа: расшифровка каждого звонка в столбце N"
    )
    stats_cell.font = Font(name="Calibri", size=10, italic=True, color="555555")
    stats_cell.fill = PatternFill("solid", fgColor=COLOR_SUBHEADER)
    stats_cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
    ws.row_dimensions[2].height = 20

    # --- Шапка таблицы ---
    headers = [
        "Время", "Менеджер", "Тип", "Статус", "Клиент",
        "Длит.", "Ожидание", "Оценка AI", "Исход", "Направление",
        "Проблемы", "Возражения клиента", "Оценка менеджера (AI)", "Транскрипт"
    ]
    col_widths = [10, 22, 12, 12, 18, 9, 10, 10, 14, 20, 35, 35, 45, 80]

    for col_idx, (h, w) in enumerate(zip(headers, col_widths), start=1):
        cell = ws.cell(row=3, column=col_idx, value=h)
        cell.font = Font(name="Calibri", size=10, bold=True, color=COLOR_HEADER_FONT)
        cell.fill = _header_fill()
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = _thin_border()
        ws.column_dimensions[get_column_letter(col_idx)].width = w

    ws.row_dimensions[3].height = 22
    ws.freeze_panes = "A4"

    # --- Строки данных ---
    for row_idx, r in enumerate(rows, start=4):
        score = r["score"]
        row_fill = _row_fill(score)
        is_problem = score is not None and score <= min_score_threshold

        values = [
            r["time"], r["manager"], r["call_type"], r["status"], r["client"],
            r["duration"], r["wait"], score if score else "—",
            r["outcome"], r["direction"],
            r["issues"], r["objections"], r["highlights"], r["transcript"],
        ]

        for col_idx, val in enumerate(values, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.font = Font(name="Calibri", size=9)
            cell.border = _thin_border()
            cell.alignment = Alignment(
                vertical="top",
                wrap_text=True,
                horizontal="center" if col_idx <= 8 else "left",
            )

            # Оценка — своя заливка
            if col_idx == 8 and score is not None:
                cell.fill = _score_fill(score)
                cell.font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
            elif row_fill and is_problem:
                cell.fill = row_fill

        # Высота строки: зависит от длины транскрипта
        transcript_lines = r["transcript"].count("\n") + 1
        row_height = min(max(transcript_lines * 13, 30), 400)
        ws.row_dimensions[row_idx].height = row_height

    # Условный раздел — разделитель между проблемными и нормальными
    problem_end = sum(1 for r in rows if r["_score_num"] <= min_score_threshold)
    if 0 < problem_end < len(rows):
        sep_row = 4 + problem_end
        for col_idx in range(1, 15):
            ws.cell(row=sep_row, column=col_idx).border = Border(
                top=Side(style="medium", color="FF4444"),
                left=_thin_border().left,
                right=_thin_border().right,
                bottom=_thin_border().bottom,
            )

    # Автофильтр
    ws.auto_filter.ref = f"A3:N{3 + len(rows)}"

    # Сохраняем
    if out_path is None:
        out_path = REPORTS_DIR / f"calls_{date.strftime('%Y-%m-%d')}.xlsx"

    wb.save(str(out_path))
    return out_path
