"""Интеграционная проверка «Оптимизатора» в max_bot.py без сети.

Ставит заглушку на MAX API (`call`) и прогоняет пользовательский путь через
настоящие обработчики: «bot_started» → шаг 1 с семью сферами → шаги по кнопкам
→ число сообщением → финал → заявка владельцу → отмена, чужая кнопка и
просроченная сессия. Тексты проверяет simulate_optimizer.py, здесь — обвязка:
маршрутизацию callbacks, хранение шага, доставку в личку, поведение без
лички, горячую подгрузку content.json из main и то, что в новом боте нет
лид-веток старого.

    python3 tools/check_max_bot.py
"""
import json
import os
import sys
import tempfile
import time

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


def fake_call(method, path, query=None, body=None, token=None):
    SENT.append({"method": method, "path": path, "query": query or {},
                 "body": body or {}, "token": token or max_bot.TOKEN})
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
    """Сообщения адресату: MAX принимает либо chat_id, либо user_id."""
    return [s for s in SENT if s["path"] == "messages"
            and (s["query"].get("chat_id") == chat
                 or s["query"].get("user_id") == chat)]


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


# Заглушка загрузки файлов: `upload_file` ходит в MAX прямым `requests.post`,
# минуя `call`. Ставим её до первого же клика — иначе секции, которые шлют
# заявку с PDF, ушли бы в сеть настоящим тестовым токеном.
UP = []


class Resp:
    def __init__(self, payload, code=200):
        self._payload = payload
        self.status_code = code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def fake_post(url, params=None, headers=None, files=None, timeout=None, **_kw):
    UP.append({"url": url, "params": params, "auth": (headers or {}).get("Authorization"),
               "name": (files or {}).get("data", ("",))[0]})
    if url.endswith("/uploads"):
        return Resp({"url": "https://upload.example/put/42"})
    return Resp({"token": "file-token-42"})


class FakeRequests:
    RequestException = RuntimeError

    @staticmethod
    def post(url, **kwargs):
        return fake_post(url, **kwargs)


real_requests = max_bot.requests
max_bot.requests = FakeRequests

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
results.append(check("после заявки шаг закрыт, но расчёт жив для кнопки PDF",
                     (session_of() or {}).get("step") == "done"
                     and bool((session_of() or {}).get("calc"))
                     and bool((session_of() or {}).get("sav")),
                     str((session_of() or {}).get("step"))))

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

# 12b. Куда уходит заявка: чат лид-бота → свой диалог → очередь повторов
saved_lead = (max_bot.LEAD_BOT_TOKEN, max_bot.LEAD_ADMIN_USER_ID,
              max_bot.LEAD_ADMIN_DIALOG_ID)
saved_admin = (max_bot.ADMIN_CHAT_ID, max_bot.ADMIN_DIALOG_ID)
reset()
max_bot.LEAD_BOT_TOKEN, max_bot.LEAD_ADMIN_USER_ID = "lead-token", "999"
max_bot.LEAD_ADMIN_DIALOG_ID = ""
ok = max_bot.send_admin_lead("📩 Расчёт из «Оптимизатора»\nтест")
results.append(check("с LEAD_MAX_BOT_TOKEN заявка идёт его токеном в личку владельца",
                     ok and len(SENT) == 1
                     and SENT[0]["query"] == {"user_id": "999"}
                     and SENT[0]["token"] == "lead-token"))

# если задан точный chat_id диалога с лид-ботом — он важнее user_id
max_bot.LEAD_ADMIN_DIALOG_ID = "485434975"
reset()
ok = max_bot.send_admin_lead("заявка по chat_id")
results.append(check("LEAD_MAX_ADMIN_DIALOG_ID пробуют раньше, чем user_id",
                     ok and len(SENT) == 1
                     and SENT[0]["query"] == {"chat_id": "485434975"}
                     and SENT[0]["token"] == "lead-token"))
max_bot.LEAD_ADMIN_DIALOG_ID = ""

