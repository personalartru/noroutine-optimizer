"""MAX-транспорт отдельного бота «Оптимизатор» (@se14730534_1_bot).

Логика шести шагов целиком в optimizer.py, формулировки и каталог — в content.py
(с поверх кодовых дефолтов ложится content.json из репозитория); здесь только
long polling MAX, доставка шагов в личку, ответы на нажатия кнопок, пересылка
заявки владельцу и горячая подгрузка контента.

Как это работает с MAX:
- бот пишет только тем, кто его запустил, поэтому личка может быть недоступна —
  тогда отвечаем на callback, и клик не теряется;
- chat_id диалога НЕ равен user_id: писать нужно на chat_id из события,
  иначе 404 chat.not.found — соответствие помним в .max_dialogs.json;
  в личку по идентификатору пользователя MAX ждёт параметр user_id, а не chat_id;
- заявка с финального шага уходит в диалог старого лид-бота @se14730534_bot
  (секрет LEAD_MAX_BOT_TOKEN), где владелец уже читает лиды; если токена нет —
  в собственный диалог с владельцем; если не дошла — ждёт повтора в
  .max_admin_pending.json;
- каждое сообщение режется по 900 символов (см. optimizer.TEXT_CHUNK);
- правки админки видны не в следующем круге Actions, а в этом: цикл сверяется
  с content.json в main (try_refresh_content) и меняет оверлей на лету.

Запуск: python max_bot.py [--minutes N]
"""
import hashlib
import json
import os
import re
import sys
import time

import requests

import optimizer
import content
import report_pdf

MAX_API = os.environ.get("MAX_API_BASE", "https://platform-api2.max.ru")
# Пустой токен — не исключение, а штатная ситуация «секрет ещё не добавлен»:
# main() честно скажет об этом и не станет молча крутить цикл.
TOKEN = os.environ.get("MAX_BOT_TOKEN", "")
ADMIN_CHAT_ID = os.environ.get("MAX_ADMIN_CHAT_ID", "")
# Заявка должна приходить туда, где владелец уже читает лиды, — в диалог со
# старым лид-ботом @se14730534_bot. Его токен — отдельный секрет
# LEAD_MAX_BOT_TOKEN; без него пишем в собственный диалог «Оптимизатора».
LEAD_BOT_TOKEN = os.environ.get("LEAD_MAX_BOT_TOKEN", "")
# `MAX_ADMIN_CHAT_ID` и `LEAD_MAX_ADMIN_CHAT_ID` — это user_id владельца,
# а не chat_id диалога: в MAX POST /messages принимает либо chat_id (чат или
# канал), либо user_id (личка). chat_id=60669315 даёт 404 chat.not.found.
LEAD_ADMIN_USER_ID = os.environ.get("LEAD_MAX_ADMIN_CHAT_ID", "") or ADMIN_CHAT_ID
# Точный chat_id диалога с лид-ботом, если владелец его знает и задаст:
# надёжнее, чем user_id, и проверяется раньше.
LEAD_ADMIN_DIALOG_ID = os.environ.get("LEAD_MAX_ADMIN_DIALOG_ID", "")
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

# Горячая подгрузка контента: как часто смотреть content.json в main прямо
# внутри живого цикла. 0 или меньше — подгрузка выключена, цикл работает на
# файле из checkout (как было раньше). Нижняя граница 5 с — чтобы опечатка в
# переменной не превратила цикл в гонку за API.
CONTENT_REFRESH_SECONDS = float(os.environ.get("CONTENT_REFRESH_SECONDS", "45") or 45)
if CONTENT_REFRESH_SECONDS > 0:
    CONTENT_REFRESH_SECONDS = max(5.0, CONTENT_REFRESH_SECONDS)
# Сюда кладём скачанный файл: боевой content.json из checkout не трогаем, чтобы
# `git status` прогона оставался чистым, а tools/check_content.py работал с
# репозиторием.
CONTENT_LIVE_FILE = os.environ.get("MAX_CONTENT_LIVE_FILE", ".content_live.json")
CONTENT_WATCH = {"digest": None, "next": 0.0}

TEXT_UNKNOWN_KEY = "start_hint"
"""Ключ встречи и ответа на чужую кнопку до входа в расчёт.

Не строка, а именно ключ: значение читается на каждом ответе, поэтому правка из
админки видна со следующего же сообщения, а не со следующего круга Actions.
"""
TEXT_NO_TOKEN = ("MAX_BOT_TOKEN не задан — бот не может опрашивать MAX. "
                 "Добавьте секрет и перезапустите цикл.")


