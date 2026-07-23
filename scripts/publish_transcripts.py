#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Публикация полных транскрибаций звонков в Google Sheets (вместо ежедневных XLSX).

Одна строка на звонок текущего месяца: дата, время, менеджер, телефон, направление,
качество, исход, проблемы, AI-разбор (для косяков) и ПОЛНЫЙ транскрипт.
Сохраняет индекс public_id → строка в data/transcript_index.json — по нему лист
«Косяки звонков» в дашборде даёт прямую ссылку на конкретный звонок (контроль РОПом).

Таблица (создаёт пользователь, шарит на SA-Редактор):
  1cxUyebJDQEC1u5qI-Yqs0hR28hzVNkLpufeGhTKYbbQ
Креды — тот же service-account, что у Salebot-дашборда.

Запуск:
  python3 publish_transcripts.py                 # текущий месяц
  python3 publish_transcripts.py --month 2026-07
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from calendar import monthrange
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from manager_stats import load_phones_map, load_analyses_for_date, get_manager_name
from export_excel import build_transcript_text

DATA_DIR = Path(__file__).parent.parent / "data"
CALLS_DIR = DATA_DIR / "calls"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
DEEP_DIR = DATA_DIR / "deep"
TOPICS_DIR = DATA_DIR / "topics"
INDEX_PATH = DATA_DIR / "transcript_index.json"

THEME_RU = {"продажа": "Продажа", "сервис_по_туру": "Сервис по туру",
            "отмена_возврат": "Отмена/возврат", "оплата": "Оплата",
            "юридический_риск": "Юр. риск", "не_клиент": "Не клиент",
            "жалоба": "Жалоба", "другое": "Другое"}


def _theme_ru(pid):
    f = TOPICS_DIR / f"{pid}.json"
    try:
        d = json.loads(f.read_text())
        return THEME_RU.get(d.get("theme"), "—")
    except Exception:
        return "—"

SHEET_ID = "1cxUyebJDQEC1u5qI-Yqs0hR28hzVNkLpufeGhTKYbbQ"
CREDS = "/home/user/Analytics_salebot/data/gsheets_credentials.json"