# без токена лид-бота — в запомненный диалог владельца, а не на голый user_id
max_bot.LEAD_BOT_TOKEN, max_bot.LEAD_ADMIN_USER_ID = "", ""
json.dump({"999": "583859747"}, open(os.environ["MAX_DIALOGS_FILE"], "w"))
reset()
ok = max_bot.send_admin_lead("заявка в свой диалог")
chats = [s["query"].get("chat_id") for s in SENT if s["path"] == "messages"]
results.append(check("без него пишет в диалог владельца, а не на user_id (404 fix)",
                     ok and chats == ["583859747"]))

# кэш пуст и диалог не запомнен — id владельца уходят параметром user_id,
# а не chat_id (chat_id=60669315 в MAX = 404 chat.not.found)
os.remove(os.environ["MAX_DIALOGS_FILE"])
max_bot.ADMIN_CHAT_ID = "60669315"
max_bot.ADMIN_DIALOG_ID = ""
reset()
ok = max_bot.send_admin_lead("холодный кэш")
results.append(check("без запомненного диалога id владельца уходит параметром user_id",
                     ok and SENT[0]["query"] == {"user_id": "60669315"}))
(max_bot.ADMIN_CHAT_ID, max_bot.ADMIN_DIALOG_ID) = saved_admin

# все маршруты умерли — текст не теряется, а ждёт повтора
real_call = max_bot.call


def dead_call(method, path, query=None, body=None, token=None):
    raise RuntimeError("POST messages: HTTP 404 {\"code\":\"chat.not.found\"}")


max_bot.call = dead_call
reset()
ok = max_bot.send_admin_lead("утраченная заявка")
pending_path = max_bot.ADMIN_PENDING_FILE
queued = (json.load(open(pending_path, encoding="utf-8")).get("leads")
          if os.path.exists(pending_path) else [])
results.append(check("недоотправленная заявка легла в очередь повторов",
                     not ok and queued == ["утраченная заявка"]))

# канал ожил — цикл довозит её сам и очищает файл
max_bot.call = fake_call
reset()
delivered = max_bot.flush_admin_leads()
flushed = [s for s in SENT if s["path"] == "messages"]
results.append(check("при старте следующего круга очередь довозится и очищается",
                     delivered == 1 and len(flushed) == 1
                     and "утраченная заявка" in flushed[0]["body"]["text"]
                     and not os.path.exists(pending_path)))
max_bot.call = real_call
(max_bot.LEAD_BOT_TOKEN, max_bot.LEAD_ADMIN_USER_ID,
 max_bot.LEAD_ADMIN_DIALOG_ID) = saved_lead

# 12c. Тихий круг без событий не обрывает цепочку кэша: файлы состояния есть всегда
STATE_FILES = (max_bot.OFFSET_FILE, max_bot.DIALOGS_FILE, max_bot.SESSIONS_FILE,
               max_bot.ADMIN_PENDING_FILE)
for path in STATE_FILES:
    try:
        os.remove(path)
    except OSError:
        pass
max_bot.bootstrap_state()
results.append(check("после bootstrap_state все файлы состояния существуют",
                     all(os.path.exists(p) for p in STATE_FILES)))

# 12d. PDF: в чате короткий чек-лист, подробный — файлом, а если файла нет — текстом
# холодный кэш диалогов: личку пишем параметром user_id, а не chat_id=<user_id>
# (в MAX chat_id=60669315 = 404 chat.not.found, а user_id=60669315 доставляет)
reset()
click("opt:start")
cold = [s for s in SENT if s["path"] == "messages"]
results.append(check("при пустом кэше диалогов шаг уходит параметром user_id",
                     bool(cold) and all(s["query"] == {"user_id": USER} for s in cold),
                     str([s["query"] for s in cold][:2])))
max_bot.remember_dialog(USER, DIALOG)
max_bot.remember_dialog(USER2, DIALOG2)

short_chat = optimizer.text_checklist({"ops": []}, {"tools": []})
results.append(check("чек-лист в чате — одна строка на шаг и влезает в лимит",
                     len(short_chat) <= optimizer.TEXT_CHUNK
                     and short_chat.count("\n") >= 7
                     and any(ch in short_chat for ch in "🎯📝🔑📏📈")))
results.append(check("полная версия остаётся где взять",
                     "Чек-лист внедрения — подробно" in
                     optimizer.checklist_full_text({"ops": []}, {"tools": []})))

