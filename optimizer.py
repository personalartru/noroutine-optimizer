"""«Оптимизатор» — отдельный пошаговый чат-бот в MAX (@se14730534_1_bot).

Шаги диалога (inline_keyboard, payload вида ``opt:...``):

1. сфера деятельности — 7 сфер из калькулятора;
2. кого оптимизируем — «только я» или команда по специальностям + количество
   (кнопками 1–5, диапазонами или числом сообщением);
3. наиболее частые рутинные операции — мультивыбор, у каждой часов в месяц;
4. результат №1 — описание ситуации, часы рутины и их стоимость;
5. инструменты оптимизации — мультивыбор + шуточная кнопка «Уволить всех»;
6. результат №2 — экономия в часах и рублях и пошаговый чек-лист внедрения;
7. примерная стоимость внедрения, окупаемость, дисклеймер и кнопка
   обратной связи (текст пользователя уходит владельцу).

Модуль не знает про MAX API: он возвращает список сообщений
``{"text": ..., "buttons": [...]}``, а транспорт, ответы на callbacks и
хранение состояния делает max_bot.py. Благодаря этому флоу прогоняется
локально без токена: ``python tools/simulate_optimizer.py``.

Формулировки, подписи кнопок и каталог (сферы, роли, ставки, рутина,
инструменты, чек-лист) — не здесь, а в content.py, который поверх кодовых
дефолтов накладывает content.json. Админка правит именно JSON и коммитит его в
main; следующий круг Actions делает checkout и подхватывает правку. Здесь
остаётся логика: порядок шагов, валидация ответов и арифметика расчёта — то,
что нельзя отдать свободному тексту, иначе цифры начнут врать.

Методика расчёта: часы рутины × фактор инструмента. Факторы (chat 0.65,
agent 0.85, n8n 0.75, tracker 0.5, full 0.9) складываются как независимые
события: непокрытая доля = произведение (1 − f), с потолком 92%. Ставка —
типовая месячная зарплата по специальности, поделённая на ~160 рабочих часов.
"""
import html
import os
import re
import time

import content

# Финальный шаг, кроме заявки, ведёт читателя в канал про автоматизацию —
# там выходят разборы и готовые связки. Ссылка правится в content.json, а
# переменная окружения MAX_CHANNEL_URL по-прежнему сильнее файла: её задаёт
# workflow, и деплой не может случайно показать старую ссылку.
CHANNEL_URL = content.channel_url()

SESSION_TTL = 40 * 60          # без действий сессия сбрасывается
MAX_HEADCOUNT = 300            # потолок для ручного ввода
TEXT_CHUNK = 900               # запас под лимит длины сообщения в MAX
WORK_HOURS = 160               # рабочих часов в месяце на человека

# Каталог и формулировки живут в content.py (и переопределяются content.json),
# здесь остаются только алиасы: на них смотрит код расчёта, а tools/*.py — на
# optimizer.SPHERES / optimizer.ALL_TOOLS.
SELF_ROLE = content.self_role()
SPHERES = content.spheres()
TOOLS = content.tools()
ALL_TOOLS = content.all_tools()

# Кнопки количества: 1–5 отдельными кнопками, дальше диапазоны-оценки.
# Подписи и payload'ы — в контенте, клавиатуру собирает keyboard_count() —
# без поля "type" MAX молча выбрасывает клавиатуру целиком.
COUNT_BUTTONS = content.count_buttons()
# диапазоны считаем по середине оценки
COUNT_RANGE = content.count_range()


def refresh_catalog():
    """Перечитать каталог и ссылку канала после content.reload().

    Нужна всякому, кто меняет контент на лету: в проде это max_bot
    .try_refresh_content() (правка админки внутри живого цикла), в тестах —
    чекеры, которые прогоняют битый и отредактированный оверлей, не перезапуская
    питон, и админка-предпросмотр, рисующая клавиатуру из правленого каталога.
    Без этого шага optimizer.SPHERES остался бы прошлогодним, и правка подписи
    сферы «не применилась бы» — ровно то, что чекер обязан поймать.
    """
    global CHANNEL_URL, SELF_ROLE, SPHERES, TOOLS, ALL_TOOLS
    global COUNT_BUTTONS, COUNT_RANGE
    CHANNEL_URL = content.channel_url()
    SELF_ROLE = content.self_role()
    SPHERES = content.spheres()
    TOOLS = content.tools()
    ALL_TOOLS = content.all_tools()
    COUNT_BUTTONS = content.count_buttons()
    COUNT_RANGE = content.count_range()


# ── Мелкие утилиты ───────────────────────────────────────────────────────────
def esc(value):
    """Экранирование пользовательского текста: сообщения уходят format=html."""
    return html.escape(str(value), quote=False)


def fmt(number):
    return f"{int(round(number)):,}".replace(",", " ")


def money(number):
    return fmt(number) + " ₽"


def hour_rate(salary):
    return round(salary / WORK_HOURS / 5) * 5


def plural(number, forms):
    """plural(2, ("операция", "операции", "операций")) — русские склонения."""
    n = abs(int(number)) % 100
    digit = n % 10
    if 11 <= n <= 14:
        return forms[2]
    if digit == 1:
        return forms[0]
    if 2 <= digit <= 4:
        return forms[1]
    return forms[2]


def capital(text):
    return text[:1].upper() + text[1:] if text else text


def round_to(number, step=1000):
    return int(round(number / step) * step)


def money_range(low, high):
    """Диапазон сумм одним знаком рубля: «200 000–500 000 ₽», а не два «₽» подряд."""
    return f"{fmt(low)}–{fmt(high)} ₽"


