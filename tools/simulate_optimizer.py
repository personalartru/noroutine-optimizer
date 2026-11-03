"""Локальный прогон «Оптимизатора» без токена MAX.

Моделирует клик-путь пользователя по клавиатуре (payload'ы callbacks и текст в
свободных шагах) и печатает транскрипт диалога так, как его увидел бы человек:
сообщения бота, подписанные нажатия и кнопки. Заодно проверяет инварианты:
доходимость до финала, длину payload и кнопок, резку длинных сообщений,
целостность HTML-разметки.

    python3 tools/simulate_optimizer.py                  # все сценарии + превью
    python3 tools/simulate_optimizer.py --scenario solo  # один сценарий
    python3 tools/simulate_optimizer.py --json           # транскрипт в JSON
    python3 tools/simulate_optimizer.py --quiet          # только проверки
    python3 tools/simulate_optimizer.py --overlay        # репетиция правки из админки

Превью для глаз: optimizer-preview.html (открыть в браузере).

Режим --overlay прогоняет те же сценарии второй раз поверх намеренно edited
content.json (как его запишет админка) и сверяет два снимка: путь по кнопкам,
форму клавиатур и цифры расчёта. Права из админки обязана менять слова, а не
арифметику — если цифры сдвинулись, значит редактор контента полез туда, куда
ему нельзя, или payload кнопки уехал вместе с подписью.
"""
import argparse
import contextlib
import copy
import html as html_mod
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PREVIEW_PATH = os.path.join(ROOT, "optimizer-preview.html")

import content  # noqa: E402
import optimizer as opt  # noqa: E402

SCENARIOS = {
    "solo": {
        "title": "Сам на всё в небольшом интернет-магазине",
        "clicks": ["opt:s:ecom", "opt:m:self",
                   "opt:o:cards", "opt:o:reviews", "opt:o:stock", "opt:o:report",
                   "opt:o:ok",
                   "opt:t:n8n", "opt:t:ok", "opt:t:plan", "opt:fb"],
        "text": ["Веду три маркетплейса один, карточки и отзывы — вечером. "
                 "Контакт: @ivan"],
    },
    "team": {
        "title": "Поддержка IT-продукта: 8 первой линии + 4 аналитика + 2 тестера",
        "clicks": ["opt:s:it", "opt:m:team",
                   "opt:r:0", "opt:r:1", "opt:r:2", "opt:r:ok",
                   "opt:c:8", "opt:c:4", "opt:c:2",
                   "opt:o:ticket", "opt:o:feedback", "opt:o:metrics", "opt:o:release",
                   "opt:o:ok",
                   "opt:t:chat", "opt:t:agent", "opt:t:n8n", "opt:t:ok",
                   "opt:t:plan", "opt:fb"],
        "text": ["Одни и те же вопросы про статус и оплату, 14 человек в контуре. "
                 "support@company.ru"],
    },
    "fire": {
        "title": "Бухгалтерия 9 человек и шутка про «Уволить всех»",
        "clicks": ["opt:s:finance", "opt:m:team", "opt:r:0", "opt:r:ok", "opt:c:x",
                   "opt:o:primary", "opt:o:bank", "opt:o:close", "opt:o:ok",
                   "opt:t:fire", "opt:t:full", "opt:t:ok", "opt:t:plan", "opt:fb"],
        "text": ["9", "Соберите, пожалуйста, сверку выписок и разнос платежей."],
    },
    "again": {
        "title": "Второй проход: финал → «Пройти заново» → снова шаг 1",
        "clicks": ["opt:s:marketing", "opt:m:self",
                   "opt:o:leads", "opt:o:crm", "opt:o:ok",
                   "opt:t:chat", "opt:t:ok", "opt:t:plan", "opt:fb",
                   "opt:again", "opt:s:services", "opt:m:self",
                   "opt:o:record", "opt:o:chat", "opt:o:ok",
                   "opt:t:agent", "opt:t:ok", "opt:t:plan", "opt:fb"],
        "text": ["Реклама и отчёты по лидам, одна. marketing@shop.ru",
                 "Запись клиентов и ответы в чатах, администратор одна."],
    },
    "mistakes": {
        "title": "Пользователь жмёт не туда и пишет не то",
        "clicks": ["opt:t:chat", "opt:o:ok", "opt:n:calc", "opt:c:x", "opt:m:self",
                   "opt:r:1", "opt:r:1", "opt:r:ok", "opt:n:more",
                   "opt:s:it", "opt:o:zz", "opt:t:qq", "opt:o:ticket",
                   "opt:o:ok", "opt:t:ok", "opt:t:back", "opt:reset"],
        "text": ["не число", "сорок два", "отмена"],
    },
    "typed": {
        "title": "Команда: количество набрано сообщением, без кнопки «Своё число»",
        "clicks": ["opt:s:it", "opt:m:team", "opt:r:0", "opt:r:1", "opt:r:ok",
                   "opt:o:ticket", "opt:o:ok", "opt:t:chat", "opt:t:ok",
                   "opt:t:plan", "opt:fb"],
        "text": ["8", "4", "Поддержка продукта, 12 человек в контуре. "
                 "support@company.ru"],
    },
}