def text_unknown():
    """Встреча для незнакомой кнопки или сообщения — всегда из свежего контента."""
    return content.text(TEXT_UNKNOWN_KEY)


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


def send_direct(user_id, text, buttons=None, chat_id=None, file_token=None):
    """Личное сообщение: chat_id диалога, если он известен, иначе user_id.

    MAX принимает либо `chat_id`, либо `user_id` — и отдаёт 404 chat.not.found,
    если подставить user_id в chat_id. Поэтому при пустом кэше диалогов пишем
    именно параметром `user_id`, а не угадываем чат.
    """
    query = {"chat_id": chat_id} if chat_id else {"user_id": user_id}
    target = chat_id or user_id
    body = {"text": text, "format": "html", "notify": True}
    attachments = []
    if file_token:
        attachments.append({"type": "file", "payload": {"token": file_token}})
    if buttons:
        attachments.append({"type": "inline_keyboard",
                            "payload": {"buttons": buttons}})
    if attachments:
        body["attachments"] = attachments
    try:
        call("POST", "messages", query=query, body=body)
        print("Отправлено в личку:", target,
              "(chat_id)" if chat_id else "(user_id)")
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


# ── Файлы в MAX ──────────────────────────────────────────────────────────────
UPLOADS = {}


def upload_file(data, filename="report.pdf", mime="application/pdf", token=None):
    """Двухшаговая загрузка файла, как в лид-боте: /uploads -> multipart.

    1) POST /uploads?type=file -> {"url": "..."} (или сразу token)
    2) POST файла на этот url (поле data) -> {"token": "..."}
    Токен файла привязан к загрузившему боту, поэтому кэш по (хэш, токен).
    """
    key = (hashlib.sha1(data).hexdigest(), token or TOKEN, filename)
    if key in UPLOADS:
        return UPLOADS[key]
    headers = {"Authorization": token or TOKEN}
    r = requests.post(f"{MAX_API}/uploads", params={"type": "file"},
                      headers=headers, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"uploads: HTTP {r.status_code} {r.text[:200]}")
    payload = r.json()
    file_token = payload.get("token") or (payload.get("result") or {}).get("token")
    if not file_token:
        upload_url = payload.get("url")
        if not upload_url:
            raise RuntimeError(f"MAX не вернул url для загрузки: {str(payload)[:200]}")
        r2 = requests.post(upload_url, headers=headers,
                           files={"data": (filename, data, mime)}, timeout=180)
        if r2.status_code != 200:
            raise RuntimeError(f"upload url: HTTP {r2.status_code} {r2.text[:200]}")
        got = r2.json()
        file_token = (got.get("token") or (got.get("result") or {}).get("token")
                      or payload.get("token"))
    if not file_token:
        raise RuntimeError("MAX не вернул токен файла")
    UPLOADS[key] = file_token
    print(f"Файл загружен в MAX: {filename} ({len(data)} байт)")
    return file_token


def report_bytes(data):
    """PDF отчёта или None: без fpdf2/шрифта бот честно отдаст текст."""
    if not report_pdf.available():
        print("::warning::PDF недоступен (нет fpdf2 или кириллического шрифта) — "
              "отправлю чек-лист текстом")
        return None
    try:
        return report_pdf.build(data)
    except Exception as e:                       # не роняем шаг из-за вёрстки
        print(f"::warning::не удалось собрать PDF: {e} — отправлю текстом")
        return None


def report_filename(data):
    """Имя файла по-человечески: optimizator-it-03-11-2026.pdf (id сферы, не кириллица)."""
    base = re.sub(r"[^a-z0-9]+", "-", str(data.get("slug") or "").lower()).strip("-")
    if not base:
        base = re.sub(r"[^a-z0-9]+", "-",
                      str(data.get("title", "")).lower()).strip("-")
    base = re.sub(r"-{2,}", "-", base)[:40] or "optimizator"
    date = re.sub(r"[^0-9]", "-", str(data.get("date", ""))).strip("-")
    return f"optimizator-{base}-{date}.pdf"