def sphere_by_id(sid):
    return next((s for s in SPHERES if s["id"] == sid), None)


def tool_by_id(tid):
    return next((t for t in ALL_TOOLS if t["id"] == tid), None)


def role_at(sphere, arg):
    """Специальность в сфере по индексу из payload'а (opt:r:0 … opt:r:3)."""
    try:
        return sphere["roles"][int(arg)]
    except (TypeError, ValueError, IndexError):
        return None


def btn(text, payload):
    return {"type": "callback", "text": text, "payload": payload}


def chunk(text, size=TEXT_CHUNK):
    """Резка длинного текста по абзацам, чтобы не упереться в лимит сообщения."""
    if len(text) <= size:
        return [text]
    # Ведущие пустые строки — часть вёрстки: они отделяют этот текст от
    # предыдущего сообщения. Разбивка идёт по "\n\n", и блок из одних переводов
    # строк выпал бы как пустой абзац, поэтому их снимаем, режем остаток по
    # остатку лимита и возвращаем их на место.
    lead = text[:len(text) - len(text.lstrip("\n"))]
    budget = size - len(lead)
    parts = []
    current = ""
    for block in text[len(lead):].split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= budget:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(block) > budget:
            cut = block.rfind("\n", 0, budget)
            cut = cut if cut > budget // 2 else budget
            parts.append(block[:cut].rstrip())
            block = block[cut:].lstrip()
        current = block
    if current:
        parts.append(current)
    if lead and parts:
        parts[0] = lead + parts[0]
    return parts


def emit(text, buttons=None):
    """Текст (или список текстов) + клавиатура на последнем сообщении."""
    texts = text if isinstance(text, list) else chunk(text)
    messages = [{"text": part} for part in texts]
    if buttons and messages:
        messages[-1]["buttons"] = buttons
    return messages


# ── Состояние ────────────────────────────────────────────────────────────────
def new_session():
    return {
        "step": "sphere",
        "sphere": None,
        "sphere_label": None,
        "mode": None,
        "roles": [],
        "role_pick": [],         # отмеченные на шаге 2 специальности (индексы)
        "role_queue": [],        # у кого ещё спрашиваем количество
        "pending_role": None,
        "last_role": None,
        "ops": [],
        "tools": [],
        "calc": None,
        "sav": None,
        "updated": time.time(),
    }


def is_expired(session, now=None):
    if not session:
        return True
    return (now or time.time()) - float(session.get("updated") or 0) > SESSION_TTL


def expects_text(session):
    """Шаги, где ждём свободный текст: количество и текст заявки.

    «count» — сам вопрос о количестве с кнопками 1–5: в его тексте обещано
    «или число сообщением», значит цифра сообщением должна работать сразу, без
    предварительного нажатия «Своё число» (шаг «count_text»).
    """
    return bool(session) and session.get("step") in ("count", "count_text", "feedback")


def headcount(session):
    return sum(role["count"] for role in session["roles"])


def roles_summary(session):
    """Один человек — «вы сами»; команда — «роль × сколько»."""
    if session.get("mode") == "self":
        return content.text("team.self")
    return ", ".join(content.text("team.pair", label=esc(r["label"]), count=r["count"])
                     for r in session["roles"])


def people_phrase(count):
    return content.text("people_phrase", count=count,
                        word=plural(count, content.words("people")))


def blended_rate(session):
    people = headcount(session)
    if not people:
        return hour_rate(SELF_ROLE["salary"])
    return sum(r["rate"] * r["count"] for r in session["roles"]) / people


# ── Расчёт ───────────────────────────────────────────────────────────────────
def compute_routine(session):
    sphere = sphere_by_id(session.get("sphere")) or SPHERES[0]
    chosen = [op for op in sphere["ops"] if op["id"] in session["ops"]]
    per_person = min(sum(op["hours"] for op in chosen), WORK_HOURS)
    people = headcount(session) or 1
    rate = blended_rate(session)
    hours = per_person * people
    return {
        "sphere_label": sphere["label"],
        "blurb": sphere["blurb"],
        "people": people,
        "rate": rate,
        "per_person": per_person,
        "share": round(per_person / WORK_HOURS * 100),
        "hours": hours,
        "cost_month": hours * rate,
        "cost_year": hours * rate * 12,
        "ops": chosen,
    }


def combined_factor(tools):
    remaining = 1.0
    for tool in tools:
        remaining *= (1 - tool["factor"])
    return min(1 - remaining, 0.92)


def compute_savings(session, calc):
    tools = [t for t in (tool_by_id(tid) for tid in session["tools"]) if t]
    factor = combined_factor(tools)
    saved_hours = calc["hours"] * factor
    saved_money = saved_hours * calc["rate"]
    scale = 1 + 0.10 * max(0, len(tools) - 1) + 0.08 * max(0, len(calc["ops"]) - 2)
    if calc["people"] >= 10:
        scale += 0.25
    if calc["people"] >= 30:
        scale += 0.25
    impl_low = round_to(sum(t["impl"][0] for t in tools) * scale)
    impl_high = round_to(sum(t["impl"][1] for t in tools) * scale)
    support_low = round_to(sum(t["support"][0] for t in tools))
    support_high = round_to(sum(t["support"][1] for t in tools))
    payback = (impl_low + impl_high) / 2 / saved_money if saved_money else None
    return {
        "tools": tools,
        "factor": factor,
        "saved_hours": saved_hours,
        "saved_money": saved_money,
        "saved_year": saved_money * 12,
        "impl_low": impl_low,
        "impl_high": impl_high,
        "support_low": support_low,
        "support_high": support_high,
        "payback": payback,
    }


