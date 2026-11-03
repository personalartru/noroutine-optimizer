"""Фаззинг стейт-машины и обход всех сфер до финала.

Две задачи. Первая: случайные payload'ы и тексты (включая битые, чужие и
пустые) не должны ронять dispatch/dispatch_text и не должны рождать
некорректные клавиатуры — MAX молча не показывает клавиатуру без `type`, а
одно исключение в проде стоит молчаливого диалога. Вторая: из каждой сферы
полный путь по кнопкам доходит до `rollout`/финала.

    python3 tools/fuzz_optimizer.py [--runs 600] [--seed 7]
"""
import argparse
import random
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import optimizer as opt  # noqa: E402

ARG_POOL = ["", "0", "1", "2", "3", "8", "x", "ok", "clear", "more", "calc",
            "fire", "plan", "self", "team", "nope", "-1", "999",
            "ticket", "feedback", "metrics", "release", "chat", "agent", "n8n",
            "<script>", "' OR 1=1 --", "a" * 40]
KINDS = list(opt.HANDLERS) + ["start", "again", "reset", "cancel", "fb", "zzz", ""]
TEXTS = ["42", "примерно 12", "", "   ", "не число", "-5", "0", "999999999",
         "хочу автоматизировать тикеты", "<b>жирный</b>", "a" * 2000,
         "<i>незакрытый", "стоп", "отмена"]


def shape_problems(messages):
    """Те же инварианты клавиатуры, что проверяет simulate_optimizer."""
    problems = []
    for message in messages:
        text = message.get("text") or ""
        if len(text) > opt.TEXT_CHUNK:
            problems.append(f"сообщение {len(text)} символов — резка не сработала")
        if text and not text.strip():
            problems.append("ушло сообщение из одних пробелов")
        for tag in ("b", "i"):
            if text.count(f"<{tag}>") != text.count(f"</{tag}>"):
                problems.append(f"незакрытый <{tag}> в «{text[:40]}»")
        buttons = message.get("buttons") or []
        if len(buttons) > 30:
            problems.append(f"{len(buttons)} рядов — MAX допускает 30")
        for row in buttons:
            if len(row) > 7:
                problems.append(f"в ряду {len(row)} кнопок — MAX допускает 7")
            for button in row:
                kind = button.get("type")
                if kind not in ("callback", "link"):
                    problems.append(f"у кнопки «{button.get('text')}» нет type")
                elif kind == "callback" and not button.get("payload"):
                    problems.append(f"callback без payload — «{button.get('text')}»")
                elif kind == "link" and not str(button.get("url", "")).startswith("http"):
                    problems.append(f"link без url — «{button.get('text')}»")
                if len(button.get("text") or "") > 64:
                    problems.append(f"подпись кнопки длинная: «{button['text'][:40]}»")
    return problems


def fuzz(runs, rng):
    problems = []
    for _ in range(runs):
        session = opt.new_session()
        for _ in range(rng.randint(1, 14)):
            if rng.random() < 0.25:
                payload = rng.choice(["", "want_task", "check_sub", "opt",
                                      "opt:", "opt:s", f"opt:{rng.choice(KINDS)}",
                                      f"opt:{rng.choice(KINDS)}:{rng.choice(ARG_POOL)}"])
                session, messages = opt.dispatch(payload, session)
            else:
                session, messages = opt.dispatch_text(rng.choice(TEXTS), session)
            for problem in shape_problems(messages):
                problems.append(problem)
            if len(problems) > 5:
                return problems
        if session.get("step") == "feedback" and rng.random() < 0.5:
            session, messages = opt.dispatch_text(rng.choice(TEXTS), session)
            problems.extend(shape_problems(messages))
    return problems


def sweep():
    """Из каждой сферы — полный путь по кнопкам: сфера → роли → операции →
    инструменты → economy → rollout. Возвращает список проблем."""
    problems = []
    for sphere in opt.SPHERES:
        session = opt.new_session()
        chain = [f"opt:s:{sphere['id']}", "opt:m:team", "opt:r:0", "opt:r:ok", "opt:c:5"]
        chain += [f"opt:o:{op['id']}" for op in sphere["ops"][:2]] + ["opt:o:ok"]
        chain += [f"opt:t:{tool['id']}" for tool in opt.ALL_TOOLS[:2]] + ["opt:t:ok"]
        chain += ["opt:t:plan"]
        steps = []
        for payload in chain:
            session, messages = opt.dispatch(payload, session)
            steps.append(session.get("step"))
            problems.extend(f"{sphere['id']}: {p}" for p in shape_problems(messages))
            if session.get("step") == "feedback":
                break
        if session.get("step") not in ("rollout", "feedback", "done"):
            problems.append(f"{sphere['id']}: не дошёл до плана (шаги: {steps})")
        elif session.get("calc", {}).get("hours", 0) <= 0:
            problems.append(f"{sphere['id']}: расчёт пуст")
    return problems


def blank_lines(rng, rounds=400):
    """Резка не имеет права трогать вёрстку: ведущие пустые строки, целые абзацы
    и лимит сообщения. Случайные тексты ловят границы, которых нет в файле."""
    problems = []
    for _ in range(rounds):
        lead = "\n" * rng.randint(0, 3)
        blocks = ["".join(rng.choice("абвгде .,!-") for _ in range(rng.randint(1, 1400))).strip()
                  for _ in range(rng.randint(1, 6))]
        text = lead + "\n\n".join(b if b else "х" for b in blocks)
        parts = opt.chunk(text)
        if not parts:
            problems.append(f"резка отдала пустой список для текста {len(text)}")
            continue
        if any(not p for p in parts):
            problems.append("резка отдала пустое сообщение")
        if any(len(p) > opt.TEXT_CHUNK for p in parts):
            problems.append(f"часть {max(len(p) for p in parts)} символов — "
                            f"лимит {opt.TEXT_CHUNK}")
        if len(lead) and (len(parts[0]) - len(parts[0].lstrip("\n"))) != len(lead):
            problems.append(f"резка съела ведущие пустые строки ({len(lead)})")
        budget = opt.TEXT_CHUNK - len(lead)
        if all(len(b) <= budget for b in text[len(lead):].split("\n\n")) and \
                "\n\n".join(parts) != text:
            problems.append("резка переставила или потеряла абзацы")
    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=600)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    problems = (fuzz(args.runs, rng) + sweep() + blank_lines(rng))
    print(f"фаззинг: {args.runs} прогонов · сфер в обходе: {len(opt.SPHERES)}")
    if problems:
        for problem in problems[:10]:
            print("❌", problem)
        return 1
    print("✅ исключений нет, клавиатуры в порядке, все сферы доходят до rollout, "
          "резка бережёт пустые строки")
    return 0


if __name__ == "__main__":
    sys.exit(main())