def admin_routes():
    """Куда слать заявку, в порядке предпочтения: (токен, query, подпись).

    1. Диалог старого лид-бота @se14730534_bot (токен из `LEAD_MAX_BOT_TOKEN`) —
       там владелец уже читает заявки, поэтому это основной путь. Пишем по
       `user_id`: по документации MAX это параметр «отправить сообщение
       пользователю», а `chat_id` нужен для чата или канала. Если задан
       `LEAD_MAX_ADMIN_DIALOG_ID`, пробуем и его — точный chat_id диалога
       надёжнее;
    2. собственный диалог «Оптимизатора» с владельцем — chat_id из события
       (`chat_id` диалога НЕ равен `user_id`, иначе 404 chat.not.found);
       свежее значение из .max_dialogs.json важнее резервного
       `MAX_ADMIN_DIALOG_ID`, чтобы не застрять на удалённом диалоге;
    3. `user_id` владельца своим токеном — последняя попытка, когда диалог ещё
       не запомнен (кэш состояния пуст).
    """
    routes = []
    if LEAD_BOT_TOKEN:
        if LEAD_ADMIN_DIALOG_ID:
            routes.append((LEAD_BOT_TOKEN, {"chat_id": LEAD_ADMIN_DIALOG_ID},
                           f"лид-бот @se14730534_bot (диалог {LEAD_ADMIN_DIALOG_ID})"))
        if LEAD_ADMIN_USER_ID:
            routes.append((LEAD_BOT_TOKEN, {"user_id": LEAD_ADMIN_USER_ID},
                           f"лид-бот @se14730534_bot (user_id {LEAD_ADMIN_USER_ID})"))
    dialog = (dialog_for(ADMIN_CHAT_ID) if ADMIN_CHAT_ID else None) or ADMIN_DIALOG_ID
    if dialog:
        routes.append((TOKEN, {"chat_id": dialog}, f"свой диалог {dialog}"))
    if ADMIN_CHAT_ID and ADMIN_CHAT_ID != dialog:
        routes.append((TOKEN, {"user_id": ADMIN_CHAT_ID}, f"user_id {ADMIN_CHAT_ID}"))
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


def send_admin_lead(text, pdf=None, filename="report.pdf"):
    routes = admin_routes()
    if not routes:
        print("::warning::некуда слать заявку: нет LEAD_MAX_BOT_TOKEN "
              "и MAX_ADMIN_CHAT_ID — текст только в очереди на повтор")
        queue_admin_lead(text)
        return False
    for token, query, label in routes:
        file_token = ""
        if pdf:
            try:
                file_token = upload_file(pdf, filename=filename, token=token)
            except (RuntimeError, requests.RequestException) as e:
                # заявка важнее приложения: уезжаем без файла
                print(f"::warning::PDF к заявке не загрузился ({label}): {e}")
        body = {"text": text, "format": "html", "notify": True}
        if file_token:
            body["attachments"] = [{"type": "file",
                                    "payload": {"token": file_token}}]
        try:
            call("POST", "messages", query=query, body=body, token=token)
            print(f"Заявка отправлена владельцу -> {label}"
                  + (" с PDF" if file_token else ""))
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

    # Второй экземпляр отчёта — владельцу: видно, что именно получил человек.
    pdf, filename = None, "report.pdf"
    if calc and session.get("sav"):
        try:
            data = optimizer.report_data(session, calc, session["sav"])
            pdf = report_bytes(data)
            filename = report_filename(data)
            if pdf:
                lines.insert(-1, "Полный расчёт и чек-лист — в PDF ниже.")
        except Exception as e:
            print(f"::warning::PDF к заявке не собрался: {e}")
            pdf = None
    return send_admin_lead("\n".join(lines), pdf=pdf, filename=filename)


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


