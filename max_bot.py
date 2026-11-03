"""MAX-транспорт отдельного бота «Оптимизатор» (@se14730534_1_bot).

Логика шести шагов целиком в optimizer.py — здесь только long polling MAX,
доставка шагов в личку, ответы на нажатия кнопок и пересылка заявки владельцу.

Как это работает с MAX:
- бот пишет только тем, кто его запустил, поэтому личка может быть недоступна —
  тогда отвечаем на callback, и клик не теряется;
- chat_id диалога НЕ равен user_id: писать нужно на chat_id из события,
  иначе 404 chat.not.found — соответствие помним в .max_dialogs.json;
- заявка с финального шага уходит в диалог старого лид-бота @se14730534_bot
  (секрет LEAD_MAX_BOT_TOKEN), где владелец уже читает лиды; если токена нет —
  в собственный диалог с владельцем; если не дошла — ждёт повтора в
  .max_admin_pending.json;
- каждое сообщение режется по 900 символов (см. optimizer.TEXT_CHUNK).

Запуск: python max_bot.py [--minutes N]
"""
import json
import os
import sys
import time

import requests

import optimizer

MAX_API = os.environ.get("MAX_API_BASE", "https://platform-api2.max.ru")
# Пустой токен — не исключение, а штатная ситуация «секрет ещё не добавлен»:
# main() честно скажет об этом и не станет молча крутить цикл.
TOKEN = os.environ.get("MAX_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.environ.get("MAX_ADMIN_CHAT_ID", "")
# Заявка должна приходить туда, где владелец уже читает лиды, — в диалог со
# старым лид-ботом @se14730534_bot. Его токен — отдельный секрет
# LEAD_MAX_BOT_TOKEN; без него пишем в собственный диалог «Оптимизатора».
LEAD_BOT_TOKEN = os.environ.get("LEAD_MAX_BOT_TOKEN", "")
LEAD_ADMIN_CHAT_ID = os.environ.get("LEAD_MAX_ADMIN_CHAT_ID", "") or ADMIN_CHAT_ID
# Резервный chat_id диалога владельца на случай холодного кэша состояния.
ADMIN_DIALOG_ID = os.environ.get("MAX_ADMIN_DIALOG_ID", "")
RUN_MINUTES = float(os.environ.get("BOT_RUN_MINUTES", "9") or 9)
OFFSET_FILE = os.environ.get("MAX_OFFSET_FILE", ".max_offset")
DIALOGS_FILE = os.environ.get("MAX_DIALOGS_FILE", ".max_dialogs.json")
SESSIONS_FILE = os.environ.get("MAX_OPTIMIZER_FILE", ".max_optimizer.json")
# Недоставленные заявки: без них текст теряется вместе с упавшим запросом.
ADMIN_PENDING_FILE = os.environ.get("MAX_ADMIN_PENDING_FILE", ".max_admin_pending.json")
ADMIN_PENDING_MAX = 50
BOT_USERNAME = os.environ.get("MAX_BOT_USERNAME", "")

TEXT_UNKNOWN = (
    "Я Оптимизатор: шесть коротких вопросов — и на выходе часы рутины, их "
    "стоимость, экономия и чек-лист внедрения.\n\n"
    "Напишите /старт — и поехали."
)
TEXT_NO_TOKEN = ("MAX_BOT_TOKEN не задан — бот не может опрашивать MAX. "
                 "Добавьте секрет и перезапустите цикл.")


def btn(text, payload):
    return {"type": "callback", "text": text, "payload": payload}


# ── API ──────────────────────────────────────────────────────────────────────
def call(method, path, query=None, body=None, token=None):
    key = token or TOKEN
    if not key:
        raise RuntimeError(TEXT_NO_TOKEN)
    r = requests.request(
        method,
        f"{MAX_API}/{path}",
        params=query,
        headers={"Authorization": key, "Content-Type": "application/json"},
        json=body,
        timeout=90,
    )
    if r.status_code != 200:
        raise RuntimeError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


def bot_username():
    """Технический юзернейм бота для диплинков (как в /me)."""
    global BOT_USERNAME
    if not BOT_USERNAME:
        try:
            BOT_USERNAME = (call("GET", "me") or {}).get("username") or ""
        except RuntimeError:
            BOT_USERNAME = ""
    return BOT_USERNAME


def forget_dialog(user_id):
    """Пользователь удалил диалог — убираем устаревший chat_id."""
    dialogs = load_dialogs()
    if str(user_id) in dialogs:
        del dialogs[str(user_id)]
        save_dialogs(dialogs)
        print(f"Забыл устаревший диалог: user_id={user_id}")


def send_direct(user_id, text, buttons=None, chat_id=None):
    """Личное сообщение. chat_id диалога важнее user_id — иначе 404."""
    target = chat_id or user_id
    body = {"text": text, "format": "html", "notify": True}
    if buttons:
        body["attachments"] = [{"type": "inline_keyboard",
                                "payload": {"buttons": buttons}}]
    try:
        call("POST", "messages", query={"chat_id": target}, body=body)
        print("Отправлено в личку:", target)
        return True
    except RuntimeError as e:
        print(f"Личка недоступна ({target}):", e)
        if "chat.not.found" in str(e) and chat_id:
            forget_dialog(user_id)
        return False


def answer_callback(callback_id, text):
    """Ответ на нажатие кнопки: POST /answers?callback_id=..."""
    if not callback_id:
        return False
    try:
        call("POST", "answers", query={"callback_id": callback_id},
             body={"message": {"text": text, "format": "html"}})
        return True
    except RuntimeError as e:
        print("Не удалось ответить на callback:", e)
        return False


def admin_routes():
    """Куда слать заявку, в порядке предпочтения.

    1. Диалог старого лид-бота @se14730534_bot (токен из `LEAD_MAX_BOT_TOKEN`) —
       там владелец уже читает заявки, поэтому это основной путь;
    2. собственный диалог «Оптимизатора» с владельцем — chat_id из события
       (`chat_id` диалога НЕ равен `user_id`, иначе 404 chat.not.found);
       свежее значение из .max_dialogs.json важнее резервного
       `MAX_ADMIN_DIALOG_ID`, чтобы не застрять на удалённом диалоге;
    3. голый `user_id` владельца — последняя попытка, когда диалог ещё не
       запомнен (кэш состояния пуст).
    """
    routes = []
    if LEAD_BOT_TOKEN and LEAD_ADMIN_CHAT_ID:
        routes.append((LEAD_BOT_TOKEN, LEAD_ADMIN_CHAT_ID, "лид-бот @se14730534_bot"))
    dialog = (dialog_for(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else None) or ADMIN_DIALOG_ID
    if dialog:
        routes.append((TOKEN, dialog, f"свой диалог {dialog}"))
    if ADMIN_CHAT_ID and ADMIN_CHAT_ID != dialog:
        routes.append((TOKEN, ADMIN_CHAT_ID, f"user_id {ADMIN_CHAT_ID}"))
    return routes


def queue_admin_lead(text):
    """Заявку не теряем: кладём в файл и пробуем довезти в следующем цикле."""
    pending = load_json(ADMIN_PENDING_FILE)
    items = pending.get("leads") or []
    items.append(text)
    pending["leads"] = items[-ADMIN_PENDING_MAX:]
    save_json(ADMIN_PENDING_FILE, pending)
    print(f"Заявка не доставлена — сохранил для повтора "
          f"(в очереди: {len(pending['leads'])})")


def send_admin_lead(text):
    routes = admin_routes()
    if not routes:
        print("::warning::некуда слать заявку: нет LEAD_MAX_BOT_TOKEN "
              "и MAX_ADMIN_CHAT_ID — текст только в очереди на повтор")
        queue_admin_lead(text)
        return False
    for token, chat_id, label in routes:
        try:
            call("POST", "messages", query={"chat_id": chat_id},
                 body={"text": text, "format": "html", "notify": True},
                 token=token)
            print(f"Заявка отправлена владельцу -> {label}")
            return True
        except RuntimeError as e:
            print(f"Заявка не прошла через {label}: {e}")
    queue_admin_lead(text)
    return False


def flush_admin_leads():
    """Повтор недоставленных заявок — при каждом проходе цикла."""
    pending = load_json(ADMIN_PENDING_FILE)
    items = pending.get("leads") or []
    if not items:
        return 0
    remaining = []
    sent = 0
    for text in items:
        if send_admin_lead(text):
            sent += 1
        else:
            remaining.append(text)
            break  # канал недоступен — остальное повтим на следующем круге
    if remaining:
        save_json(ADMIN_PENDING_FILE, {"leads": remaining})
    else:
        try:
            os.remove(ADMIN_PENDING_FILE)
        except OSError:
            save_json(ADMIN_PENDING_FILE, {"leads": []})
    if sent:
        print(f"Довёз заявок из очереди: {sent}, осталось: {len(remaining)}")
    return sent


def notify_admin(user_id, name, request, username, session):
    """Заявка с финального шага: текст пользователя плюс его же расчёт."""
    calc = session.get("calc") or {}
    who = name or "без имени"
    if username:
        who += f" (@{username})"
    lines = ["📩 Расчёт из «Оптимизатора»",
             f"Кто: {who}",
             f"user_id: {user_id}",
             f"Ссылка на профиль: https://max.ru/u/{user_id}",
             "",
             f"Текст запроса:\n{request}"]
    if calc:
        tools = ", ".join(t["label"] for t in (session.get("sav") or {}).get("tools", []))
        summary = (f"{calc.get('sphere_label')} · {calc.get('people')} чел · "
                   f"{int(calc.get('hours') or 0)} ч/мес рутины · "
                   f"~{int(calc.get('cost_month') or 0)} ₽/мес")
        if tools:
            summary += f" · инструменты: {tools}"
        lines += ["", f"[Оптимизатор] {summary}"]
    lines += ["", "Ответьте ему в личку MAX: откройте профиль и напишите."]
    return send_admin_lead("\n".join(lines))


# ── Состояние ────────────────────────────────────────────────────────────────
def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        return True
    except OSError as e:
        print(f"Не удалось сохранить {path}:", e)
        return False


def code_sha():
    """Версия кода — чтобы по логу видеть, какой коммит работает."""
    import subprocess
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True,
                              timeout=10).stdout.strip() or "?"
    except Exception:
        return "?"


def remember_dialog(user_id, chat_id):
    if not user_id or not chat_id:
        return
    dialogs = load_json(DIALOGS_FILE)
    if dialogs.get(str(user_id)) != chat_id:
        dialogs[str(user_id)] = chat_id
        save_json(DIALOGS_FILE, dialogs)
        print("Запомнил диалог:", user_id, "->", chat_id)


def dialog_for(user_id):
    return load_json(DIALOGS_FILE).get(str(user_id))


def load_sessions():
    return load_json(SESSIONS_FILE)


def save_sessions(sessions):
    save_json(SESSIONS_FILE, sessions)


def session_for(user_id):
    """Активная сессия; просроченную выбрасываем и отдаём None."""
    sessions = load_sessions()
    session = sessions.get(str(user_id))
    if not session:
        return None
    if optimizer.is_expired(session):
        sessions.pop(str(user_id), None)
        save_sessions(sessions)
        return None
    return session


def store_session(user_id, session):
    """Держим только живой шаг: финал и отмена очищают файл."""
    sessions = load_sessions()
    key = str(user_id)
    if not session or session.get("step") == "done":
        sessions.pop(key, None)
    else:
        sessions[key] = session
    save_sessions(sessions)


def wants_text(user_id):
    session = session_for(user_id)
    return session if session and optimizer.expects_text(session) else None


def load_offset():
    try:
        with open(OFFSET_FILE, encoding="utf-8") as f:
            return int(f.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def save_offset(offset):
    try:
        with open(OFFSET_FILE, "w", encoding="utf-8") as f:
            f.write(str(offset))
    except OSError:
        pass


def bootstrap_state():
    """Файлы состояния пишем до первого же события, а не после него.

    `actions/cache/save@v4` отказывается сохранять кэш, когда ни одного из
    перечисленных путей нет: круг без сообщений не создавал `.max_offset` и
    `.max_dialogs.json`, цепочка кэша обрывалась («Path Validation Error» →
    «Cache save failed» → в следующем цикле «Cache not found»), и бот
    стартовал с offset 0 и без памяти о диалоге владельца.
    """
    save_offset(load_offset())
    save_json(DIALOGS_FILE, load_json(DIALOGS_FILE))
    save_sessions(load_sessions())
    if not os.path.exists(ADMIN_PENDING_FILE):
        save_json(ADMIN_PENDING_FILE, {"leads": []})


# ── Оптимизатор ──────────────────────────────────────────────────────────────
def deliver_messages(user_id, chat_id, messages, callback_id=None):
    """Шаги — отдельными сообщениями, как их собрал optimizer.py.

    Личка недоступна (бот пишет только запустившим его) — отвечаем на callback,
    чтобы нажатие кнопки не выглядело потерянным.
    """
    delivered = False
    for index, message in enumerate(messages):
        text = (message or {}).get("text")
        if not text:
            continue
        if chat_id and send_direct(user_id, text, buttons=message.get("buttons"),
                                   chat_id=chat_id):
            delivered = True
            continue
        if not delivered and index == 0:
            delivered = answer_callback(callback_id, text)
    if not delivered and callback_id and messages:
        delivered = answer_callback(callback_id, messages[0]["text"])
    return delivered


def handle_optimizer(user_id, chat_id, payload=None, text=None, callback_id=None,
                     name="", username=None):
    """Один шаг: нажатие кнопки (payload) или текст пользователя."""
    session = session_for(user_id) or optimizer.new_session()
    if payload is not None:
        session, messages = optimizer.dispatch(payload, session)
    else:
        session, messages = optimizer.dispatch_text(text, session)

    for message in messages:
        request = (message or {}).get("admin")
        if request:
            notify_admin(user_id, name, request, username, session)

    delivered = deliver_messages(user_id, chat_id, messages, callback_id)
    store_session(user_id, session)
    print(f"optimizer: user={user_id} шаг={session.get('step')} "
          f"сообщений={len(messages)} доставлено={delivered}")
    return delivered


# ── Обработчики ─────────────────────────────────────────────────────────────
def handle_callback(callback):
    payload = callback.get("payload") or ""
    callback_id = callback.get("callback_id")
    user = callback.get("user") or {}
    user_id = user.get("user_id")
    chat_id = dialog_for(user_id) or user_id

    if not payload.startswith("opt:"):
        # Кнопка из старого диалога (бот до переезда) — не теряем клик.
        answer_callback(callback_id, TEXT_UNKNOWN)
        return
    handle_optimizer(user_id, chat_id, payload=payload, callback_id=callback_id,
                     name=user.get("name") or user.get("first_name") or "",
                     username=user.get("username"))


def handle_text(user_id, chat_id, name, username, text):
    lowered = text.strip().lower()
    if lowered in ("/start", "старт", "начать", "/старт", "заново", "оптимизатор",
                   "калькулятор", "расчёт", "расчет", "посчитай"):
        handle_optimizer(user_id, chat_id, payload="opt:start", name=name,
                         username=username)
        return

    if lowered in ("отмена", "отменить", "стоп", "хватит", "cancel", "/cancel"):
        handle_optimizer(user_id, chat_id, payload="opt:cancel", name=name,
                         username=username)
        return

    if wants_text(user_id):
        handle_optimizer(user_id, chat_id, text=text, name=name, username=username)
        return

    send_direct(user_id, TEXT_UNKNOWN, buttons=[[btn("🔧 Начать расчёт", "opt:start")]],
                chat_id=chat_id)


def handle_bot_started(event):
    """Пользователь открыл бота: сразу ведём в шаг 1, лишних тапов нет."""
    user = event.get("user") or {}
    user_id = user.get("user_id")
    chat_id = event.get("chat_id") or dialog_for(user_id) or user_id
    remember_dialog(user_id, chat_id)
    print("bot_started | user", user_id, "| chat", chat_id,
          "| payload", event.get("payload"))
    handle_optimizer(user_id, chat_id, payload="opt:start",
                     name=user.get("name") or user.get("first_name") or "",
                     username=user.get("username"))


def handle_event(event):
    kind = event.get("update_type") or event.get("type")

    if kind == "bot_started":
        handle_bot_started(event)
        return

    if kind == "message_callback":
        callback = event.get("callback") or {}
        print("событие message_callback | payload", callback.get("payload"),
              "| user", (callback.get("user") or {}).get("user_id"))
        handle_callback(callback)
        return

    message = event.get("message") or {}
    body = message.get("body") or {}
    text = (body.get("text") or "").strip()
    recipient = message.get("recipient") or {}
    chat_id = recipient.get("chat_id")
    chat_type = recipient.get("chat_type")
    sender = message.get("sender") or event.get("user") or {}
    user_id = sender.get("user_id")
    name = sender.get("name") or sender.get("first_name") or ""
    username = sender.get("username")

    if chat_type == "dialog" and chat_id:
        remember_dialog(user_id, chat_id)
    print(f"событие {kind} | chat_type {chat_type} | user {user_id} | "
          f"текст: {text[:60]!r}")
    if not text:
        return
    target = chat_id if chat_type == "dialog" else dialog_for(user_id)
    handle_text(user_id, target, name, username, text)


def main():
    minutes = RUN_MINUTES
    if "--minutes" in sys.argv:
        minutes = float(sys.argv[sys.argv.index("--minutes") + 1])

    if not TOKEN:
        print(f"::error::{TEXT_NO_TOKEN}")
        return 2

    offset = load_offset()
    deadline = time.time() + minutes * 60
    print(f"MAX-бот «Оптимизатор» запущен (версия {code_sha()}), "
          f"offset {offset}, лимит {minutes} мин")
    # Проверка токена до первого же обновления: на неверном ключе /me молчит,
    # и иначе цикл крутил бы 9 минут впустую.
    who = bot_username()
    routes = admin_routes()
    print(f"аккаунт: @{who or 'не опознан — проверьте MAX_BOT_TOKEN'} · "
          f"заявки: {routes[0][2] if routes else 'некуда слать (только очередь)'} · "
          f"кнопка на финале: {optimizer.CHANNEL_URL}")
    flush_admin_leads()
    bootstrap_state()
    next_flush = time.time() + 60

    while time.time() < deadline:
        try:
            data = call("GET", "updates",
                        query={"offset": offset, "timeout": 20, "limit": 100})
        except RuntimeError as e:
            print("Ошибка API:", e)
            time.sleep(3)
            continue
        except requests.exceptions.RequestException as e:
            print("Сеть недоступна:", e)
            time.sleep(5)
            continue

        for event in (data.get("updates") or []):
            offset = max(offset, (event.get("update_id") or 0) + 1)
            save_offset(offset)
            try:
                handle_event(event)
            except Exception as e:
                print("Ошибка обработки события:", e)

        # Недоставленные заявки повторяем раз в минуту, а не на каждом поллинге.
        if time.time() >= next_flush:
            flush_admin_leads()
            next_flush = time.time() + 60

    print("Время вышло, offset сохранён:", offset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
