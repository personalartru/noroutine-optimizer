"""Интеграционная проверка «Оптимизатора» в max_bot.py без сети.

Ставит заглушку на MAX API (`call`) и прогоняет пользовательский путь через
настоящие обработчики: «bot_started» → шаг 1 с семью сферами → шаги по кнопкам
→ число сообщением → финал → заявка владельцу → отмена, чужая кнопка и
просроченная сессия. Тексты проверяет simulate_optimizer.py, здесь — обвязка:
маршрутизацию callbacks, хранение шага, доставку в личку, поведение без
лички и то, что в новом боте нет лид-веток старого.

    python3 tools/check_max_bot.py
"""
import json
import os
import sys
import tempfile

WORKDIR = tempfile.mkdtemp(prefix="max-bot-test-")
os.chdir(WORKDIR)
os.environ.update({
    "MAX_BOT_TOKEN": "test-token",
    "MAX_ADMIN_CHAT_ID": "999",
    "MAX_OFFSET_FILE": os.path.join(WORKDIR, ".max_offset"),
    "MAX_DIALOGS_FILE": os.path.join(WORKDIR, ".max_dialogs.json"),
    "MAX_OPTIMIZER_FILE": os.path.join(WORKDIR, ".max_optimizer.json"),
})

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import max_bot  # noqa: E402
import optimizer  # noqa: E402

SENT = []


def fake_call(method, path, query=None, body=None):
    SENT.append({"method": method, "path": path, "query": query or {},
                 "body": body or {}})
    if path == "me":
        return {"username": "se14730534_1_bot", "name": "Оптимизатор"}
    return {"ok": True}


max_bot.call = fake_call

USER = 123456
DIALOG = "dialog-123456"
USER2 = 654321              # второй пользователь: проверки, которые портят сессию
DIALOG2 = "dialog-654321"
ADMIN = "999"

FLOW = ["opt:s:it", "opt:m:team", "opt:r:0", "opt:r:1", "opt:r:ok",
        "opt:c:8", "opt:c:4",
        "opt:o:ticket", "opt:o:metrics", "opt:o:ok",
        "opt:t:chat", "opt:t:n8n", "opt:t:ok", "opt:t:plan"]


def reset():
    del SENT[:]


def messages_to(chat=DIALOG):
    return [s for s in SENT if s["path"] == "messages"
            and s["query"].get("chat_id") == chat]


def texts_to(chat=DIALOG):
    return [m["body"]["text"] for m in messages_to(chat)]


def buttons_of(message):
    for attachment in message["body"].get("attachments", []):
        if attachment["type"] == "inline_keyboard":
            return attachment["payload"]["buttons"]
    return []


def all_buttons(chat=DIALOG):
    return [b for m in messages_to(chat) for row in buttons_of(m) for b in row]


def session_of(user=USER):
    return json.load(open(os.environ["MAX_OPTIMIZER_FILE"])).get(str(user))


def click(payload, user=USER, callback_id="cb"):
    max_bot.handle_callback({"payload": payload, "callback_id": callback_id,
                             "user": {"user_id": user, "name": "Павел",
                                      "username": "personartru"}})


def check(label, condition, detail=""):
    print(f"{'✅' if condition else '❌'} {label}" + (f" — {detail}" if detail else ""))
    return bool(condition)


results = []

# 1. «bot_started»: бот сам открывает шаг 1, лишних тапов нет
reset()
max_bot.handle_event({"update_type": "bot_started",
                      "user": {"user_id": USER, "name": "Павел"},
                      "chat_id": DIALOG})
texts = texts_to()
results.append(check("открыт шаг «сфера» с приветствием",
                     any("Оптимизатор — бот" in t for t in texts)
                     and any("Шаг 1 из 6" in t for t in texts), f"{len(texts)} сообщ."))
results.append(check("семь сфер на кнопках",
                     sum(1 for b in all_buttons() if b.get("payload", "").startswith("opt:s:")) == 7))
results.append(check("диалог запомнен (chat_id ≠ user_id)",
                     max_bot.dialog_for(USER) == DIALOG))
results.append(check("шаг сохранён в файл состояния",
                     (session_of() or {}).get("step") == "sphere",
                     str((session_of() or {}).get("step"))))

# 2. Полный путь по кнопкам до финала
reset()
for payload in FLOW:
    click(payload)
