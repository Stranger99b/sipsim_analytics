# -*- coding: utf-8 -*-
"""Оценка выполнения СКРИПТА по теме звонка (Haiku) → data/scripts/<pid>.json.

Для каждого продажного/сервисного/оплатного/отменного входящего звонка AI проверяет
чеклист ИМЕННО этой темы (галочки по пунктам) и считает script_pct = % выполненных пунктов.
Метрика дашборда «Техника звонка» строится поверх этого (sipsim_source), со сравнением
со средним по теме. Темы вне списка (не_клиент/жалоба/юр.риск/другое) не скриптуются.

Бизнес-контекст: GoTrips = ФИКСИРОВАННЫЕ ГРУППОВЫЕ АВТОБУСНЫЕ туры (клиент выбирает
направление+дату заезда; бюджета/подбора отелей как в авиа НЕТ). Чеклисты утверждены
пользователем (см. память project_sipsim_call_scripts).
"""
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from classify_calls import (  # noqa
    build_transcript_text, CLAUDE_BIN, CALLS_DIR, TRANSCRIPTS_DIR, TOPICS_DIR,
)

DATA_DIR = CALLS_DIR.parent
SCRIPTS_DIR = DATA_DIR / "scripts"
SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
MODEL = "claude-haiku-4-5"

CHECKLISTS = {
    "продажа": [
        "выявил потребность: НАПРАВЛЕНИЕ + желаемые ДАТЫ + СОСТАВ (сколько человек едет). "
        "Бюджет уточнять НЕ нужно — туры фиксированные групповые автобусные",
        "предложил конкретику под запрос: назвал подходящие групповые ЗАЕЗДЫ (даты) и НАЛИЧИЕ МЕСТ; "
        "предложил РАЗМЕЩЕНИЕ по составу — если клиент едет ОДИН: подселение или индивидуальный "
        "номер за доплату; если компания 3+: объяснил, как будет расселение",
        "если предложенное не подошло — предложил АЛЬТЕРНАТИВНЫЙ групповой тур (другое направление/даты)",
        "договорился о следующем шаге — ЛЮБОЙ из: бронь на звонке / перезвон / "
        "перевод в чат (SMS со ссылкой) для деталей или цифрового бронирования",
    ],
    "сервис_по_туру": [
        "идентифицировал бронь / клиента",
        "точно, по существу и ПОЛНО ответил на вопрос(ы) клиента",
        "убедился, что вопрос клиента закрыт, или назначил следующий шаг",
    ],
    "оплата": [
        "идентифицировал заказ / бронь клиента (если это нужно для ответа; для простого вопроса "
        "«до какого числа платить» идентификация не обязательна → applicable=false)",
        "КОРРЕКТНО назвал нужный ЭТАП и СРОК оплаты. Оплата в 3 этапа: (1) туруслуга по ссылке; "
        "(2) предоплата за тур за 10 дней до выезда; (3) оплата на месте у принимающей стороны",
        "если оплату нужно совершить сейчас — отправил/назвал ССЫЛКУ на оплату (в чат или SMS); "
        "если клиент лишь спрашивал сроки → applicable=false",
        "подтвердил/зафиксировал срок — до какого числа оплатить",
    ],
    "отмена_возврат": [
        "выяснил причину отмены",
        "сделал попытку УДЕРЖАНИЯ (перенос дат / альтернатива) — ТОЛЬКО если причина «мягкая» "
        "(передумал / сомнения / дорого). При отмене по вине оператора (не собралась группа) "
        "или форс-мажоре (травма и т.п.) давление неуместно → пункт НЕ применим (applicable=false)",
        "чётко объяснил условия/сроки возврата или вариант переноса",
        "зафиксировал следующий шаг / оставил дверь открытой",
    ],
}

PROMPT = """Ты — контролёр качества звонков турагентства GoTrips.
ВАЖНО про бизнес: GoTrips продаёт ФИКСИРОВАННЫЕ ГРУППОВЫЕ АВТОБУСНЫЕ туры по России и СНГ.
Клиент выбирает НАПРАВЛЕНИЕ и ДАТУ ЗАЕЗДА из готовых групповых туров; менеджер называет заезды
и наличие мест. Логики «подбор отелей под бюджет», как в авиатурах, тут НЕТ — бюджет не выясняют.
Звонок отнесён к типу «{theme}». Оцени, выполнил ли менеджер СКРИПТ для этого типа.

Ниже пункты скрипта. Для КАЖДОГО реши по транскрипту: done=true (выполнил) или false (нет).
Если пункт по ситуации НЕ применим (в тексте пункта указано, когда), ставь applicable=false и done=true.
evidence — короткая цитата или пояснение (до 12 слов).

ПУНКТЫ СКРИПТА:
{items}

Ответ СТРОГО JSON без markdown:
{"items":[{"n":1,"done":true,"applicable":true,"evidence":"..."}],"comment":"1 фраза: что упустил/сделал хорошо"}

Speaker 0 обычно менеджер (отвечает), Speaker 1 — клиент.
ТРАНСКРИПТ:
{transcript}"""