# ── Клавиатуры ───────────────────────────────────────────────────────────────
def mark(on):
    """Отметка «галочка/пусто» перед подписью кнопки — тоже текст из контента."""
    return content.label("marks.on" if on else "marks.off")


def keyboard_sphere():
    return [[btn(s["label"], f"opt:s:{s['id']}")] for s in SPHERES]


def keyboard_mode():
    return [[btn(content.label("mode.self"), "opt:m:self"),
             btn(content.label("mode.team"), "opt:m:team")],
            [btn(content.label("reset"), "opt:reset")]]


def keyboard_roles(session):
    """Шаг 2: отмечаем несколько специалистов сразу."""
    sphere = sphere_by_id(session.get("sphere")) or SPHERES[0]
    rows = []
    for i, role in enumerate(sphere["roles"]):
        rows.append([btn(content.label("roles.item", mark=mark(i in session.get("role_pick", [])),
                                      label=role["label"],
                                      rate=money(hour_rate(role["salary"]))),
                         f"opt:r:{i}")])
    rows.append([btn(content.label("roles.ok"), "opt:r:ok"),
                 btn(content.label("roles.clear"), "opt:r:clear")])
    rows.append([btn(content.label("reset"), "opt:reset")])
    return rows


def keyboard_count():
    """1–5 отдельными кнопками, затем диапазоны и свой вариант."""
    return [[btn(label, payload) for label, payload in row] for row in COUNT_BUTTONS]


def keyboard_more():
    """Клавиатура для старых сообщений: там шаг 2 был по одной специальности."""
    return [[btn(content.label("more.add"), "opt:n:more")],
            [btn(content.label("more.calc"), "opt:n:calc")],
            [btn(content.label("reset"), "opt:reset")]]


def keyboard_ops(session):
    sphere = sphere_by_id(session.get("sphere")) or SPHERES[0]
    rows = []
    for op in sphere["ops"]:
        rows.append([btn(content.label("ops.item", mark=mark(op["id"] in session["ops"]),
                                      label=op["label"], hours=op["hours"]),
                         f"opt:o:{op['id']}")])
    rows.append([btn(content.label("ops.ok"), "opt:o:ok"),
                 btn(content.label("ops.clear"), "opt:o:clear")])
    rows.append([btn(content.label("reset"), "opt:reset")])
    return rows


def keyboard_tools_marked(session):
    rows = []
    for tool in ALL_TOOLS:
        if tool["id"] == "fire":
            rows.append([btn(tool["label"], "opt:t:fire")])
            continue
        rows.append([btn(content.label("tools.item",
                                       mark=mark(tool["id"] in session["tools"]),
                                       label=tool["label"]),
                         f"opt:t:{tool['id']}")])
    rows.append([btn(content.label("tools.ok"), "opt:t:ok"),
                 btn(content.label("tools.clear"), "opt:t:clear")])
    rows.append([btn(content.label("reset"), "opt:reset")])
    return rows


def keyboard_final():
    return [[btn(content.label("final.pdf"), "opt:p:report")],
            [btn(content.label("final.lead"), "opt:fb")],
            [btn(content.label("final.again"), "opt:again")]]


def keyboard_after_lead():
    """После заявки расчёт всё ещё жив — кнопка отчёта остаётся нужной."""
    return [[btn(content.label("final.pdf"), "opt:p:report")],
            [btn(content.label("after_lead.again"), "opt:again")],
            [{"type": "link", "text": content.label("after_lead.channel"),
              "url": CHANNEL_URL}]]


# ── Тексты ───────────────────────────────────────────────────────────────────
# Каждый текст берётся из контента по ключу и подставляется именованными
# переменными. Формат числа — ответственность кода (money/fmt/plural): иначе
# админка случайно рассинхронизирует «383 726 ₽» в чате и в PDF.
def text_intro():
    return content.text("intro")


def text_mode(session):
    return content.text("mode.body", sphere=esc(session["sphere_label"]))


def text_roles(session):
    sphere = sphere_by_id(session.get("sphere")) or SPHERES[0]
    picked = [capital(sphere["roles"][i]["label"])
              for i in sorted(session.get("role_pick") or []) if i < len(sphere["roles"])]
    lines = [content.text("roles.title"), "", content.text("roles.hint")]
    if picked:
        lines += ["", content.text("roles.picked",
                                   picked=", ".join(esc(x) for x in picked))]
    return "\n".join(lines)


def text_count(session):
    role = session["pending_role"]
    left = len(session.get("role_queue") or [])
    further = ""
    if left:
        further = content.text("count.left", n=left,
                               word=plural(left, content.words("roles")))
    return content.text("count.body", role=esc(role["label"]),
                        rate=money(role["rate"]), further=further)


def text_team(session):
    roles = ", ".join(content.text("team.pair", label=esc(r["label"]), count=r["count"])
                      for r in session["roles"])
    return content.text("team.body", roles=roles,
                        people=people_phrase(headcount(session)))


def text_ops(session):
    sphere = sphere_by_id(session.get("sphere")) or SPHERES[0]
    total = sum(op["hours"] for op in sphere["ops"] if op["id"] in session["ops"])
    lines = [content.text("ops.title"), "", content.text("ops.hint"), ""]
    for i, op in enumerate(sphere["ops"], start=1):
        lines.append(content.text("ops.item", mark=mark(op["id"] in session["ops"]),
                                  i=i, label=esc(op["label"]), hours=op["hours"]))
    n = len(session["ops"])
    lines += ["", content.text("ops.footer", n=n, hours=total,
                               word=plural(n, content.words("ops")))]
    return "\n".join(lines)