texts = texts_to()
results.append(check("есть часы и стоимость рутины", any("Часы рутины" in t for t in texts)))
results.append(check("есть экономия", any("Экономия" in t for t in texts)))
results.append(check("есть чек-лист внедрения", any("Чек-лист внедрения" in t for t in texts)))
results.append(check("есть бюджет и дисклеймер",
                     any("Примерный бюджет" in t for t in texts)
                     and any("Дисклеймер" in t for t in texts)))
results.append(check("в финале кнопка заявки",
                     any(b.get("payload") == "opt:fb" for b in all_buttons())))

# 3. Финальная клавиатура ведёт в канал: callback чужого бота недопустим
reset()
for payload in FLOW + ["opt:fb"]:
    click(payload)
max_bot.handle_text(USER, DIALOG, "Павел", "personartru",
                    "Хочу автоматизировать тикеты, 8 человек")
msgs = [m for m in messages_to()
        if any(b.get("payload") == "opt:again" for row in buttons_of(m) for b in row)]
flat = [b for m in msgs for row in buttons_of(m) for b in row]
results.append(check("есть «Оптимизатор заново» (callback)",
                     any(b.get("payload") == "opt:again" for b in flat)))
links = [b for b in flat if b.get("type") == "link"]
results.append(check("на финале одна ссылка — «Канал про автоматизацию»",
                     len(links) == 1 and links[0].get("text") == "Канал про автоматизацию",
                     str([b.get("text") for b in links])))
results.append(check("ссылка ведёт на канал noroutine",
                     links and links[0].get("url") == "https://max.ru/channel_noroutine",
                     str([b.get("url") for b in links])))
results.append(check("ссылки на старого лид-бота в текстах не осталось",
                     not any("se14730534_bot" in t for t in texts_to())
                     and not any("se14730534_bot" in (b.get("url") or "") for b in flat)))
results.append(check("в новом боте нет чужих callback-payload'ов",
                     not any(b.get("payload") == "want_task" for b in flat)))

# 4. Заявка ушла владельцу, сессия закрыта
admin = messages_to(ADMIN)
results.append(check("заявка доставлена владельцу", len(admin) == 1))
if admin:
    body = admin[0]["body"]["text"]
    results.append(check("в заявке есть текст, профиль и черновик расчёта",
                         "автоматизировать тикеты" in body
                         and "[Оптимизатор]" in body
                         and "IT / продукт" in body
                         and f"max.ru/u/{USER}" in body))
results.append(check("сессия очищена после заявки", session_of() is None))

# 5. Регресс на прод-сбой: на шаге количества кнопки обязаны быть с type
reset()
max_bot.remember_dialog(USER2, DIALOG2)
for payload in ("opt:start", "opt:s:ecom", "opt:m:team", "opt:r:0", "opt:r:ok"):
    click(payload, user=USER2, callback_id="cb2")
rows = [row for m in messages_to(DIALOG2) for row in buttons_of(m)
        if any(b.get("payload") == "opt:c:1" for b in row)]
labels = [b["text"] for row in rows for b in row]
results.append(check("есть кнопки 1–5 на вопросе о количестве",
                     all(str(i) in labels for i in range(1, 6)), str(labels)))
results.append(check("у всех кнопок клавиатуры есть type",
                     all(b.get("type") in ("callback", "link")
                         for m in messages_to(DIALOG2) for row in buttons_of(m) for b in row)))

# 6. Свободный текст на шаге «своё число» не превращается в заявку
reset()
for payload in ("opt:start", "opt:s:it", "opt:m:team", "opt:r:0", "opt:r:1",
                "opt:r:ok", "opt:c:3"):
    click(payload)
click("opt:c:x")
max_bot.handle_text(USER, DIALOG, "Павел", "personartru", "42")
results.append(check("число не стало лидом (заявок владельцу: 0)",
                     not messages_to(ADMIN)))
sess = session_of() or {}
results.append(check("количество по второй роли принято",
                     (sess.get("last_role") or "").endswith("× 42"),
                     str(sess.get("last_role", ""))))
results.append(check("первая роль осталась в команде",
                     any(r["label"].startswith("L1-поддержка") and r["count"] == 3
                         for r in sess.get("roles", [])), str(sess.get("roles"))))

# 6b. Число сообщением прямо на шаге с кнопками 1–5: текст шага обещает
# «или число сообщением», значит «Своё число» нажимать не обязательно.
reset()
for payload in ("opt:start", "opt:s:it", "opt:m:team", "opt:r:0", "opt:r:ok"):
    click(payload)