def score(pid, theme, force=False, model=MODEL):
    out = SCRIPTS_DIR / f"{pid}.json"
    if out.exists() and not force:
        return json.loads(out.read_text())
    if theme not in CHECKLISTS:
        return None
    tf = TRANSCRIPTS_DIR / f"{pid}.json"
    if not tf.exists():
        return None
    text = build_transcript_text(json.loads(tf.read_text()))
    if not text or len(text.strip()) < 20:
        return None
    items = CHECKLISTS[theme]
    numbered = "\n".join(f"{i + 1}. {it}" for i, it in enumerate(items))
    prompt = (PROMPT.replace("{theme}", theme).replace("{items}", numbered)
              .replace("{transcript}", text))
    for attempt in range(3):
        try:
            proc = subprocess.run(
                [CLAUDE_BIN, "--print", "--model", model,
                 "--dangerously-skip-permissions", "-p", prompt],
                capture_output=True, text=True, timeout=120)
            raw = proc.stdout.strip() if proc.returncode == 0 else ""
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            if not raw:
                raise ValueError(f"empty (exit={proc.returncode})")
            parsed = json.loads(raw)
            done = sum(1 for it in parsed["items"] if it.get("done"))
            result = {"public_id": pid, "theme": theme,
                      "items": parsed["items"],
                      "script_pct": round(100 * done / len(items)),
                      "comment": parsed.get("comment", "")}
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            return result
        except Exception as e:
            if attempt < 2:
                time.sleep((attempt + 1) * 8)
            else:
                print(f"[score] ERROR {pid}: {e}", flush=True)
                return None


def find_script_calls(start_dt, end_dt):
    """(pid, theme) для входящих отвеченных звонков со скриптовой темой."""
    res, d = [], start_dt
    while d <= end_dt:
        f = CALLS_DIR / f"{d.strftime('%Y-%m-%d')}.json"
        d += timedelta(days=1)
        if not f.exists():
            continue
        for c in json.loads(f.read_text()):
            if c.get("call_type") != "inbound" or c.get("sip_status") != "answer":
                continue
            pid = c.get("public_id")
            tf = TOPICS_DIR / f"{pid}.json"
            if not tf.exists():
                continue
            th = json.loads(tf.read_text()).get("theme")
            if th in CHECKLISTS:
                res.append((pid, th))
    return res


def backfill(start_dt, end_dt, workers=3, force=False, delay=0.0):
    calls = find_script_calls(start_dt, end_dt)
    todo = [(p, t) for p, t in calls if force or not (SCRIPTS_DIR / f"{p}.json").exists()]
    print(f"[score] скриптовых звонков: {len(calls)}, к разбору: {len(todo)}, "
          f"воркеров {workers}, задержка {delay}с, модель {MODEL}", flush=True)
    n = 0
    if workers <= 1:
        # тихий режим: последовательно + пауза между звонками (щадит частотный лимит)
        for a in todo:
            score(*a, force=force)
            n += 1
            if n % 20 == 0 or n == len(todo):
                print(f"[score] {n}/{len(todo)}", flush=True)
            if delay:
                time.sleep(delay)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for _ in ex.map(lambda a: score(*a, force=force), todo):
                n += 1
                if n % 20 == 0 or n == len(todo):
                    print(f"[score] {n}/{len(todo)}", flush=True)
    print(f"[score] готово. Файлов в data/scripts: {len(list(SCRIPTS_DIR.glob('*.json')))}", flush=True)


if __name__ == "__main__":
    import argparse
    from calendar import monthrange
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", help="YYYY-MM (по умолчанию текущий месяц до сегодня)")
    ap.add_argument("--start", help="YYYY-MM-DD (ручной диапазон, override)")
    ap.add_argument("--end", help="YYYY-MM-DD")
    ap.add_argument("--workers", type=int, default=1, help="1 стабильно (частотный лимит); 3 падает")
    ap.add_argument("--delay", type=float, default=4.0, help="пауза между звонками (сек)")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    today = datetime.now()
    if a.start and a.end:
        start = datetime.strptime(a.start, "%Y-%m-%d")
        end = datetime.strptime(a.end, "%Y-%m-%d")
    else:
        y, m = map(int, a.month.split("-")) if a.month else (today.year, today.month)
        start = datetime(y, m, 1)
        end_day = today.day if (y, m) == (today.year, today.month) else monthrange(y, m)[1]
        end = datetime(y, m, end_day, 23, 59, 59)
    backfill(start, end, a.workers, a.force, a.delay)