def text_tools(session, calc):
    lines = [content.text("tools.title"), "", content.text("tools.hint"), ""]
    for i, tool in enumerate(ALL_TOOLS):
        if tool["id"] == "fire":
            lines.append(content.text("tools.fire_item", i=i + 1,
                                      label=esc(tool["label"])))
            continue
        lines.append(content.text("tools.item", mark=mark(tool["id"] in session["tools"]),
                                  i=i + 1, label=esc(tool["label"]),
                                  pct=int(round(tool["factor"] * 100))))
    lines += ["", content.text("tools.footer", hours=fmt(calc["hours"]))]
    return "\n".join(lines)


def text_routine(session, calc):
    ops = "\n".join(content.text("routine.item", label=esc(op["label"]),
                                 hours=op["hours"], detail=esc(op["detail"]))
                    for op in calc["ops"])
    return [
        content.text("routine.head", sphere=esc(calc["sphere_label"]),
                     who=roles_summary(session), per_person=calc["per_person"],
                     share=calc["share"], ops=ops),
        content.text("routine.figures", hours=fmt(calc["hours"]),
                    people=people_phrase(calc["people"]),
                    hours_year=fmt(calc["hours"] * 12),
                    cost_month=money(calc["cost_month"]), rate=money(calc["rate"]),
                    cost_year=money(calc["cost_year"])),
    ]


def text_fire(calc):
    return content.text("fire.body", money=money(calc["cost_month"]))


def text_savings(session, calc, sav):
    names = ", ".join(esc(t["label"]) for t in sav["tools"])
    freed = sav["saved_hours"] / max(1, calc["people"])
    top = "\n".join(
        content.text("savings.top_item", label=esc(op["label"]), hours=op["hours"],
                     detail=esc(op["detail"]))
        for op in sorted(calc["ops"], key=lambda o: -o["hours"])[:3])
    return [
        content.text("savings.head", names=names,
                     pct=int(round(sav["factor"] * 100)),
                     hours=fmt(calc["hours"]),
                     after=fmt(calc["hours"] - sav["saved_hours"]),
                     saved=fmt(sav["saved_hours"]),
                     saved_year=fmt(sav["saved_hours"] * 12),
                     money_month=money(sav["saved_money"]),
                     money_year=money(sav["saved_year"]),
                     freed=fmt(freed), freed_pct=round(freed / WORK_HOURS * 100)),
        content.text("savings.top_title") + "\n" + top,
    ]


def checklist_values(calc, sav):
    """Что подставляется в шаблонные поля чек-листа из каталога."""
    ops = sorted(calc.get("ops") or [], key=lambda o: -o["hours"])
    first = ops[0]["label"] if ops else content.text("checklist.fallback_first")
    first_hours = ops[0]["hours"] if ops else 0
    second = ops[1]["label"] if len(ops) > 1 else content.text("checklist.fallback_second")
    pilot = (sav["tools"][0]["pilot"] if sav.get("tools")
             else content.text("checklist.fallback_pilot"))
    tool_names = (", ".join(t["label"].lower() for t in sav.get("tools", []))
                  or content.text("checklist.fallback_tools"))
    return {
        "first": esc(first),
        "first_hours": first_hours,
        "second": esc(second),
        "pilot": esc(pilot),
        "tools": esc(tool_names),
    }


def control_values(calc, sav):
    """Подстановки каталога «Контроль окупаемости»: расчётная экономия в рублях."""
    return {"money": money((sav or {}).get("saved_money") or 0)}


def control_points(calc, sav):
    """Точки контроля из каталога — тем же манером, что и шаги чек-листа."""
    values = control_values(calc, sav)
    return [{"point": content.fill(point.get("point"), values),
             "when": content.fill(point.get("when"), values),
             "short": content.fill(point.get("short"), values),
             "check": content.fill(point.get("check"), values)}
            for point in content.controls()]


def checklist_steps(calc, sav):
    """Шаги внедрения из каталога — один источник для чата, PDF и текстового fallback.

    `short` — строка в чате, `body`/`result`/`owner` — три колонки таблицы 04 в PDF:
    в сообщении они раздули бы финальный экран до трёх простыней.
    """
    values = checklist_values(calc, sav)
    steps = []
    for step in content.checklist():
        steps.append({
            "emoji": content.fill(step.get("emoji"), values),
            "title": content.fill(step.get("title"), values),
            "short": content.fill(step.get("short"), values),
            "body": content.fill(step.get("body"), values),
            "when": content.fill(step.get("when"), values),
            "result": content.fill(step.get("result"), values),
            "owner": content.fill(step.get("owner"), values),
        })
    return steps


# ── Финальный отчёт: пять блоков на один источник ─────────────────────────────
# В чате короткая форма (text_brief), в PDF — таблицы (report_blocks через
# report_data), а если файл в этом окружении не собрать — те же блоки
# сообщением (report_text). Все три читают один report_numbers() и один
# report_blocks(), поэтому цифра из чата не может разойтись с цифрой из файла:
# расчёт живёт здесь, а не в двух вёрстках.

BRIEF_OPS = 3    # сколько операций перечисляем в чате; остальные — «и ещё N»