# заглушка на `requests` стоит в начале файла: загрузка файла в MAX не должна
# уходить в сеть даже в секциях, которым PDF не нужен (иначе проверка «без сети»
# начнёт дёргать platform-api2.max.ru тестовым токеном).
if not max_bot.report_pdf.available():
    print("::warning::нет fpdf2 или кириллического шрифта — `pip install "
          "-r requirements.txt`, иначе проверки PDF ниже падают справедливо")

# до этого шага сессия уже прошла полный путь, но её перезапускают другие
# проверки — проигрываем клик-путь заново, чтобы calc и sav точно были
max_bot.UPLOADS.clear()   # кэш загрузки тёплый от заявки выше — обнуляем счётчики
del UP[:]
reset()
for payload in FLOW:
    click(payload)

reset()
click("opt:p:report")
file_msgs = [m for m in SENT if m["path"] == "messages"
             and any(a.get("type") == "file" for a in m["body"].get("attachments", []))]
results.append(check("по кнопке файл загружается в MAX (uploads → multipart)",
                     len(UP) == 2 and UP[0]["url"].endswith("/uploads")
                     and UP[1]["url"] == "https://upload.example/put/42",
                     f"{len(UP)} загрузок"))
results.append(check("PDF уходит сообщением с attachment type=file",
                     len(file_msgs) == 1
                     and file_msgs[0]["body"]["attachments"][0]["payload"]["token"]
                     == "file-token-42"
                     and file_msgs[0]["query"].get("chat_id") == DIALOG,
                     f"{len(file_msgs)} файл-сообщ., "
                     + str([m["query"] for m in file_msgs])))
results.append(check("кнопки остались под файлом",
                     any(a.get("type") == "inline_keyboard"
                         for a in file_msgs[0]["body"].get("attachments", []))
                     if file_msgs else False))
results.append(check("имя файла читаемое",
                     UP[1]["name"].startswith("optimizator-")
                     and UP[1]["name"].endswith(".pdf")))

# второй клик того же отчёта не должен дважды грузить один и тот же файл
reset()
click("opt:p:report")
results.append(check("повтор той же кнопки не перезагружает файл", len(UP) == 2))

# Тот же отчёт, собранный заново, обязан дать ровно те же байты: транспорт
# кэширует загрузку по хэшу файла, а fpdf2 без фиксированной даты пишет в
# /CreationDate текущую секунду — и второй клик грузил бы дубликат.
state = session_of() or {}
report = optimizer.report_data(state, state.get("calc") or {}, state.get("sav") or {}) \
    if state.get("calc") and state.get("sav") else None
if report:
    time.sleep(1.1)
    again = optimizer.report_data(state, state["calc"], state["sav"])
    results.append(check("одинаковый отчёт даёт одинаковые байты PDF",
                         max_bot.report_bytes(report) == max_bot.report_bytes(again)))
else:
    results.append(check("одинаковый отчёт даёт одинаковые байты PDF", False,
                         "в сессии нет готового расчёта"))

# кнопка «Полный отчёт (PDF)» живёт и под закрытой заявкой: регресс на «сессия
# очищена» — клик давал «сначала пройдём шаги» вместо файла
del UP[:]
reset()
for payload in FLOW + ["opt:fb"]:
    click(payload)
max_bot.handle_text(USER, DIALOG, "Павел", "personartru",
                    "Хочу автоматизировать тикеты, 8 человек")
results.append(check("после заявки обычный текст не становится второй заявкой",
                     not max_bot.optimizer.expects_text(max_bot.session_for(USER) or {})))
reset()
click("opt:p:report")
after_lead = [m for m in SENT if m["path"] == "messages"
              and any(a.get("type") == "file"
                      for a in m["body"].get("attachments", []))]
results.append(check("PDF отдаётся и после отправленной заявки",
                     len(after_lead) == 1
                     and after_lead[0]["query"].get("chat_id") == DIALOG,
                     str([(m["query"], [a["type"] for a in m["body"].get("attachments", [])])
                         for m in after_lead])))