# Клик уместнее текста, если он про текущий шаг: иначе сценарий с цифрами,
# набранными сообщением, съедал текст заявки раньше времени, а сценарий «team»
# терял клик «opt:c:8».
STEP_KIND = {"sphere": "s", "mode": "m", "role": "r", "count": "c",
             "count_text": "c", "ops": "o", "tool": "t", "feedback": "fb"}


def click_for_step(payload, step):
    """«opt:c:8» про шаг количества? Сравниваем вид payload с шагом."""
    parts = (payload or "").split(":")
    return len(parts) > 1 and STEP_KIND.get(step) == parts[1]


def render_buttons(buttons):
    return " | ".join(b.get("text", "?") for row in buttons or [] for b in row)


def play(name, script, verbose=True):
    """Честный прогон: клик → ответ бота → если бот ждёт текст → ответ текстом."""
    session, messages = opt.start()
    transcript = []
    labels = {}          # payload → текст кнопки из последних показанных клавиатур

    def remember(buttons):
        for row in buttons or []:
            for button in row:
                if button.get("payload"):
                    labels[button["payload"]] = button["text"]

    def push(role, text, buttons="", payload=None, raw=None):
        transcript.append({"role": role, "text": text, "buttons": buttons,
                           "payload": payload, "raw": raw})

    for message in messages:
        remember(message.get("buttons"))
        push("bot", message["text"], render_buttons(message.get("buttons")),
             raw=message.get("buttons"))

    clicks, texts = list(script["clicks"]), list(script["text"])
    for _ in range(200):
        # Кнопки сценария важнее текста, пока они про текущий шаг: иначе
        # скрипт съедал текст заявки на первом же вопросе о количестве.
        use_click = bool(clicks) and (
            click_for_step(clicks[0], session.get("step"))
            or not (opt.expects_text(session) and texts))
        if use_click:
            payload = clicks.pop(0)
            push("user", f"нажал «{labels.get(payload, payload)}»", payload=payload)
            session, messages = opt.dispatch(payload, session)
        elif texts:
            text = texts.pop(0)
            push("user", text)
            session, messages = opt.dispatch_text(text, session)
        else:
            break
        for message in messages:
            remember(message.get("buttons"))
            push("bot", message["text"], render_buttons(message.get("buttons")),
                 raw=message.get("buttons"))
        if not opt.expects_text(session) and not clicks and not texts:
            break

    if verbose:
        print(f"\n{'=' * 78}\n{script['title']}  [{name}]  финальный шаг = "
              f"{session['step']}\n{'=' * 78}")
        for item in transcript:
            who = "👤" if item["role"] == "user" else "🤖"
            print(f"\n{who} {item['text']}")
            if item["buttons"]:
                print(f"   ⌨️  {item['buttons']}")
    return transcript, session


HAPPY = ("solo", "team", "fire", "again", "typed")
FAILURE_PHRASES = ("не распознана", "нет в списке", "не понял", "Не понял",
                   "нужно число", "Нужно число", "Traceback")