def payback_months(payback):
    """«1,2» / «7» / «—» — одна формулировка окупаемости для всех носителей."""
    if not payback:
        return content.text("report.none")
    return (f"{payback:.1f}".replace(".", ",").rstrip("0").rstrip(",")
            if payback < 10 else str(int(round(payback))))


def report_numbers(session, calc, sav):
    """Отформатированные значения отчёта: числа считает код, подписи — в контенте."""
    sav = sav or {}
    calc = calc or {}
    none = content.text("report.none")
    people = max(1, int(calc.get("people") or 1))
    tools = sav.get("tools") or []
    out = {
        "date": time.strftime("%d.%m.%Y"),
        "status": content.text("report.status"),
        "sphere": calc.get("sphere_label") or "",
        "people": people,
        "team": content.text("report.team_value", people=people_phrase(people),
                             who=roles_summary(session) or none),
        "rate": money(calc.get("rate", 0)),
        "hours": fmt(calc.get("hours", 0)),
        "money": money(calc.get("cost_month", 0)),
        "tools": " + ".join(t["label"] for t in tools) or none,
        "pct": int(round((sav.get("factor") or 0) * 100)),
        "freed": fmt((sav.get("saved_hours") or 0) / people),
        "saved_money": money(sav.get("saved_money", 0)),
        "saved_year": money(sav.get("saved_year", 0)),
        "budget": content.text("report.impl_value",
                               range=money_range(sav.get("impl_low", 0),
                                                 sav.get("impl_high", 0))),
        "support": (content.text("report.support_value",
                                 range=money_range(sav.get("support_low", 0),
                                                   sav.get("support_high", 0)))
                    if sav.get("support_high") else none),
        "months": payback_months(sav.get("payback")),
    }
    return out


def _brief_block(num, title_key):
    """Заголовок блока в чате: «01 · Исходные данные», шаблон и подписи — в контенте."""
    return content.text("brief.block", num=num, title=content.text(title_key))


def text_brief(session, calc, sav):
    """Короткая форма отчёта в чате: те же пять блоков, по 1–2 строки на блок."""
    n = report_numbers(session, calc, sav)
    ops = sorted(calc.get("ops") or [], key=lambda o: -o["hours"])
    shown = ops[:BRIEF_OPS]
    steps = checklist_steps(calc, sav)
    points = control_points(calc, sav)
    header = content.text("report.header_title")
    lines = [
        content.text("brief.title", header=header),
        content.text("brief.meta", date=n["date"], sphere=esc(n["sphere"]),
                     status=n["status"]),
        _brief_block("01", "report.section_data"),
        content.text("brief.data", team=n["team"], rate=n["rate"],
                     hours=n["hours"], money=n["money"]),
        content.text("brief.data_type", tools=n["tools"]),
        _brief_block("02", "report.section_ops"),
    ]
    lines += [content.text("brief.ops_item", label=esc(op["label"]), hours=op["hours"])
              for op in shown]
    rest = len(ops) - len(shown)
    if rest > 0:
        lines.append(content.text("brief.ops_rest", rest=rest,
                                  word=plural(rest, content.words("ops"))))
    lines.append(content.text("brief.ops_total", hours=n["hours"]))
    lines += [
        _brief_block("03", "report.section_effect"),
        content.text("brief.effect", pct=n["pct"],
                     freed=content.text("report.freed_value", hours=n["freed"]),
                     money=n["saved_money"], year=n["saved_year"]),
        content.text("brief.effect_budget", budget=n["budget"], support=n["support"],
                     months=n["months"]),
        _brief_block("04", "report.section_checklist"),
        content.text("brief.checklist_line",
                     items=content.text("brief.checklist_join").join(
                         content.text("brief.checklist_item", i=i, emoji=step["emoji"],
                                      short=step["short"])
                         for i, step in enumerate(steps, start=1))),
        content.text("brief.checklist_hint"),
        _brief_block("05", "report.section_control"),
        content.text("brief.control_line",
                     items=content.text("brief.control_join").join(
                         content.text("brief.control_item", i=i, short=point["short"])
                         for i, point in enumerate(points, start=1))),
        content.text("brief.hint", pdf=content.label("final.pdf")),
    ]
    return "\n".join(lines).rstrip()