# если шрифта/библиотеки нет — человек получает текст, а не тишину
real_available = max_bot.report_pdf.available
max_bot.report_pdf.available = lambda: False
reset()
click("opt:p:report")
texts = texts_to()
results.append(check("без PDF-библиотеки чек-лист уезжает текстом",
                     any("Чек-лист внедрения — подробно" in t for t in texts)))
max_bot.report_pdf.available = real_available

# заявка владельцу приезжает с тем же PDF, загруженным его токеном
saved_lead = (max_bot.LEAD_BOT_TOKEN, max_bot.LEAD_ADMIN_USER_ID)
max_bot.LEAD_BOT_TOKEN, max_bot.LEAD_ADMIN_USER_ID = "lead-token", "999"
del UP[:]
reset()
max_bot.notify_admin(USER, "Павел", "Хочу автоматизировать тикеты", "personartru",
                     session_of() or {})
lead_msgs = [m for m in SENT if m["path"] == "messages"
             and m["token"] == "lead-token"]
results.append(check("к заявке для владельца приложен PDF",
                     len(lead_msgs) == 1
                     and any(a.get("type") == "file"
                             for a in lead_msgs[0]["body"].get("attachments", []))
                     and any(u["auth"] == "lead-token" for u in UP)))
max_bot.LEAD_BOT_TOKEN, max_bot.LEAD_ADMIN_USER_ID = saved_lead
max_bot.requests = real_requests

# 13. Запуск без токена честно говорит о проблеме, а не крутит пустой цикл
saved, max_bot.TOKEN = max_bot.TOKEN, ""
results.append(check("без MAX_BOT_TOKEN main() возвращает 2", max_bot.main() == 2))
max_bot.TOKEN = saved

# 14. Контент из админки: файл в репозитории валиден, а правка доходит до чата.
#
# Админка не трогает код — она коммитит content.json. Значит у прод-цикла два
# обязательства: файл не должен быть битым (иначе бот молча вернётся к
# код-дефолтам), и строка из файла должна оказаться в доставленном сообщении.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import content  # noqa: E402

wf = open(os.path.join(REPO, ".github", "workflows", "run-optimizer.yml"),
          encoding="utf-8").read()
results.append(check("контент в репозитории загружен без предупреждений",
                     content.warnings() == [], str(content.warnings()[:2])))
results.append(check("в боевом content.json нет чужих переменных",
                     content.problems() == [], str(content.problems()[:2])))
results.append(check("workflow перезапускает цикл на правку контента",
                     "content.json" in wf and "content.py" in wf))
src_bot = open(os.path.join(REPO, "max_bot.py"), encoding="utf-8").read()
results.append(check("цикл печатает, откуда взялся контент",
                     "source_summary()" in src_bot and "problems()" in src_bot))


def run_flow(user):
    """Пользовательский путь до финала: нужен дважды — до оверлея и после него."""
    del SENT[:]
    for payload in ["opt:start"] + FLOW:
        click(payload, user=user, callback_id="cb14")
    return max_bot.session_for(user) or {}


baseline = run_flow(USER2)

overlay_file = os.path.join(tempfile.mkdtemp(), "content.json")
MARKER = "ЗАМЕНА-ИЗ-АДМИНКИ: шесть вопросов и готовый расчёт"
with open(overlay_file, "w", encoding="utf-8") as fh:
    # Без {переменных}: подстановку проверяет check_content.py, здесь важно
    # увидеть именно строку админки в доставленном сообщении.
    json.dump({"schema": content.SCHEMA, "texts": {"intro": MARKER},
               "buttons": {"reset": "🔄 ЗАМЕНА-ЗАНОВО"}}, fh, ensure_ascii=False)

saved_file, content.CONTENT_FILE = content.CONTENT_FILE, overlay_file
content.reload()
optimizer.refresh_catalog()
del SENT[:]
max_bot.handle_bot_started({"user": {"user_id": USER2, "name": "Павел"},
                            "chat_id": DIALOG2})
results.append(check("правка текста из content.json доходит до чата",
                     any(MARKER in t for t in texts_to(DIALOG2)),
                     str(texts_to(DIALOG2)[:1])))