def validate(transcripts):
    problems, payloads = [], set()
    # Приветствие берём из активного контента: в режиме --overlay оно другое,
    # строка, снятая на импорте, посчитала бы перезапуск нулём.
    intro = opt.text_intro().splitlines()[0][:24]
    for name, transcript, session in transcripts:
        if name in HAPPY and session["step"] != "done":
            problems.append(f"{name}: не дошёл до финала (шаг {session['step']})")
        if name == "again":
            restarts = sum(1 for item in transcript
                           if item["role"] == "bot" and item["text"].startswith(intro))
            if restarts < 2:
                problems.append(f"again: «Пройти заново» не вернул к шагу 1 "
                                f"(приветствий: {restarts})")
        for item in transcript:
            if item["role"] == "user":
                if item["payload"]:
                    payloads.add(item["payload"])
                    if len(item["payload"]) > 32:
                        problems.append(f"{name}: payload длинный — {item['payload']}")
                continue
            if len(item["text"]) > opt.TEXT_CHUNK + 40:
                problems.append(f"{name}: сообщение {len(item['text'])} символов — "
                                "резка не сработала")
            for tag in ("b", "i"):
                if item["text"].count(f"<{tag}>") != item["text"].count(f"</{tag}>"):
                    problems.append(f"{name}: незакрытый <{tag}> в «{item['text'][:40]}»")
            if name in HAPPY:
                for phrase in FAILURE_PHRASES:
                    if phrase in item["text"]:
                        problems.append(f"{name}: в удачном сценарии реплика "
                                        f"«{phrase[:20]}» — значит, кнопка не распознана")
            for row in item.get("raw") or []:
                if len(row) > 7:
                    problems.append(f"{name}: в ряду {len(row)} кнопок — MAX "
                                    "допускает 7")
                for button in row:
                    if button.get("type") not in ("callback", "link"):
                        problems.append(f"{name}: у кнопки «{button.get('text')}» "
                                        "нет type — MAX не покажет клавиатуру")
                    if button.get("type") == "callback" and not button.get("payload"):
                        problems.append(f"{name}: callback без payload — "
                                        f"«{button.get('text')}»")
                    if button.get("type") == "link" and not button.get("url"):
                        problems.append(f"{name}: link без url — «{button.get('text')}»")
            for key in item["buttons"].split(" | "):
                if key and len(key) > 64:
                    problems.append(f"{name}: подпись кнопки {len(key)} символов — "
                                    f"«{key[:40]}…»")
    return problems, sorted(payloads)


def render_html(transcripts):
    """Одностраничное превью диалога в стиле чата MAX — проверить глазами."""
    def bubble(item):
        cls = "me" if item["role"] == "user" else "bot"
        if item["role"] == "bot":
            body = item["text"].replace("\n", "<br>")
        else:
            body = html_mod.escape(item["text"]).replace("\n", "<br>")
        keys = ""
        if item["buttons"]:
            keys = '<div class="keys">' + "".join(
                f"<span>{html_mod.escape(part)}</span>"
                for part in item["buttons"].split(" | ")) + "</div>"
        return f'<div class="row {cls}"><div class="bubble">{body}{keys}</div></div>'

    sections = []
    for name, transcript, _ in transcripts:
        rows = "".join(bubble(item) for item in transcript)
        sections.append(
            f'<section><h2>{html_mod.escape(SCENARIOS[name]["title"])}'
            f'<code>{name}</code></h2>{rows}</section>')

    return """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Оптимизатор — превью диалога в MAX</title>
<style>
:root{--bg:#0f1115;--panel:#171a21;--me:#2e6bd6;--bot:#22262f;--on:#e8eaf0;
--dim:#98a1b3;--accent:#2fce9a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--on);
font:15px/1.55 -apple-system,Inter,system-ui,sans-serif;padding:24px 12px 80px}
header{max-width:760px;margin:0 auto 18px}
h1{font-size:20px;margin:0 0 6px}
header p{color:var(--dim);margin:0;font-size:13px}
section{max-width:760px;margin:22px auto;background:var(--panel);border-radius:18px;
padding:16px 14px 22px;box-shadow:0 8px 30px rgba(0,0,0,.35)}
h2{font-size:14px;text-transform:uppercase;letter-spacing:.04em;color:var(--accent);
margin:0 0 14px;display:flex;gap:8px;align-items:baseline}
h2 code{color:var(--dim);font-size:11px;text-transform:none;letter-spacing:0}
.row{display:flex;margin:8px 0}
.row.me{justify-content:flex-end}
.bubble{max-width:88%;background:var(--bot);border-radius:16px 16px 16px 4px;
padding:10px 13px;font-size:14px;overflow-wrap:anywhere}
.row.me .bubble{background:var(--me);border-radius:16px 16px 4px 16px;
font-size:13px;color:#eaf1ff}
.bubble b{color:#fff}
.bubble i{color:var(--dim)}
.keys{margin-top:9px;display:flex;flex-wrap:wrap;gap:5px}
.keys span{border:1px solid var(--accent);color:var(--accent);border-radius:999px;
padding:3px 9px;font-size:12px}
</style></head><body>
<header>
  <h1>Оптимизатор · превью диалога</h1>
  <p>Сообщения бота MAX с кнопкой «Оптимизатор» в клавиатуре под «Хочу такое же
  под свой процесс». Сгенерировано локальным симулятором
  <code>tools/simulate_optimizer.py</code>, без обращения к API.</p>
</header>
""" + "\n".join(sections) + "</body></html>\n"