def report_blocks(session, calc, sav):
    """Пять блоков отчёта: строки «параметр — значение» и настоящие таблицы.

    Здесь и формат (доли колонок, где жирная строка), и подстановки — всё, чего
    не знает ни content.py, ни вёрстка. Отчёт читают два рендера: report_pdf.py
    печатает файл, report_text() отдаёт то же сообщение, если файла нет.
    """
    n = report_numbers(session, calc, sav)
    rep = content.text
    ops = sorted(calc.get("ops") or [], key=lambda o: -o["hours"])
    steps = checklist_steps(calc, sav)
    points = control_points(calc, sav)
    return [
        {"num": "01", "title": rep("report.section_data"), "kind": "rows",
         "rows": [
             {"label": rep("report.row_team"), "value": n["team"]},
             {"label": rep("report.row_rate"),
              "value": rep("report.rate", rate=n["rate"])},
             {"label": rep("report.row_hours"),
              "value": rep("report.hours_value", hours=n["hours"])},
             {"label": rep("report.row_money"),
              "value": rep("report.month_value", money=n["money"])},
             {"label": rep("report.row_type"),
              "value": rep("report.type_value", tools=n["tools"])},
         ]},
        {"num": "02", "title": rep("report.section_ops"), "kind": "table",
         "columns": [{"label": rep("report.col_num"), "width": 6, "align": "center"},
                     {"label": rep("report.col_op"), "width": 32},
                     {"label": rep("report.col_hours"), "width": 12, "align": "center"},
                     {"label": rep("report.col_detail"), "width": 50}],
         "rows": [[str(i), esc(op["label"]),
                   rep("report.hours_short", hours=op["hours"]),
                   esc(op.get("detail", ""))]
                  for i, op in enumerate(ops, start=1)],
         "empty": rep("report.empty_ops"),
         "total": [None, rep("report.col_total"),
                   rep("report.hours_short", hours=n["hours"]), None]},
        {"num": "03", "title": rep("report.section_effect"), "kind": "rows",
         "rows": [
             {"label": rep("report.effect_take"),
              "value": rep("report.percent_value", pct=n["pct"])},
             {"label": rep("report.effect_freed"),
              "value": rep("report.freed_value", hours=n["freed"])},
             {"label": rep("report.effect_money"),
              "value": rep("report.money_value", money=n["saved_money"],
                           year=n["saved_year"])},
             {"label": rep("report.budget_impl"), "value": n["budget"]},
             {"label": rep("report.budget_support"), "value": n["support"]},
             {"label": rep("report.figure_payback"),
              "value": rep("report.payback_value", months=n["months"]),
              "strong": True},
         ],
         "note": rep("report.effect_note")},
        {"num": "04", "title": rep("report.section_checklist"), "kind": "table",
         "columns": [{"label": rep("report.col_num"), "width": 5, "align": "center"},
                     {"label": rep("report.col_step"), "width": 21},
                     {"label": rep("report.col_do"), "width": 32},
                     {"label": rep("report.col_result"), "width": 25},
                     {"label": rep("report.col_owner"), "width": 17}],
         "rows": [[str(i),
                   rep("report.step_when", title=step["title"], when=step["when"]),
                   step["body"], step["result"], step["owner"]]
                  for i, step in enumerate(steps, start=1)],
         "empty": rep("report.none"),
         "note": rep("report.roles_note")},
        {"num": "05", "title": rep("report.section_control"), "kind": "table",
         "columns": [{"label": rep("report.col_point"), "width": 14},
                     {"label": rep("report.col_when"), "width": 18},
                     {"label": rep("report.col_check"), "width": 68}],
         "rows": [[point["point"], point["when"], point["check"]]
                  for point in points],
         "empty": rep("report.none")},
    ]


def report_data(session, calc, sav):
    """Структура для PDF: шапка, пять блоков и подвал.

    `slug` и `date` остаются на верхнем уровне: по ним транспорт называет файл
    и по ним же фиксированная дата в заголовке PDF держит байты одинаковыми для
    повторной загрузки в MAX.
    """
    n = report_numbers(session, calc, sav)
    return {
        "slug": session.get("sphere") or "",
        "date": n["date"],
        "header": {
            "title": content.text("report.header_title"),
            "subtitle": content.text("report.header_subtitle"),
            "meta": content.text("report.header_meta", date=n["date"],
                                 sphere=esc(n["sphere"]), status=n["status"]),
        },
        "block": content.text("report.block"),
        "blocks": report_blocks(session, calc, sav),
        "howto": {"title": content.text("report.howto"),
                  "body": content.text("report.howto_body")},
        "channel": {"url": CHANNEL_URL,
                    "text": content.text("report.channel_line", channel=CHANNEL_URL)},
    }


def report_text(session, calc, sav):
    """Тот же отчёт сообщением: запасной путь, если PDF не собрался.

    Рисует блоки из report_blocks() — файл и сообщение расходятся только
    оформлением, но не содержимым.
    """
    n = report_numbers(session, calc, sav)
    report = report_data(session, calc, sav)
    lines = [content.text("brief.title", header=content.text("report.header_title")),
             content.text("brief.meta", date=n["date"], sphere=esc(n["sphere"]),
                          status=n["status"])]
    for spec in report["blocks"]:
        lines.append(content.text("fallback.block", num=spec["num"],
                                  title=spec["title"]))
        if spec["kind"] == "rows":
            lines += [content.text("fallback.row", label=row["label"],
                                   value=row["value"]) for row in spec["rows"]]
        else:
            join = content.text("fallback.cell_join")
            lines += [content.text("fallback.cell",
                                   cells=join.join(cell for cell in cells if cell))
                      for cells in spec["rows"]]
            if not spec["rows"]:
                lines.append(spec["empty"])
            if spec.get("total"):
                lines.append(content.text("fallback.total", label=spec["total"][1],
                                          value=spec["total"][2]))
        if spec.get("note"):
            lines.append(content.text("fallback.note", text=spec["note"]))
    howto = report["howto"]
    lines.append(content.text("fallback.howto", title=howto["title"],
                              text=howto["body"]))
    lines.append(content.text("fallback.channel", text=report["channel"]["text"]))
    return "\n".join(lines).rstrip()


def text_rollout(sav):
    """Последнее сообщение экрана: честная оговорка и призыв отправить заявку.

    Сама фраза живёт в texts.report.howto_body — тот же текст печатается в PDF,
    поэтому правка в админке меняет и чат, и файл.
    """
    disclaimer = content.text("rollout.disclaimer",
                              howto=content.text("report.howto_body"))
    return [disclaimer + content.text("rollout.cta")]


# ── Диспетчер кнопок ─────────────────────────────────────────────────────────
def text_stale():
    return content.text("stale")

# шаги, которые бессмысленны без выбранной сферы (кнопки «сфера»/«режим» — нет)
STALE_GUARDED = {"r", "c", "n", "o", "t"}