# «Сброс» появляется на шаге выбора людей: на шаге сфер кнопок-действий нет.
click("opt:s:it", user=USER2, callback_id="cb15")
results.append(check("правка подписи кнопки видна в клавиатуре",
                     content.label("reset") == "🔄 ЗАМЕНА-ЗАНОВО"
                     and any(b.get("payload") == "opt:reset"
                             and b["text"] == "🔄 ЗАМЕНА-ЗАНОВО"
                             for b in all_buttons(DIALOG2))))
after = run_flow(USER2)
results.append(check("расчёт не зависит от формулировок",
                     baseline.get("calc") == after.get("calc")
                     and baseline.get("sav") == after.get("sav"),
                     str([(baseline.get("calc") or {}).get("hours"),
                          (after.get("calc") or {}).get("hours")])))
results.append(check("оверлей не сломал клавиатуру (у всех кнопок есть type)",
                     all(b.get("type") in ("callback", "link")
                         for b in all_buttons(DIALOG2))))

content.CONTENT_FILE = saved_file
content.reload()
optimizer.refresh_catalog()
results.append(check("после съёма оверлея возвращаемся к боевому файлу",
                     content.text("intro") != MARKER and content.warnings() == []))

# Вёрстка пустыми строками: она отделяет строку бюджета от «Сопровождение:»,
# а дисклеймер — от призыва отправить заявку. Теряется на любом из шагов
# (файл → оверлей → подстановка → резка), и в чате получается склеенная строка.
del SENT[:]
final = run_flow(USER2)
delivered = texts_to(DIALOG2)
support_label = (content.get("texts.rollout.support") or "").split("{")[0]
disclaimer = content.text("rollout.disclaimer").rstrip()
cta = content.text("rollout.cta").lstrip("\n")
results.append(check("строка сопровождения начинается с пустой строки",
                     bool(support_label.startswith("\n"))
                     and any(t for t in delivered if support_label in t),
                     repr(support_label[:24])))
results.append(check("дисклеймер и призыв разделены пустой строкой",
                     any(f"{disclaimer}\n\n{cta}" in t for t in delivered)))
results.append(check("в доставленных текстах нет склеенных строк",
                     not any(disclaimer + cta in t for t in delivered),
                     next((t[:60] for t in delivered if disclaimer + cta in t), "")))
# То, что отрендерил код, должно прийти в MAX байт в байт.
from_rollout = []
for part in optimizer.text_rollout(final.get("sav") or {}):
    from_rollout.extend(optimizer.chunk(part))
lost = [p[:46] for p in from_rollout if p not in delivered]
results.append(check("финальные тексты уходят целиком, с пустыми строками",
                     bool(from_rollout) and not lost, "; ".join(lost[:2])))

# 15. Горячая подгрузка контента: правка админки доходит до чата в этом же цикле.
#
# Цикл стартует на content.json из checkout, а админка коммитит правку в main.
# Без подгрузки текст ждал бы конца цикла и слота очереди: по прод-пушам это
# 35 с / 71 с / 2 мин 22 с / 4 мин 46 с и до ~9,5 мин в худшем случае. Здесь
# сеть подменена — проверяется и что оверлей меняется на лету, и что любая
# осечка оставляет прежний текст.
LIVE_TEXT = "ИЗ-ПОДГРУЗКИ: расчёт за шесть вопросов"
LIVE_BUTTON = "🔄 ЗАМЕНА-ИЗ-ПОДГРУЗКИ"
saved_source, saved_fetch = max_bot.content_source, max_bot.fetch_content
saved_seconds, saved_live_file = max_bot.CONTENT_REFRESH_SECONDS, max_bot.CONTENT_LIVE_FILE
saved_watch = dict(max_bot.CONTENT_WATCH)
saved_content_file = content.CONTENT_FILE
repo_content = os.path.join(REPO, "content.json")
repo_before = open(repo_content, encoding="utf-8").read()
CHECKS = []


def watch_source():
    """Ссылка «на main» — настоящий content_source её соберёт, здесь важна подмена."""
    return ("https://api.github.com/repos/personalartru/noroutine-optimizer"
            "/contents/content.json?ref=main", {})