results.append(check("шаг — вопрос о количестве с кнопками",
                     (session_of() or {}).get("step") == "count",
                     str((session_of() or {}).get("step"))))
max_bot.handle_text(USER, DIALOG, "Павел", "personartru", "2")
sess = session_of() or {}
results.append(check("число с клавиатуры принято без «Своё число»",
                     (sess.get("last_role") or "").endswith("× 2"), str(sess)))
results.append(check("после количества шаг уехал с «count»",
                     sess.get("step") != "count", str(sess.get("step"))))
results.append(check("напечатанная цифра не стала заявкой",
                     not messages_to(ADMIN)))
reset()
for payload in ("opt:start", "opt:s:it", "opt:m:team", "opt:r:0", "opt:r:ok"):
    click(payload)
max_bot.handle_text(USER, DIALOG, "Павел", "personartru", "пару человек")
results.append(check("текст без цифры оставляет на шаге количества",
                     (session_of() or {}).get("step") == "count"))
results.append(check("текст без цифры получает внятную подсказку",
                     any("Нужно число" in t for t in texts_to())))

# 7. Отмена и перезапуск
reset()
click("opt:start")
max_bot.handle_text(USER, DIALOG, "Павел", "personartru", "отмена")
results.append(check("отмена закрывает сессию", session_of() is None))
reset()
for payload in ("opt:start", "opt:s:it", "opt:m:self"):
    click(payload)
max_bot.handle_text(USER, DIALOG, "Павел", "personartru", "/start")
results.append(check("/start в середине сбрасывает к шагу 1",
                     (session_of() or {}).get("step") == "sphere",
                     str((session_of() or {}).get("step"))))

# 8. Незнакомое сообщение без сессии — подсказка, а не молчание и не заявка
reset()
max_bot.handle_event({"update_type": "message_created",
                      "message": {"body": {"text": "привет"},
                                  "recipient": {"chat_id": DIALOG,
                                                "chat_type": "dialog"},
                                  "sender": {"user_id": USER, "name": "Павел"}}})
texts = texts_to()
results.append(check("подсказка с кнопкой «Начать расчёт»",
                     any("шесть коротких вопросов" in t for t in texts)
                     and any(b.get("payload") == "opt:start" for b in all_buttons())))
results.append(check("случайный текст не стал заявкой", not messages_to(ADMIN)))

# 9. Кнопка не из этого бота (payload без opt:) — клик не теряется
reset()
click("want_task", callback_id="cb9")
answers = [s for s in SENT if s["path"] == "answers"]
results.append(check("чужой payload получил ответ на callback", len(answers) == 1))

# 10. Личка недоступна → ответ на callback, а не потерянный шаг
reset()
_real_send_direct = max_bot.send_direct
max_bot.send_direct = lambda *a, **kw: False
click("opt:start", user=777, callback_id="cb10")
max_bot.send_direct = _real_send_direct
results.append(check("без доступной лички клик не теряется",
                     len([s for s in SENT if s["path"] == "answers"]) >= 1))

# 11. Просроченная сессия не перехватывает обычный текст
store = json.load(open(os.environ["MAX_OPTIMIZER_FILE"]))
sess = optimizer.new_session()
sess["step"] = "feedback"
sess["updated"] -= optimizer.SESSION_TTL + 60
store[str(USER)] = sess
json.dump(store, open(os.environ["MAX_OPTIMIZER_FILE"], "w"), ensure_ascii=False)
reset()
max_bot.handle_text(USER, DIALOG, "Павел", "personartru", "напишите мне по автоматизации")
results.append(check("просроченная сессия отдала подсказку, а не заявку",
                     not messages_to(ADMIN) and any("Оптимизатор" in t for t in texts_to())))

# 12. Тексты: без имени владельца и без обещания «как на сайте»
all_text = "\n".join(texts_to()) + json.dumps(SENT, ensure_ascii=False)
results.append(check("в текстах нет имени владельца", "Павлу" not in all_text))
results.append(check("нет ссылки на калькулятор сайта",
                     "калькуляторе на сайте" not in all_text))

# 13. Запуск без токена честно говорит о проблеме, а не крутит пустой цикл
saved, max_bot.TOKEN = max_bot.TOKEN, ""
results.append(check("без MAX_BOT_TOKEN main() возвращает 2", max_bot.main() == 2))
max_bot.TOKEN = saved

print(f"\n{sum(results)}/{len(results)} проверок пройдено")
sys.exit(0 if all(results) else 1)