# сессия без сферы — значит кнопки пришли из давно сброшенного расчёта
def is_stale(session):
    return sphere_by_id(session.get("sphere")) is None


def start(session=None):
    """Точка входа: /start, «bot_started» или кнопка «Начать расчёт»."""
    return new_session(), emit(text_intro(), keyboard_sphere())


def dispatch(payload, session):
    """Один клик по клавиатуре → (новое состояние, список сообщений)."""
    if not payload or not payload.startswith("opt:"):
        return session, []
    # добиваем недостающие поля: состояние могло прийти из более старой версии
    session = {**new_session(), **(session or {})}
    session["updated"] = time.time()
    parts = payload.split(":")
    kind = parts[1] if len(parts) > 1 else ""
    arg = parts[2] if len(parts) > 2 else ""

    if kind in NO_ARG_KINDS:
        if kind == "cancel":
            return {**session, "step": "done"}, emit(content.text("cancel"))
        return start(session)

    handler = HANDLERS.get(kind)
    if not handler:
        return session, emit(content.text("unknown_button"))
    if kind in STALE_GUARDED and is_stale(session):
        # тап по клавиатуре из сброшенного/просроченного расчёта
        return session, emit(text_stale())
    return handler(session, arg)


def on_sphere(session, arg):
    sphere = sphere_by_id(arg)
    if not sphere:
        return session, emit(content.text("errors.no_sphere"))
    session.update(sphere=sphere["id"], sphere_label=sphere["label"], mode=None,
                   roles=[], role_pick=[], role_queue=[], pending_role=None,
                   last_role=None, ops=[], tools=[], calc=None, sav=None,
                   step="mode")
    return session, emit(text_mode(session), keyboard_mode())


def on_mode(session, arg):
    if session["step"] != "mode":
        return session, emit(content.text("errors.sphere_first"))
    if arg == "self":
        session["mode"] = "self"
        session["roles"] = [{"label": SELF_ROLE["label"], "count": 1,
                             "rate": hour_rate(SELF_ROLE["salary"])}]
        session["last_role"] = f"{SELF_ROLE['label']} × 1"
        return to_ops(session)
    session["mode"] = "team"
    session["step"] = "role"
    session["role_pick"] = []
    session["role_queue"] = []
    return session, emit(text_roles(session), keyboard_roles(session))


def on_role(session, arg):
    """Шаг 2: отмечаем несколько специалистов, затем спрашиваем количество по каждому."""
    sphere = sphere_by_id(session.get("sphere"))
    if not sphere:
        return start(session)
    if arg == "clear":
        session["role_pick"] = []
        return session, emit(text_roles(session), keyboard_roles(session))
    if arg == "ok":
        picks = [i for i in session.get("role_pick") or [] if i < len(sphere["roles"])]
        if not picks:
            return session, emit(content.text("errors.no_role_pick"))
        session["role_queue"] = [{"label": sphere["roles"][i]["label"],
                                  "rate": hour_rate(sphere["roles"][i]["salary"])}
                                 for i in picks]
        session["role_pick"] = []
        return ask_count(session)
    role = role_at(sphere, arg)
    if not role:
        return session, emit(content.text("errors.bad_role"))
    index = sphere["roles"].index(role)
    pick = list(session.get("role_pick") or [])
    pick.remove(index) if index in pick else pick.append(index)
    session["role_pick"] = pick
    return session, emit(text_roles(session), keyboard_roles(session))


def ask_count(session):
    """Спрашиваем количество по следующей роли из очереди; очередь пуста — в рутину."""
    queue = session.get("role_queue") or []
    if not queue:
        return counts_done(session)
    session["pending_role"] = queue[0]
    session["role_queue"] = queue[1:]
    session["step"] = "count"
    return session, emit(text_count(session), keyboard_count())


def on_count(session, arg):
    role = session.get("pending_role")
    if not role:
        return on_role(session, "ok") if session.get("role_pick") else start(session)
    if arg == "x":
        session["step"] = "count_text"
        return session, emit(content.text("count.ask", role=esc(role["label"])))
    count = COUNT_RANGE.get(f"opt:c:{arg}")
    if count is None:
        try:
            count = int(arg)
        except ValueError:
            return session, emit(content.text("errors.bad_count"))
    record_role(session, role["label"], count, role["rate"])
    session["pending_role"] = None
    return ask_count(session)


def record_role(session, label, count, rate):
    """Плюсуем роль в команду: повторный выбор той же роли складывается."""
    count = max(1, min(int(count), MAX_HEADCOUNT))
    roles = list(session["roles"])
    for existing in roles:
        if existing["label"] == label:
            existing["count"] += count
            break
    else:
        roles.append({"label": label, "count": count, "rate": rate})
    session["roles"] = roles
    session["last_role"] = content.text("team.pair", label=label, count=count)


def counts_done(session):
    """Про всех отмеченных спросили количество — показываем команду и шаг рутины."""
    session, ops_messages = to_ops(session)
    return session, emit(text_team(session)) + ops_messages


def on_more(session, arg):
    """Совместимость с клавиатурой прошлой версии шага 2 (одна специальность за раз)."""
    if arg == "more":
        session["step"] = "role"
        return session, emit(text_roles(session), keyboard_roles(session))
    return to_ops(session)


def to_ops(session):
    sphere = sphere_by_id(session.get("sphere"))
    if not sphere:
        return start(session)
    if not session["roles"]:
        session["roles"] = [{"label": SELF_ROLE["label"], "count": 1,
                             "rate": hour_rate(SELF_ROLE["salary"])}]
        session["mode"] = "self"
    session["step"] = "ops"
    return session, emit(text_ops(session), keyboard_ops(session))