MANAGERS = {"Фёдорова Анастасия", "Рогачевская Карина", "Яршевич Екатерина", "Лазарчук Кристина"}
MONTHS_RU = {1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель", 5: "Май", 6: "Июнь",
             7: "Июль", 8: "Август", 9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"}
OUTCOME_RU = {"booked": "Бронь", "interested": "Интерес", "not_interested": "Не интересно",
              "callback": "Перезвон", "client_unavailable": "Недоступен", "info_only": "Только инфо",
              "complaint": "Жалоба", "wrong_number": "Ошибочный", "no_transcript": "—", "unknown": "—"}

HEADER = ["№", "Дата", "Время", "Менеджер", "Тип", "Тема", "Телефон клиента", "Направление",
          "Длит.,с", "Качество", "Исход", "Проблемы", "AI-разбор косяка", "ПОЛНЫЙ ТРАНСКРИПТ"]


def _gc():
    import gspread
    from google.oauth2.service_account import Credentials
    creds = Credentials.from_service_account_file(CREDS, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"])
    return gspread.authorize(creds)


def collect_rows(year, month, end_day):
    pm = load_phones_map()
    rows, index = [], {}
    d = datetime(year, month, 1)
    end = datetime(year, month, end_day, 23, 59, 59)
    while d <= end:
        f = CALLS_DIR / f"{d.strftime('%Y-%m-%d')}.json"
        if f.exists():
            calls = json.loads(f.read_text())
            an = load_analyses_for_date(d)
            day_rows = []
            for c in calls:
                if c.get("sip_status") != "answer":
                    continue
                name = get_manager_name(c, pm)
                if name not in MANAGERS:
                    continue
                pid = c.get("public_id")
                tf = TRANSCRIPTS_DIR / f"{pid}.json"
                if not tf.exists():
                    continue
                td = json.loads(tf.read_text())
                text = build_transcript_text(td)
                if not text or len(text.strip()) < 20:
                    continue
                a = an.get(pid, {})
                deep = None
                df = DEEP_DIR / f"{pid}.json"
                if df.exists():
                    try:
                        deep = json.loads(df.read_text())
                    except Exception:
                        deep = None
                start = (c.get("start_time") or "")[:16].replace("T", " ")
                ai = ""
                if deep:
                    ai = f"❗ {deep.get('problem','')}\n✅ {deep.get('advice','')}"
                day_rows.append({
                    "pid": pid,
                    "date": d.strftime("%d.%m.%Y"),
                    "time": start[11:16] if len(start) >= 16 else "",
                    "manager": name,
                    "type": "Вход." if c.get("call_type") == "inbound" else "Исход.",
                    "theme": _theme_ru(pid),
                    "phone": c.get("client_number", ""),
                    "direction": a.get("direction") or "",
                    "dur": c.get("duration_sec") or "",
                    "quality": a.get("quality_score") if a.get("quality_score") is not None else "",
                    "outcome": OUTCOME_RU.get(a.get("outcome"), a.get("outcome") or ""),
                    "issues": ", ".join(a.get("issues") or []),
                    "ai": ai,
                    "transcript": text,
                })
            rows.extend(day_rows)
        d += timedelta(days=1)
    # сортировка: менеджер → дата/время
    rows.sort(key=lambda r: (r["manager"], r["date"].split(".")[::-1], r["time"]))
    return rows


def publish(year, month, end_day):
    import gspread
    rows = collect_rows(year, month, end_day)
    if not rows:
        print("[transcripts] нет данных за период")
        return
    gc = _gc()
    sh = gc.open_by_key(SHEET_ID)
    title = f"Звонки {MONTHS_RU[month]} {year}"
    # Пересоздаём лист через временное имя — нельзя удалить единственный лист в книге.
    tmp = f"{title}__tmp"
    for t in (tmp,):
        try:
            sh.del_worksheet(sh.worksheet(t))
        except gspread.WorksheetNotFound:
            pass
    ws = sh.add_worksheet(title=tmp, rows=len(rows) + 10, cols=len(HEADER))
    try:
        sh.del_worksheet(sh.worksheet(title))
    except gspread.WorksheetNotFound:
        pass
    ws.update_title(title)

    values = [HEADER]
    index = {}
    for i, r in enumerate(rows):
        rownum = i + 2  # +1 header, +1 1-based
        index[r["pid"]] = rownum
        values.append([str(i + 1), r["date"], r["time"], r["manager"], r["type"], r["theme"],
                       r["phone"], r["direction"], r["dur"], r["quality"], r["outcome"],
                       r["issues"], r["ai"], r["transcript"]])
    ws.update(range_name="A1", values=values, value_input_option="RAW")

    sid = ws.id
    def W(c1, c2, px):
        return {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS",
                "startIndex": c1, "endIndex": c2}, "properties": {"pixelSize": px}, "fields": "pixelSize"}}
    reqs = [
        {"updateSheetProperties": {"properties": {"sheetId": sid,
            "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.12, "green": 0.16, "blue": 0.22},
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                "wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy)"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": 13, "endColumnIndex": 14},
            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP",
                "textFormat": {"fontSize": 9}}}, "fields": "userEnteredFormat(wrapStrategy,verticalAlignment,textFormat)"}},
        {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": 11, "endColumnIndex": 13},
            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
            "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}},
        W(0, 1, 40), W(1, 2, 72), W(2, 3, 52), W(3, 4, 150), W(4, 5, 56), W(5, 6, 110), W(6, 7, 120),
        W(7, 8, 110), W(8, 9, 56), W(9, 10, 60), W(10, 11, 90), W(11, 12, 170), W(12, 13, 300), W(13, 14, 620),
    ]
    sh.batch_update({"requests": reqs})

    INDEX_PATH.write_text(json.dumps(
        {"spreadsheet_id": SHEET_ID, "gid": sid, "title": title, "rows": index},
        ensure_ascii=False), encoding="utf-8")
    print(f"[transcripts] опубликовано звонков: {len(rows)} → лист «{title}» (gid={sid})")
    print(f"[transcripts] индекс: {INDEX_PATH}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", help="YYYY-MM (по умолчанию текущий)")
    a = ap.parse_args()
    today = datetime.now()
    if a.month:
        y, m = map(int, a.month.split("-"))
    else:
        y, m = today.year, today.month
    end_day = today.day if (y, m) == (today.year, today.month) else monthrange(y, m)[1]
    publish(y, m, end_day)


if __name__ == "__main__":
    main()