def save_text(path, text):
    """Как save_json, только уже готовый текст: скачанный файл не пересобираем."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
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
    """Живой шаг держим, отменённый выбрасываем, готовый расчёт — отдельно.

    Финал (`step == "done"`) раньше очищал запись целиком, и кнопка
    «📄 Полный отчёт (PDF)» под сообщением с заявкой попадала в пустую сессию:
    человек жал её, а бот отвечал «сначала пройдём шаги». Теперь от финала
    оставляем только расчёт (`calc`/`sav`) — он больше ни на что не влияет:
    `expects_text` на "done" ложь, поэтому обычные сообщения не становятся
    заявками, а «Пройти заново» начинает чистую сессию как и раньше.
    """
    sessions = load_sessions()
    key = str(user_id)
    step = (session or {}).get("step")
    if not session:
        sessions.pop(key, None)
    elif step == "done" and not (session.get("calc") and session.get("sav")):
        sessions.pop(key, None)
    elif step == "done":
        sessions[key] = {"step": "done", "calc": session["calc"],
                         "sav": session["sav"], "sphere": session.get("sphere"),
                         "roles": session.get("roles"), "ops": session.get("ops"),
                         "tools": session.get("tools"),
                         "updated": session.get("updated", time.time())}
    else:
        sessions[key] = session
    # заодно чистим протухшие: файл состояния живёт в actions/cache и не должен
    # разрастаться записями всех, кто когда-то нажал кнопку
    for stale in [k for k, v in sessions.items() if optimizer.is_expired(v)]:
        sessions.pop(stale, None)
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


# ── Горячая подгрузка контента ───────────────────────────────────────────────
def content_source():
    """Откуда брать content.json main'а: (url, заголовки) или ("", {}) если нечего.

    Новых секретов подгрузка не просит: у шага в Actions уже есть GITHUB_TOKEN,
    а репозиторий публичный. Вне Actions (тесты, ручной запуск) переменных
    GITHUB_* нет, и функция честно возвращает пустую ссылку — цикл работает на
    файле из checkout.

    С токена идём в API, без него на raw.githubusercontent: оба могут отдать
    ответ из кэша (CDN держит raw до 5 минут), поэтому в url каждый осмотр новый
    «свежающий» параметр — с ним кэш не в силах соврать про версию файла.
    """
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo or CONTENT_REFRESH_SECONDS <= 0:
        return "", {}
    ref = os.environ.get("GITHUB_REF_NAME", "") or "main"
    fresh = int(time.time())
    headers = {"Cache-Control": "no-cache"}
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        return (f"https://api.github.com/repos/{repo}/contents/content.json"
                f"?ref={ref}&_={fresh}",
                {**headers, "Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github.raw+json"})
    return (f"https://raw.githubusercontent.com/{repo}/{ref}/content.json?_={fresh}",
            headers)


def fetch_content(url, headers):
    """Текст content.json из main или None: сетевая осечка не роняет цикл."""
    try:
        r = requests.get(url, headers=headers, timeout=15)
    except requests.RequestException as e:
        print("::warning::content.json не скачался:", str(e)[:160],
              "— оставляю прежний оверлей")
        return None
    if r.status_code != 200:
        print(f"::warning::content.json: HTTP {r.status_code} — оставляю прежний оверлей")
        return None
    return r.text


def start_content_watch():
    """Запомнить хэш файла, на котором цикл стартовал, и срок следующего осмотра."""
    digest = ""
    try:
        with open(content.CONTENT_FILE, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
    except OSError:
        pass
    CONTENT_WATCH["digest"] = digest
    CONTENT_WATCH["next"] = time.time() + CONTENT_REFRESH_SECONDS
    return digest


def try_refresh_content(force=False):
    """Подхватить правку админки внутри живого цикла; True — если тексты сменились.

    Зачем: content.json читается на старте процесса, цикл живёт ~9 минут, а
    новый прогон ещё и ждёт слот concurrency. По прод-пушам контента это
    35 с / 71 с / 2 мин 22 с / 4 мин 46 с и до ~9,5 мин в худшем случае. С
    подгрузкой задержка — один шаг цикла.

    Ничего не портим: осечка сети, HTTP-ошибка, битый JSON или не-объект
    оставляют прежний оверлей, а content.json из checkout остаётся нетронутым —
    скачанный файл живёт отдельно в CONTENT_LIVE_FILE.
    """
    url, headers = content_source()
    if not url:
        return False
    if not force and time.time() < CONTENT_WATCH["next"]:
        return False
    CONTENT_WATCH["next"] = time.time() + CONTENT_REFRESH_SECONDS
    raw = fetch_content(url, headers)
    if raw is None:
        return False
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if digest == CONTENT_WATCH["digest"]:
        return False
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        print(f"::warning::content.json из main не разбирается ({str(exc)[:120]}) — "
              "оставляю прежний оверлей")
        return False
    if not isinstance(parsed, dict):
        print("::warning::content.json из main: ожидался объект — оставляю прежний оверлей")
        return False
    if not save_text(CONTENT_LIVE_FILE, raw):
        return False
    content.CONTENT_FILE = CONTENT_LIVE_FILE
    content.reload()
    optimizer.refresh_catalog()
    CONTENT_WATCH["digest"] = digest
    print("контент обновлён из main без рестарта цикла:", content.source_summary())
    for line in content.problems():
        print(f"::warning::контент: {line}")
    return True


# ── Оптимизатор ──────────────────────────────────────────────────────────────
def deliver_pdf(user_id, chat_id, message):
    """Отдать PDF по кнопке: файл с подписью, а если файла нет — текст-выручалочка.

    Отчёт может не собраться (в окружении нет fpdf2 или кириллического шрифта)
    или не загрузиться (MAX не ответил токеном). В обоих случаях человек обязан
    получить содержимое: `pdf_fallback` — развёрнутый чек-лист сообщением.
    """
    data = message.get("pdf") or {}
    fallback = message.get("pdf_fallback") or ""
    buttons = message.get("buttons")
    blob = report_bytes(data)
    if blob is None:
        return send_text_checklist(user_id, chat_id, fallback, buttons)
    try:
        file_token = upload_file(blob, filename=report_filename(data))
    except (RuntimeError, requests.RequestException) as e:
        print(f"::warning::файл не загрузился в MAX: {e} — отправлю текстом")
        return send_text_checklist(user_id, chat_id, fallback, buttons)
    return send_direct(user_id, message.get("text", ""), buttons=buttons,
                       chat_id=chat_id, file_token=file_token)


def send_text_checklist(user_id, chat_id, text, buttons=None):
    """Развёрнутый чек-лист текстом: режем по 900 символов, кнопки — на последнем."""
    if not text:
        return False
    parts = optimizer.chunk(text)
    ok = False
    for i, part in enumerate(parts):
        ok = send_direct(user_id, part,
                         buttons=buttons if i == len(parts) - 1 else None,
                         chat_id=chat_id) or ok
    return ok


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
        if message.get("pdf"):
            if deliver_pdf(user_id, chat_id, message):
                delivered = True
                continue
        if send_direct(user_id, text, buttons=message.get("buttons"),
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
    chat_id = dialog_for(user_id)

    if not payload.startswith("opt:"):
        # Кнопка из старого диалога (бот до переезда) — не теряем клик.
        answer_callback(callback_id, text_unknown())
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

    send_direct(user_id, text_unknown(),
                buttons=[[btn(content.label("start"), "opt:start")]],
                chat_id=chat_id)


def handle_bot_started(event):
    """Пользователь открыл бота: сразу ведём в шаг 1, лишних тапов нет."""
    user = event.get("user") or {}
    user_id = user.get("user_id")
    chat_id = event.get("chat_id") or dialog_for(user_id)
    remember_dialog(user_id, chat_id)
    print("bot_started | user", user_id, "| chat", chat_id or "нет (пишу user_id)",
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
    if report_pdf.available():
        font = os.path.basename(report_pdf.font_regular())
        pdf_state = f"собираю ({font})"
    else:
        pdf_state = "нет — чек-лист отдам текстом"
    print(f"аккаунт: @{who or 'не опознан — проверьте MAX_BOT_TOKEN'} · "
          f"заявки: {routes[0][2] if routes else 'некуда слать (только очередь)'} · "
          f"PDF: {pdf_state} · "
          f"кнопка на финале: {optimizer.CHANNEL_URL}")
    # Строка нужна, чтобы по логу прод-цикла видеть, подхватил ли бот правку
    # админки (content.json) или всё ещё говорит код-дефолтами.
    print("контент:", content.source_summary())
    for line in content.problems():
        print(f"::warning::контент: {line}")
    url, _ = content_source()
    start_content_watch()
    if url:
        print(f"подгрузка контента: сверяюсь с main каждые "
              f"{CONTENT_REFRESH_SECONDS:.0f} с — правка из админки "
              "доезжает до следующего расчёта, не дожидаясь конца цикла")
    else:
        print("подгрузка контента: выключена (нет GITHUB_REPOSITORY или лимит 0) "
              "— цикл говорит на файле из checkout")
    flush_admin_leads()
    bootstrap_state()
    next_flush = time.time() + 60

    while time.time() < deadline:
        # Правку админки подхватываем на лету: без этого новый текст ждал бы
        # конца цикла и слота в очереди (замерено до ~9,5 мин). В начале прохода,
        # чтобы осечка MAX с `continue` не откладывала осмотр до следующего круга.
        try_refresh_content()
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