def serve(payload):
    """Что вернёт «main» на следующий осмотр: текст файла, объект или None (осечка)."""
    raw = payload if payload is None or isinstance(payload, str) \
        else json.dumps(payload, ensure_ascii=False)

    def _fetch(url, headers):
        CHECKS.append(url)
        return raw

    max_bot.fetch_content = _fetch
    return raw


max_bot.content_source = watch_source
max_bot.CONTENT_REFRESH_SECONDS = 45
max_bot.CONTENT_LIVE_FILE = os.path.join(WORKDIR, ".content_live.json")
max_bot.start_content_watch()
results.append(check("первый осмотр отложен, а не «сейчас»",
                     max_bot.CONTENT_WATCH["next"] > time.time(),
                     str(max_bot.CONTENT_WATCH["digest"])[:12]))

serve({"schema": content.SCHEMA, "texts": {"intro": LIVE_TEXT},
       "buttons": {"reset": LIVE_BUTTON}})
changed = max_bot.try_refresh_content(force=True)
results.append(check("правка из main подхватывается внутри живого цикла",
                     changed is True and content.text("intro") == LIVE_TEXT,
                     str(content.text("intro")[:40])))
results.append(check("подгрузка пишет скачанный файл рядом, а не в checkout",
                     content.CONTENT_FILE == max_bot.CONTENT_LIVE_FILE
                     and open(repo_content, encoding="utf-8").read() == repo_before))
del SENT[:]
max_bot.handle_bot_started({"user": {"user_id": USER2, "name": "Павел"},
                            "chat_id": DIALOG2})
results.append(check("новый текст уходит в чат без перезапуска цикла",
                     any(LIVE_TEXT in t for t in texts_to(DIALOG2)),
                     str(texts_to(DIALOG2)[:1])))
# «Сброс» виден со шага выбора людей: на шаге сфер кнопок-действий нет.
click("opt:s:it", user=USER2, callback_id="cb15")
results.append(check("новая подпись кнопки видна в клавиатуре без рестарта",
                     content.label("reset") == LIVE_BUTTON
                     and any(b.get("payload") == "opt:reset"
                             and b.get("text") == LIVE_BUTTON
                             for b in all_buttons(DIALOG2))))

cta_now = content.get("texts.rollout.cta") or ""
lead_now = len(cta_now) - len(cta_now.lstrip("\n"))
LIVE_CTA = "\n" * lead_now + "ПРАВКА-ИЗ-ПОДГРУЗКИ"
serve({"schema": content.SCHEMA,
       "texts": {"intro": LIVE_TEXT, "rollout": {"cta": LIVE_CTA}}})
max_bot.try_refresh_content(force=True)
cta_after = content.get("texts.rollout.cta") or ""
results.append(check("подгрузка сохраняет ведущие пустые строки вёрстки",
                     lead_now > 0 and content.text("intro") == LIVE_TEXT
                     and cta_after == LIVE_CTA
                     and len(cta_after) - len(cta_after.lstrip("\n")) == lead_now,
                     f"{lead_now} строк(и) в начале"))

same_digest = max_bot.CONTENT_WATCH["digest"]
results.append(check("тот же файл из main не пересобирает оверлей второй раз",
                     max_bot.try_refresh_content(force=True) is False
                     and max_bot.CONTENT_WATCH["digest"] == same_digest))
checks_after = len(CHECKS)
max_bot.CONTENT_WATCH["next"] = time.time() + 45
results.append(check("между осмотрами цикл не дёргает API",
                     max_bot.try_refresh_content() is False
                     and len(CHECKS) == checks_after))

good_live = open(max_bot.CONTENT_LIVE_FILE, encoding="utf-8").read()
broken = []
for label, payload in (("битый JSON", "{ это не json"),
                       ("не-объект", "[1, 2, 3]"),
                       ("осечка сети", None)):
    serve(payload)
    ok = max_bot.try_refresh_content(force=True)
    if ok is not False or content.CONTENT_FILE != max_bot.CONTENT_LIVE_FILE \
            or open(max_bot.CONTENT_LIVE_FILE, encoding="utf-8").read() != good_live:
        broken.append(label)
