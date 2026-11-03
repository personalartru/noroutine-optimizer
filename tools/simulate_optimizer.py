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

Превью для глаз: optimizer-preview.html (открыть в браузере).
"""
import argparse
import html as html_mod
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PREVIEW_PATH = os.path.join(ROOT, "optimizer-preview.html")

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
}


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
        if opt.expects_text(session) and texts:
            text = texts.pop(0)
            push("user", text)
            session, messages = opt.dispatch_text(text, session)
        elif clicks:
            payload = clicks.pop(0)
            push("user", f"нажал «{labels.get(payload, payload)}»", payload=payload)
            session, messages = opt.dispatch(payload, session)
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


HAPPY = ("solo", "team", "fire", "again")
FAILURE_PHRASES = ("не распознана", "нет в списке", "не понял", "Не понял",
                   "нужно число", "Нужно число", "Traceback")
INTRO = opt.text_intro().splitlines()[0][:24]


def validate(transcripts):
    problems, payloads = [], set()
    for name, transcript, session in transcripts:
        if name in HAPPY and session["step"] != "done":
            problems.append(f"{name}: не дошёл до финала (шаг {session['step']})")
        if name == "again":
            restarts = sum(1 for item in transcript
                           if item["role"] == "bot" and item["text"].startswith(INTRO))
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--no-preview", action="store_true")
    args = parser.parse_args()

    names = [args.scenario] if args.scenario else list(SCENARIOS)
    transcripts = [(name,) + play(name, SCENARIOS[name], verbose=not args.quiet)
                   for name in names]
    problems, payloads = validate(transcripts)

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