def on_op(session, arg):
    sphere = sphere_by_id(session.get("sphere"))
    if not sphere:
        # кнопка со старого сообщения: сфера уже сбросилась или её не выбирали
        return session, emit(text_stale())
    if arg == "clear":
        session["ops"] = []
        return session, emit(text_ops(session), keyboard_ops(session))
    if arg == "ok":
        if not session["ops"]:
            return session, emit(content.text("errors.no_op_pick"))
        calc = compute_routine(session)
        session["calc"] = calc
        session["step"] = "tools"
        return session, emit(text_routine(session, calc), keyboard_tools_marked(session))
    op = next((o for o in sphere["ops"] if o["id"] == arg), None)
    if not op:
        return session, emit(content.text("errors.bad_op"))
    ops = list(session["ops"])
    ops.remove(op["id"]) if op["id"] in ops else ops.append(op["id"])
    session["ops"] = ops
    return session, emit(text_ops(session), keyboard_ops(session))


def on_tool(session, arg):
    if session.get("step") not in ("tools", "savings", "rollout"):
        return session, emit(text_stale())
    calc = session.get("calc") or compute_routine(session)
    session["calc"] = calc
    if arg == "clear":
        session["tools"] = []
        return session, emit(text_tools(session, calc), keyboard_tools_marked(session))
    if arg == "ok":
        if not session["tools"]:
            return session, emit(content.text("errors.no_tool_pick"))
        sav = compute_savings(session, calc)
        session["sav"] = sav
        session["step"] = "savings"
        return session, emit(text_savings(session, calc, sav),
                             [[btn(content.label("tools.plan"), "opt:t:plan")],
                              [btn(content.label("tools.back"), "opt:t:back")]])
    if arg == "plan":
        sav = session.get("sav") or compute_savings(session, calc)
        session["step"] = "rollout"
        messages = emit(text_brief(session, calc, sav))
        messages += emit(text_rollout(sav), keyboard_final())
        return session, messages
    if arg == "back":
        session["step"] = "tools"
        return session, emit(text_tools(session, calc), keyboard_tools_marked(session))
    tool = tool_by_id(arg)
    if not tool:
        return session, emit(content.text("errors.bad_tool"))
    if tool["id"] == "fire":
        return session, emit(text_fire(calc)) \
            + emit(text_tools(session, calc), keyboard_tools_marked(session))
    tools = list(session["tools"])
    tools.remove(tool["id"]) if tool["id"] in tools else tools.append(tool["id"])
    session["tools"] = tools
    return session, emit(text_tools(session, calc), keyboard_tools_marked(session))


def on_feedback(session, _arg=None):
    session["step"] = "feedback"
    return session, emit(content.text("feedback.prompt"))


def on_pdf(session, _arg=None):
    """Кнопка «Полный отчёт (PDF)»: транспорт соберёт файл из `pdf`.

    Если генерация или загрузка не получится, транспорт отправит `pdf_fallback`
    — тот же отчёт текстом (report_text), чтобы человек не остался ни с чем.
    """
    calc = session.get("calc") or {}
    sav = session.get("sav") or {}
    if not calc or not sav:
        return session, emit(content.text("pdf.needs_steps"), keyboard_sphere())
    data = report_data(session, calc, sav)
    return session, [{
        "text": content.text("pdf.preparing"),
        "pdf": data,
        "pdf_fallback": report_text(session, calc, sav),
        "buttons": keyboard_final(),
    }]


HANDLERS = {
    "s": on_sphere,
    "m": on_mode,
    "r": on_role,
    "c": on_count,
    "n": on_more,
    "o": on_op,
    "t": on_tool,
    "p": on_pdf,
    "fb": on_feedback,
}

# Кнопки без аргумента: dispatch разбирает их до HANDLERS. Список живёт рядом с
# разбором, чтобы справочник админки (content.schema.json) перечислял виды
# payload'ов из кода, а не из догадки на странице.
NO_ARG_KINDS = ("reset", "again", "start", "cancel")


def payload_kinds():
    """Виды payload'ов `opt:<вид>`, которые dispatch действительно понимает."""
    return sorted(set(HANDLERS) | set(NO_ARG_KINDS))


# ── Текстовые сообщения внутри активной сессии ───────────────────────────────
def dispatch_text(text, session):
    """Свободный текст на шагах, где он ожидается: число и заявка."""
    session = {**new_session(), **(session or {})}
    session["updated"] = time.time()
    step = session.get("step")

    if step in ("count", "count_text"):
        role = session.get("pending_role")
        if not role:
            # состояние пришло без роли — возвращаем к выбору специальности
            return to_ops(session) if session.get("roles") else start(session)
        match = re.search(r"\d+", text or "")
        if not match:
            return session, emit(content.text("errors.need_number"))
        record_role(session, role["label"], int(match.group()), role["rate"])
        session["pending_role"] = None
        return ask_count(session)

    if step == "feedback":
        calc = session.get("calc") or {}
        summary = ""
        if calc:
            summary = content.text("feedback.draft",
                                   sphere=calc.get("sphere_label"),
                                   people=calc.get("people"),
                                   hours=fmt(calc.get("hours", 0)),
                                   cost=money(calc.get("cost_month", 0)))
        messages = emit(content.text("feedback.done") + summary, keyboard_after_lead())
        # «admin» — сигнал транспорту переслать этот текст владельцу
        messages[-1]["admin"] = text
        return {**session, "step": "done"}, messages

    return session, []