results.append(check("битый файл, не-объект и сетевой сбой оставляют прежний оверлей",
                     not broken, "; ".join(broken)))

# Маршрут подгрузки проверяем настоящим content_source: от него зависит, не
# съест ли правку кэш и хватит ли уже имеющегося токена.
saved_env = {k: os.environ.get(k) for k in
             ("GITHUB_REPOSITORY", "GITHUB_REF_NAME", "GITHUB_TOKEN")}


def env(**kwargs):
    for key, value in kwargs.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


max_bot.content_source = saved_source
env(GITHUB_REPOSITORY="personalartru/noroutine-optimizer", GITHUB_REF_NAME="main",
    GITHUB_TOKEN="gha-token")
url_api, hdr_api = max_bot.content_source()
env(GITHUB_TOKEN=None)
url_raw, hdr_raw = max_bot.content_source()
max_bot.CONTENT_REFRESH_SECONDS = 0
url_off, hdr_off = max_bot.content_source()
max_bot.CONTENT_REFRESH_SECONDS = 45
env(GITHUB_REPOSITORY=None)
url_none, _ = max_bot.content_source()
results.append(check("с GITHUB_TOKEN контент берётся из API (raw-CDN держит файл минут 5)",
                     url_api.startswith("https://api.github.com/repos/"
                                        "personalartru/noroutine-optimizer/"
                                        "contents/content.json?ref=main&_=")
                     and hdr_api.get("Accept") == "application/vnd.github.raw+json"
                     and hdr_api.get("Authorization") == "Bearer gha-token",
                     url_api))
results.append(check("без токена — raw с «свежающим» параметром",
                     "raw.githubusercontent.com" in url_raw
                     and "personalartru/noroutine-optimizer/main/content.json?_=" in url_raw
                     and not hdr_raw.get("Authorization"), url_raw))
results.append(check("CONTENT_REFRESH_SECONDS=0 и отсутствие GITHUB_REPOSITORY "
                     "выключают подгрузку",
                     url_off == "" and hdr_off == {} and url_none == ""))
for key, value in saved_env.items():
    env(**{key: value})


class RespGet:
    def __init__(self, text, code):
        self.text, self.status_code = text, code


class Net:
    """Замена requests.get: RequestException — тот же RuntimeError, что в FakeRequests."""
    RequestException = RuntimeError

    def __init__(self, resp=None, error=None):
        self.resp, self.error = resp, error

    def get(self, url, headers=None, timeout=None):
        if self.error:
            raise self.error("сеть легла")
        return self.resp


saved_requests = max_bot.requests
max_bot.fetch_content = saved_fetch        # дальше проверяем саму функцию, не заглушку
max_bot.requests = Net(RespGet("текст ответа", 200))
got = max_bot.fetch_content("https://example/content.json", {})
max_bot.requests = Net(RespGet('{"message": "Not Found"}', 404))
miss = max_bot.fetch_content("https://example/content.json", {})
max_bot.requests = Net(error=RuntimeError)
down = max_bot.fetch_content("https://example/content.json", {})
max_bot.requests = saved_requests
results.append(check("fetch_content: 200 — текст, 404 и сетевой сбой — None",
                     got == "текст ответа" and miss is None and down is None))

max_bot.content_source, max_bot.fetch_content = saved_source, saved_fetch
max_bot.CONTENT_REFRESH_SECONDS, max_bot.CONTENT_LIVE_FILE = saved_seconds, saved_live_file
max_bot.CONTENT_WATCH = saved_watch
content.CONTENT_FILE = saved_content_file
content.reload()
optimizer.refresh_catalog()
results.append(check("после подгрузки возвращаемся к боевому файлу",
                     content.text("intro") != LIVE_TEXT and content.warnings() == []))
results.append(check("workflow даёт циклу токен на чтение и ручку частоты подгрузки",
                     "CONTENT_REFRESH_SECONDS" in wf and "GITHUB_TOKEN" in wf
                     and "try_refresh_content" in src_bot))
results.append(check("цикл печатает, что подгрузка включена",
                     "подгрузка контента" in src_bot))

print(f"\n{sum(results)}/{len(results)} проверок пройдено")
sys.exit(0 if all(results) else 1)