ADMIN_MARK = "ПРАВКА-ИЗ-АДМИНКИ"


def numbers_only(value):
    """Числовая тень расчёта: подписи и названия выброшены, цифры остались.

    Нужно для сверки «до правки / после правки»: админка меняет слова, и если
    вместе со словами сдвинулась цифра — значит редактор полез в то, что должно
    остаться в коде (ставкам, часам и факторам в оверлее не бывать).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, dict):
        return {key: numbers_only(item) for key, item in value.items()}
    if isinstance(value, list):
        return [numbers_only(item) for item in value]
    return None


def fingerprint(transcript, session):
    """Отпечаток прогона: куда кликали, какие клавиатуры показывали, что посчитали."""
    clicks = [item["payload"] for item in transcript if item["payload"]]
    keyboards = [[[button.get("payload") or button.get("url") or button.get("text")
                   for button in row] for row in (item.get("raw") or [])]
                 for item in transcript if item.get("raw")]
    maths = {key: numbers_only(session.get(key))
             for key in ("calc", "sav", "roles", "ops", "tools")}
    return {"clicks": clicks, "keyboards": keyboards, "maths": maths,
            "messages": len(transcript)}


def admin_overlay():
    """content.json таким, каким его отдаёт админка после правки формулировок.

    Правим по одному полю каждого сорта — текст, подпись кнопки, сфера, роль,
    операция, инструмент, шаг чек-листа — и проверяем две стороны одной медали.
    Поля из `shown` обязаны появиться в диалоге или в отчёте: если не появились,
    админка дарит владельцу редактор для строки, которую бот не показывает. Поля
    из `silent` обязаны НЕ появляться: они лежат в content.json как справочник
    расчёта, и мету их держит meta.internal — админка гасит такие поля и пишет,
    что на сообщения они не влияют.

    Берём те сферы, роли и инструменты, по которым сценарии действительно
    кликают, иначе «правка не появилась» означала бы лишь то, что сценарий туда
    не заглядывал. Числа (ставки, часы, факторы, бюджеты) не трогаем: админка их
    не показывает, а чекер следит, чтобы они не поехали сами.
    """
    with open(content.CONTENT_FILE, encoding="utf-8") as handle:
        tree = json.load(handle)
    shown, silent = {}, {}
    cat = tree["catalog"]

    def sphere_of(sid):
        return next(s for s in cat["spheres"] if s["id"] == sid)

    def edit_text(path, value, note):
        node = tree["texts"]
        for key in path.split(".")[:-1]:
            node = node[key]
        node[path.split(".")[-1]] = value
        shown[note] = value

    edit_text("intro", f"{ADMIN_MARK}: шесть вопросов — и расчёт готов.",
              "текст шага 1")
    edit_text("stale", f"{ADMIN_MARK}: кнопка из прошлого расчёта.",
              "реплика про просроченный шаг")
    edit_text("checklist.title", f"<b>{ADMIN_MARK}: порядок внедрения</b>",
              "заголовок чек-листа")
    tree["buttons"]["final"]["pdf"] = f"📄 {ADMIN_MARK}-отчёт"
    shown["кнопка полного отчёта"] = tree["buttons"]["final"]["pdf"]

    # Справочные поля: участвуют в расчёте или в логике, но в сообщениях не видны.
    # Именно их перечисляет meta.internal — админка гасит такие поля с пояснением.
    cat["self_role"]["label"] = f"{ADMIN_MARK}-руковожу-сам"
    silent["роль «только я»"] = cat["self_role"]["label"]
    sphere_of("ecom")["blurb"] = f"{ADMIN_MARK}-описание-сферы"
    silent["описание сферы"] = sphere_of("ecom")["blurb"]

    sphere_of("ecom")["label"] = f"{ADMIN_MARK}-сфера"
    shown["кнопка сферы"] = sphere_of("ecom")["label"]
    op = sphere_of("ecom")["ops"][0]
    op["label"] = f"{ADMIN_MARK}-операция"
    shown["операция в рутине"] = op["label"]
    op["detail"] = f"{ADMIN_MARK}-пояснение к операции"
    shown["пояснение операции"] = op["detail"]
    role = sphere_of("it")["roles"][0]
    role["label"] = f"{ADMIN_MARK}-специальность"
    shown["специальность в команде"] = role["label"]
    tool = next(t for t in cat["tools"] if t["id"] == "n8n")
    tool["label"] = f"{ADMIN_MARK}-инструмент"
    shown["инструмент оптимизации"] = tool["label"]
    tool["what"] = f"{ADMIN_MARK}-что делает инструмент"
    shown["назначение инструмента"] = tool["what"]
    tool["pilot"] = f"{ADMIN_MARK}-пилот инструмента"
    shown["пилот инструмента"] = tool["pilot"]
    cat["checklist"][0]["short"] = f"{ADMIN_MARK}-шаг в чате"
    shown["короткий шаг чек-листа"] = cat["checklist"][0]["short"]
    cat["checklist"][1]["title"] = f"{ADMIN_MARK}-шаг в отчёте"
    shown["подробный шаг чек-листа"] = cat["checklist"][1]["title"]
    cat["checklist"][2]["body"] = f"{ADMIN_MARK} пояснение шага внедрения"
    shown["тело шага внедрения"] = cat["checklist"][2]["body"]
    return tree, shown, silent


@contextlib.contextmanager
def overlay(tree):
    """Подменить content.json на время: ровно то, что делает следующий круг Actions."""
    path = os.path.join(tempfile.mkdtemp(), "content.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(tree, handle, ensure_ascii=False)
    saved = content.CONTENT_FILE
    content.CONTENT_FILE = path
    content.reload()
    opt.refresh_catalog()
    try:
        yield
    finally:
        content.CONTENT_FILE = saved
        content.reload()
        opt.refresh_catalog()


def run_overlay(names, verbose=False):
    """Второй заход теми же сценариями поверх оверлея + сверка с боевым прогоном."""
    problems = []
    with overlay(_TREE):
        transcripts = [(name,) + play(name, SCENARIOS[name], verbose=verbose)
                       for name in names]

        # 1. Пути и цифры не изменились.
        for name, transcript, session in transcripts:
            here = fingerprint(transcript, session)
            there = _FINGERPRINTS[name]
            if here["clicks"] != there["clicks"]:
                problems.append(f"{name}: после правки изменился путь по кнопкам — "
                                "payload уехал вместе с подписью")
            if here["maths"] != there["maths"]:
                problems.append(f"{name}: после правки сдвинулись цифры расчёта")
            if here["keyboards"] != there["keyboards"]:
                problems.append(f"{name}: после правки изменился состав кнопок "
                                "(не только надписи)")
        extra, _ = validate(transcripts)
        problems += [f"оверлей: {p}" for p in extra]

        # 2. Правки действительно дошли до пользователя: часть строк видна в
        # чате, часть — только в отчёте (PDF и заявке владельцу), поэтому сверяем
        # оба места. Маркер, который не появился нигде, означает «админ правит
        # поле, которое бот не показывает».
        chunks = [item["text"] + " " + item["buttons"]
                  for _, transcript, _ in transcripts for item in transcript]
        for _, _, session in transcripts:
            calc, sav = session.get("calc"), session.get("sav")
            if calc and sav:
                chunks.append(json.dumps(opt.report_data(session, calc, sav),
                                         ensure_ascii=False))
        blob = "\n".join(chunks)
        for note, marker in _SHOWN.items():
            if marker not in blob:
                problems.append(f"оверлей: правка «{note}» не появилась ни в чате, "
                                "ни в отчёте — админка редактирует строку, которую "
                                "бот не показывает")
        for note, marker in _SILENT.items():
            if marker in blob:
                problems.append(f"оверлей: справочное поле «{note}» вылезло в "
                                "сообщения — уберите его из meta.internal")
        for warning in content.warnings():
            problems.append(f"оверлей: {warning}")

    print("\n--- репетиция правки из админки ---")
    print(f"сценариев перезапущено: {len(names)} · видно в диалоге/отчёте: "
          f"{len(_SHOWN)} · справочных (не видны намеренно): {len(_SILENT)}")
    return problems


_TREE = {}
_SHOWN = {}
_SILENT = {}
_FINGERPRINTS = {}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--no-preview", action="store_true")
    parser.add_argument("--overlay", action="store_true",
                        help="прогнать сценарии ещё раз поверх правленого content.json "
                             "и сравнить путь, клавиатуры и цифры с боевым прогоном")
    args = parser.parse_args()

    names = [args.scenario] if args.scenario else list(SCENARIOS)
    transcripts = [(name,) + play(name, SCENARIOS[name], verbose=not args.quiet)
                   for name in names]
    problems, payloads = validate(transcripts)

    if args.overlay:
        # Снимки боевого прогона и оверлей готовятся до подмены файла: run_overlay
        # читает их уже из глобалов, когда контент подменён.
        global _TREE, _SHOWN, _SILENT, _FINGERPRINTS
        _FINGERPRINTS = {name: fingerprint(transcript, session)
                         for name, transcript, session in transcripts}
        _TREE, _SHOWN, _SILENT = admin_overlay()
        problems += run_overlay(names, verbose=False)

    print("\n--- проверки ---")
    print(f"сценариев: {len(transcripts)} · payload'ов: {len(payloads)} · "
          f"макс. длина: {max((len(p) for p in payloads), default=0)}")
    hours_check = []
    for name, _, session in transcripts:
        calc, sav = session.get("calc"), session.get("sav")
        if calc and sav:
            hours_check.append((name, calc["hours"], sav["saved_hours"],
                                calc["cost_month"], sav["saved_money"],
                                sav["payback"]))
    for name, hours, saved, cost, saved_money, payback in hours_check:
        share = saved / hours if hours else 0
        payback_text = f" · окупаемость {payback:.1f} мес" if payback else ""
        print(f"{name}: рутина {hours:.0f} ч/мес = {cost:,.0f} ₽/мес → "
              f"экономия {saved:.0f} ч ({share * 100:.0f}%) и "
              f"{saved_money:,.0f} ₽/мес{payback_text}".replace(",", " "))
    for problem in problems:
        print("⚠️ ", problem)
    if not problems:
        print("✅ инварианты в порядке")

    if args.json:
        print(json.dumps({name: transcript for name, transcript, _ in transcripts},
                         ensure_ascii=False, indent=2))
    if not args.no_preview and not args.scenario:
        with open(PREVIEW_PATH, "w", encoding="utf-8") as handle:
            handle.write(render_html(transcripts))
        print(f"превью: {PREVIEW_PATH}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
