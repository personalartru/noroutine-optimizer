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

Методика расчёта: часы рутины × фактор инструмента. Факторы (chat 0.65,
agent 0.85, n8n 0.75, tracker 0.5, full 0.9) складываются как независимые
события: непокрытая доля = произведение (1 − f), с потолком 92%. Ставка —
типовая месячная зарплата по специальности, поделённая на ~160 рабочих часов.
"""
import html
import os
import re
import time

# Финальный шаг, кроме заявки, ведёт читателя в канал про автоматизацию —
# там выходят разборы и готовые связки. Ссылку можно переназначить переменной
# окружения MAX_CHANNEL_URL, не трогая код.
CHANNEL_URL = os.environ.get("MAX_CHANNEL_URL", "https://max.ru/channel_noroutine")

SESSION_TTL = 40 * 60          # без действий сессия сбрасывается
MAX_HEADCOUNT = 300            # потолок для ручного ввода
TEXT_CHUNK = 900               # запас под лимит длины сообщения в MAX
WORK_HOURS = 160               # рабочих часов в месяце на человека

SELF_ROLE = {"label": "Я сам (руковожу и делаю руками)", "salary": 120000}

SPHERES = [
    {
        "id": "agency",
        "label": "Digital / агентство",
        "blurb": "брифы, правки и отчёты по проектам живут в чатах и почте",
        "roles": [
            {"id": "account", "label": "Аккаунт-менеджер", "salary": 90000},
            {"id": "copy", "label": "Копирайтер", "salary": 70000},
            {"id": "target", "label": "Таргетолог", "salary": 100000},
            {"id": "design", "label": "Дизайнер", "salary": 85000},
        ],
        "ops": [
            {"id": "brief", "label": "Сбор брифов и ТЗ", "hours": 20,
             "detail": "один и тот же шаблон, правки клиента по кругу"},
            {"id": "client", "label": "Переписка с клиентами", "hours": 22,
             "detail": "вопросы про статус, сроки и стоимость заново каждый раз"},
            {"id": "track", "label": "Контроль задач и сроков", "hours": 18,
             "detail": "обход трекера, напоминания исполнителям"},
            {"id": "report", "label": "Отчёты по проектам и часам", "hours": 14,
             "detail": "выгрузки, сведение в таблицу, график для клиента"},
            {"id": "assets", "label": "Передача макетов и материалов", "hours": 10,
             "detail": "поиск файлов, ссылки, права доступа"},
            {"id": "invoice", "label": "Счета и закрывающие документы", "hours": 10,
             "detail": "по заявкам, отправка, загрузка в учётную систему"},
        ],
    },
    {
        "id": "ecom",
        "label": "E-commerce",
        "blurb": "карточки, отгрузки, отзывы и отчёты каждый день одними и теми же руками",
        "roles": [
            {"id": "support", "label": "Оператор поддержки", "salary": 60000},
            {"id": "orders", "label": "Менеджер заказов", "salary": 75000},
            {"id": "content", "label": "Контент-менеджер", "salary": 65000},
            {"id": "market", "label": "Менеджер маркетплейсов", "salary": 90000},
        ],
        "ops": [
            {"id": "cards", "label": "Заполнение и обновление карточек", "hours": 22,
             "detail": "описания, характеристики, фото, цены по регламенту"},
            {"id": "reviews", "label": "Ответы на отзывы и вопросы", "hours": 18,
             "detail": "шаблоны, модерация, отработка негатива"},
            {"id": "stock", "label": "Сверка остатков и заказов", "hours": 20,
             "detail": "обход кабинетов, таблички, поиск расхождений"},
            {"id": "ship", "label": "Оформление отгрузок и возвратов", "hours": 16,
             "detail": "перенос данных из заявок в учётную систему, документы"},
            {"id": "price", "label": "Контроль цен и конкурентов", "hours": 12,
             "detail": "ежедневной ручной мониторинг листов и кабинетов"},
            {"id": "report", "label": "Отчётность по продажам", "hours": 14,
             "detail": "выгрузки из кабинетов, сведение в таблицу, график"},
        ],
    },
    {
        "id": "it",
        "label": "IT / продукт",
        "blurb": "фидбек, баг-репорты, бэклог и метрики разлетаются по чатам",
        "roles": [
            {"id": "l1", "label": "L1-поддержка", "salary": 65000},
            {"id": "analyst", "label": "Аналитик", "salary": 130000},
            {"id": "qa", "label": "Тестировщик", "salary": 110000},
            {"id": "pm", "label": "Проджект-менеджер", "salary": 145000},
        ],
        "ops": [
            {"id": "feedback", "label": "Разбор фидбека и баг-репортов", "hours": 20,
             "detail": "чаты, почта, сториз — перенос в задачу с деталями"},
            {"id": "backlog", "label": "Ведение бэклога и задач", "hours": 16,
             "detail": "ручные апдейты статусов, приоритеты, комментарии"},
            {"id": "metrics", "label": "Сбор метрик и отчётов", "hours": 14,
             "detail": "запросы, выгрузки, сведение в дашборд вручную"},
            {"id": "onboard", "label": "Онбординг и дожим клиентов", "hours": 15,
             "detail": "шаблонные письма, контроль шагов, напоминания"},
            {"id": "ticket", "label": "Контроль тикетов и статусов", "hours": 12,
             "detail": "обход очереди, эскалации, ответы «где моя задача»"},
            {"id": "release", "label": "Релизные заметки и комментарии", "hours": 8,
             "detail": "сбор изменений, тексты, рассылка"},
        ],
    },
    {
        "id": "production",
        "label": "Производство",
        "blurb": "заявки, склад, смены и документы сходятся в одну таблицу у одного человека",
        "roles": [
            {"id": "dispatch", "label": "Диспетчер", "salary": 70000},
            {"id": "supply", "label": "Менеджер закупок", "salary": 100000},
            {"id": "store", "label": "Кладовщик", "salary": 60000},
            {"id": "master", "label": "Мастер смены", "salary": 95000},
        ],
        "ops": [
            {"id": "orders", "label": "Приём и распределение заявок", "hours": 20,
             "detail": "телефон, чат, таблица — и снова по кругу"},
            {"id": "stock", "label": "Сверка остатков на складе", "hours": 18,
             "detail": "пересчёт, поиск расхождений, акты"},
            {"id": "plan", "label": "Планирование смен и загрузки", "hours": 14,
             "detail": "ручная расстановка людей и заказов по дням"},
            {"id": "doc", "label": "Накладные, акты и ТТН", "hours": 12,
             "detail": "заполнение, печать, отправка контрагентам"},
            {"id": "supply", "label": "Контроль поставок и оплат", "hours": 12,
             "detail": "напоминания поставщикам, сверка документов"},
            {"id": "report", "label": "Отчёты по простоям и браку", "hours": 10,
             "detail": "выгрузка из учётки, сведение в таблицу"},
        ],
    },
    {
        "id": "services",
        "label": "Услуги / бьюти",
        "blurb": "запись, напоминания и ответы в мессенджерах отнимают весь день у администратора",
        "roles": [
            {"id": "admin", "label": "Администратор", "salary": 55000},
            {"id": "dispatch", "label": "Диспетчер заявок", "salary": 65000},
            {"id": "content", "label": "Контент-менеджер", "salary": 60000},
            {"id": "market", "label": "Маркетолог", "salary": 95000},
        ],
        "ops": [
            {"id": "record", "label": "Запись и переносы клиентов", "hours": 22,
             "detail": "телефон, WhatsApp, календарь — одно и то же по десять раз"},
            {"id": "chat", "label": "Ответы в мессенджерах и звонки", "hours": 20,
             "detail": "прайс, свободные окна, адрес, как записаться"},
            {"id": "remind", "label": "Напоминания о визите", "hours": 14,
             "detail": "ручная рассылка накануне, подтверждение"},
            {"id": "revenue", "label": "Отчёт по выручке и загрузке", "hours": 10,
             "detail": "выгрузка из CRM, сведение в таблицу"},
            {"id": "social", "label": "Посты и сторис по шаблону", "hours": 10,
             "detail": "одинаковые рубрики, ручная публикация"},
            {"id": "supply", "label": "Закупка расходников", "hours": 8,
             "detail": "остатки, заявка поставщику, сверка"},
        ],
    },
    {
        "id": "finance",
        "label": "Финансы / учёт",
        "blurb": "первичка, платежи и сверки приходят каждый день и каждый день их кто-то ищет руками",
        "roles": [
            {"id": "account", "label": "Бухгалтер", "salary": 85000},
            {"id": "check", "label": "Оператор сверок", "salary": 65000},
            {"id": "finman", "label": "Финменеджер", "salary": 130000},
            {"id": "kadro", "label": "Кадровик", "salary": 70000},
        ],
        "ops": [
            {"id": "primary", "label": "Сбор и сверка первички", "hours": 24,
             "detail": "почта, ЭДО, папки, поиск недостающих документов"},
            {"id": "bank", "label": "Разнос платежей и выписок", "hours": 18,
             "detail": "ручное отнесение, назначения, разбивки"},
            {"id": "invoice", "label": "Счета, акты и УПД", "hours": 14,
             "detail": "создание документов по заявкам, отправка клиентам"},
            {"id": "budget", "label": "Отчёты по расходам и бюджету", "hours": 14,
             "detail": "выгрузки, сведение план/факта в таблицу"},
            {"id": "close", "label": "Закрытие периода и реконсиляция", "hours": 16,
             "detail": "сверки с контрагентами, поиск расхождений"},
            {"id": "remind", "label": "Напоминания по оплатам", "hours": 10,
             "detail": "контроль дебиторки, письма и звонки"},
        ],
    },
    {
        "id": "marketing",
        "label": "Маркетинг",
        "blurb": "заявки приходят из пяти источников, и каждую кто-то открывает, копирует и прозванивает",
        "roles": [
            {"id": "target", "label": "Таргетолог", "salary": 100000},
            {"id": "smm", "label": "SMM-менеджер", "salary": 70000},
            {"id": "content", "label": "Контент-менеджер", "salary": 65000},
            {"id": "analyst", "label": "Маркетинговый аналитик", "salary": 120000},
        ],
        "ops": [
            {"id": "leads", "label": "Разбор заявок из форм и чатов", "hours": 24,
             "detail": "открыть почту или таблицу, перенести в CRM, поставить статус"},
            {"id": "crm", "label": "Перенос лидов между системами", "hours": 16,
             "detail": "копирование полей, дедупликация, ручные комментарии"},
            {"id": "camp", "label": "Сбор отчётов по кампаниям", "hours": 14,
             "detail": "выгрузки из кабинетов, сведение CPL/ДРР в таблицу"},
            {"id": "social", "label": "Ответы в комментах и директе", "hours": 15,
             "detail": "одни и те же вопросы про цену, сроки, адрес"},
            {"id": "brief", "label": "ТЗ креативам и подрядчикам", "hours": 12,
             "detail": "повторяющиеся брифы, правки, контроль сроков"},
            {"id": "pay", "label": "Контроль оплат и документов", "hours": 10,
             "detail": "сверка актов, напоминания, загрузка в учётную систему"},
        ],
    },
]

TOOLS = [
    {"id": "chat", "label": "Чат-бот", "factor": 0.65,
     "impl": (60000, 150000), "support": (8000, 15000),
     "what": "бот закрывает повторяющиеся обращения и заявки: отвечает по "
             "сценариям, собирает данные и кладёт их туда, где нужно",
     "pilot": "перевести 5–10 самых частых вопросов на сценарии бота"},
    {"id": "agent", "label": "AI-агент", "factor": 0.85,
     "impl": (90000, 250000), "support": (15000, 30000),
     "what": "модель читает входящее сообщение или документ, понимает смысл, "
             "готовит черновик ответа и передаёт человеку только спорное",
     "pilot": "автоответы и суммаризация обращений с проверкой человеком"},
    {"id": "n8n", "label": "n8n-воркфлоу", "factor": 0.75,
     "impl": (40000, 120000), "support": (5000, 12000),
     "what": "связки между системами: заявка → задача → уведомление → отчёт, "
             "без копирования поля из окна в окно",
     "pilot": "сквозная связка «источник → система → уведомление»"},
    {"id": "tracker", "label": "Интеграция Яндекс Трекер", "factor": 0.5,
     "impl": (30000, 90000), "support": (4000, 10000),
     "what": "задачи создаются сами: из форм, чатов и писем, с полями, "
             "исполнителем и автоматическими статусами",
     "pilot": "вебхук на создание задач плюс автоназначение и уведомления"},
    {"id": "full", "label": "Комплексная автоматизация", "factor": 0.9,
     "impl": (200000, 500000), "support": (25000, 50000),
     "what": "контур процесса целиком: бот + воркфлоу + трекер + дашборды, "
             "человек остаётся только на исключениях",
     "pilot": "один процесс от входа заявки до отчёта, потом тираж на соседние"},
]

FIRE_TOOL = {"id": "fire", "label": "Уволить всех 🙂", "factor": 1.0,
             "what": "радикальный метод, который мы не рекомендуем"}

ALL_TOOLS = TOOLS + [FIRE_TOOL]

DISCLAIMER = (
    "<b>Дисклеймер.</b> Расчёт ориентировочный: часы и ставки — из ваших ответов "
    "и средних по рынку, бюджет — типовой диапазон. Не оферта и не гарантия "
    "экономии; точная оценка — после разбора процесса."
)

# Кнопки количества: 1–5 отдельными кнопками, дальше диапазоны-оценки.
# Подписи и payload'ы, клавиатуру собирает keyboard_count() через btn() —
# без поля "type" MAX молча выбрасывает клавиатуру целиком.
COUNT_BUTTONS = [
    [("1", "opt:c:1"), ("2", "opt:c:2"), ("3", "opt:c:3"),
     ("4", "opt:c:4"), ("5", "opt:c:5")],
    [("6–10", "opt:c:8"), ("11–25", "opt:c:18")],
    [("Своё число", "opt:c:x")],
]
# диапазоны считаем по середине оценки
COUNT_RANGE = {"opt:c:8": 8, "opt:c:18": 18}


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
    parts = []
    current = ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= size:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(block) > size:
            cut = block.rfind("\n", 0, size)
            cut = cut if cut > size // 2 else size
            parts.append(block[:cut].rstrip())
            block = block[cut:].lstrip()
        current = block
    if current:
        parts.append(current)
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


PEOPLE_FORMS = ("человек", "человека", "человек")


def roles_summary(session):
    """Один человек — «вы сами»; команда — «роль × сколько»."""
    if session.get("mode") == "self":
        return "вы один, всё на вас"
    return ", ".join(f"{esc(r['label'])} × {r['count']}" for r in session["roles"])


def people_phrase(count):
    return f"{count} {plural(count, PEOPLE_FORMS)}"


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
def keyboard_sphere():
    return [[btn(s["label"], f"opt:s:{s['id']}")] for s in SPHERES]


def keyboard_mode():
    return [[btn("Только я", "opt:m:self"), btn("Несколько человек", "opt:m:team")],
            [btn("🔄 Начать заново", "opt:reset")]]


def keyboard_roles(session):
    """Шаг 2: отмечаем несколько специалистов сразу."""
    sphere = sphere_by_id(session.get("sphere")) or SPHERES[0]
    rows = []
    for i, role in enumerate(sphere["roles"]):
        mark = "✅" if i in session.get("role_pick", []) else "▫️"
        rate = money(hour_rate(role["salary"]))
        rows.append([btn(f"{mark} {role['label']} · {rate}/ч", f"opt:r:{i}")])
    rows.append([btn("Сколько их?", "opt:r:ok"), btn("Сбросить выбор", "opt:r:clear")])
    rows.append([btn("🔄 Начать заново", "opt:reset")])
    return rows


def keyboard_count():
    """1–5 отдельными кнопками, затем диапазоны и свой вариант."""
    return [[btn(label, payload) for label, payload in row] for row in COUNT_BUTTONS]


def keyboard_more():
    """Клавиатура для старых сообщений: там шаг 2 был по одной специальности."""
    return [[btn("Добавить специальность", "opt:n:more")],
            [btn("Считаем рутину", "opt:n:calc")],
            [btn("🔄 Начать заново", "opt:reset")]]


def keyboard_ops(session):
    sphere = sphere_by_id(session.get("sphere")) or SPHERES[0]
    rows = []
    for i, op in enumerate(sphere["ops"]):
        mark = "✅" if op["id"] in session["ops"] else "▫️"
        rows.append([btn(f"{mark} {op['label']} · {op['hours']} ч", f"opt:o:{op['id']}")])
    rows.append([btn("Посчитать рутину", "opt:o:ok"), btn("Сбросить выбор", "opt:o:clear")])
    rows.append([btn("🔄 Начать заново", "opt:reset")])
    return rows


def keyboard_tools_marked(session):
    rows = []
    for tool in ALL_TOOLS:
        if tool["id"] == "fire":
            rows.append([btn(tool["label"], "opt:t:fire")])
            continue
        mark = "✅" if tool["id"] in session["tools"] else "▫️"
        rows.append([btn(f"{mark} {tool['label']}", f"opt:t:{tool['id']}")])
    rows.append([btn("Показать экономию", "opt:t:ok"), btn("Сбросить", "opt:t:clear")])
    rows.append([btn("🔄 Начать заново", "opt:reset")])
    return rows


def keyboard_final():
    return [[btn("💬 Отправить заявку", "opt:fb")],
            [btn("Пройти заново", "opt:again")]]


# ── Тексты ───────────────────────────────────────────────────────────────────
def text_intro():
    return (
        "Оптимизатор — бот, который помогает оптимизировать ваши рабочие процессы.\n\n"
        "Шесть коротких вопросов. На выходе: часы рутины, их стоимость, экономия "
        "и чек-лист внедрения.\n\n<b>Шаг 1 из 6 · Сфера деятельности</b>"
    )


def text_mode(session):
    return (
        f"<b>{esc(session['sphere_label'])}</b> — принято.\n\n"
        "<b>Шаг 2 из 6 · Кого оптимизируем?</b>\n"
        "Вся рутина на вас — жмите «Только я»."
    )


def text_roles(session):
    sphere = sphere_by_id(session.get("sphere")) or SPHERES[0]
    picked = [capital(sphere["roles"][i]["label"])
              for i in sorted(session.get("role_pick") or []) if i < len(sphere["roles"])]
    lines = [
        "<b>Шаг 2 из 6 · Отметьте специалистов</b>",
        "",
        "Можно несколько галочек — потом спрошу, сколько человек в каждой роли.",
    ]
    if picked:
        lines += ["", "Отмечено: " + ", ".join(esc(x) for x in picked) + "."]
    return "\n".join(lines)


def text_count(session):
    role = session["pending_role"]
    left = len(session.get("role_queue") or [])
    further = f" Дальше ещё {plural(left, ('роль', 'роли', 'ролей'))}." if left else ""
    return (
        f"<b>{esc(role['label'])}</b> — сколько таких?\n"
        f"Ставка {money(role['rate'])}/ч.{further}\n"
        "Кнопки 1–5, «6–10», «11–25» — или число сообщением."
    )


def text_team(session):
    roles = ", ".join(f"{esc(r['label'])} × {r['count']}" for r in session["roles"])
    return f"Команда: {roles}.\nВ расчёте <b>{people_phrase(headcount(session))}</b>."


def text_ops(session):
    sphere = sphere_by_id(session.get("sphere")) or SPHERES[0]
    total = sum(op["hours"] for op in sphere["ops"] if op["id"] in session["ops"])
    lines = [
        "<b>Шаг 3 из 6 · Что повторяете руками?</b>",
        "",
        "Отмечайте повторы, тап ещё раз — снять отметку.",
        "",
    ]
    for i, op in enumerate(sphere["ops"], start=1):
        mark = "✅" if op["id"] in session["ops"] else "▫️"
        lines.append(f"{mark} {i}. {esc(op['label'])} · {op['hours']} ч/мес")
    n = len(session["ops"])
    lines += ["", f"Отмечено {n} {plural(n, ('операция', 'операции', 'операций'))} — "
                  f"<b>{total} ч/мес</b> на человека."]
    return "\n".join(lines)


def text_tools(session, calc):
    lines = [
        "<b>Шаг 5 из 6 · Чем убираем рутину?</b>",
        "",
        "Отметьте то, что реально рассматриваете — можно несколько, эффекты складываются.",
        "",
    ]
    for i, tool in enumerate(ALL_TOOLS):
        if tool["id"] == "fire":
            lines.append(f"▫️ {i + 1}. {esc(tool['label'])} — в расчёт не пойдёт")
            continue
        mark = "✅" if tool["id"] in session["tools"] else "▫️"
        lines.append(f"{mark} {i + 1}. {esc(tool['label'])} — до "
                     f"{int(round(tool['factor'] * 100))}% времени рутины")
    lines += ["", f"Рутины сейчас: <b>{fmt(calc['hours'])} ч/мес</b>."]
    return "\n".join(lines)


def text_routine(session, calc):
    ops = "\n".join(f"• {esc(op['label'])} — <b>{op['hours']} ч/мес</b> на человека "
                     f"({esc(op['detail'])})" for op in calc["ops"])
    return [
        (
            "<b>Шаг 4 из 6 · Что получилось</b>\n\n"
            f"{esc(calc['sphere_label'])}, {roles_summary(session)}. "
            f"Повторов — <b>{calc['per_person']} ч/мес на человека</b>, "
            f"это ~{calc['share']}% месяца.\n\n"
            f"Операции в расчёте:\n{ops}"
        ),
        (
            f"<b>Часы рутины:</b> {fmt(calc['hours'])} ч/мес на "
            f"{people_phrase(calc['people'])} · {fmt(calc['hours'] * 12)} ч/год\n"
            f"<b>Стоимость:</b> ~<b>{money(calc['cost_month'])}/мес</b> по ставке "
            f"{money(calc['rate'])}/ч · {money(calc['cost_year'])} в год"
        ),
    ]


def text_fire(calc):
    return FIRE_TEXT.replace("{money}", money(calc["cost_month"]))


FIRE_TEXT = (
    "«Уволить всех» — и рутина исчезнет вместе с процессом 🙂\n\n"
    "<b>Экономия 100% часов: {money}/мес.</b>\n\n"
    "Минус: вместе с часами уйдут заявки, клиенты и отчётность. Шутка — но ровно "
    "про масштаб проблемы: рутину надо убирать совсем, а не делать быстрее.\n\n"
    "В расчёт не беру — отмечайте настоящие инструменты."
)


def text_savings(session, calc, sav):
    names = ", ".join(esc(t["label"]) for t in sav["tools"])
    freed = sav["saved_hours"] / max(1, calc["people"])
    top = "\n".join(
        f"• {esc(op['label'])} ({op['hours']} ч/мес на человека): {esc(op['detail'])}"
        for op in sorted(calc["ops"], key=lambda o: -o["hours"])[:3])
    return [
        (
            "<b>Шаг 6 из 6 · Экономия</b>\n\n"
            f"<b>Инструменты:</b> {names}\n"
            f"<b>Забираем:</b> {int(round(sav['factor'] * 100))}% времени рутины "
            "(потолок честной автоматизации ~92% — исключения всегда остаются)\n\n"
            f"• Часы: {fmt(calc['hours'])} → {fmt(calc['hours'] - sav['saved_hours'])} "
            f"ч/мес руками, экономия <b>{fmt(sav['saved_hours'])} ч/мес</b> · "
            f"{fmt(sav['saved_hours'] * 12)} ч/год\n"
            f"• Деньги: <b>{money(sav['saved_money'])}/мес</b> · "
            f"{money(sav['saved_year'])} в год\n"
            f"• На человека: <b>{fmt(freed)} ч/мес</b> свободно — "
            f"~{round(freed / WORK_HOURS * 100)}% его месяца"
        ),
        "<b>Что освобождается первым:</b>\n" + top,
    ]


def text_checklist(calc, sav):
    ops = sorted(calc["ops"], key=lambda o: -o["hours"])
    first = ops[0]["label"] if ops else "повторяющаяся операция"
    first_hours = ops[0]["hours"] if ops else 0
    second = ops[1]["label"] if len(ops) > 1 else "соседние операции"
    pilot = sav["tools"][0]["pilot"] if sav["tools"] else "переводим шаг в сценарий"
    tool_names = ", ".join(t["label"].lower() for t in sav["tools"]) or "связки"
    steps = [
        ("Зафиксировать процесс как есть",
         f"Опишите «{esc(first)}» по шагам: кто начинает, где живут данные, что "
         "значит «готово».", "0,5 дня"),
        ("Выбрать один пилот",
         f"Берём самое дорогое — «{esc(first)}» ({first_hours} ч/мес на человека): "
         f"{esc(pilot)}. Остальное после первого результата.", "1 неделя"),
        ("Подготовить данные и доступы",
         "Регламенты, таблицы-источники, API-ключи и права, ответственный за "
         "исключения.", "2–3 дня"),
        ("Собрать MVP и гонять рядом с руками",
         f"Первая версия ({esc(tool_names)}) работает вместе с человеком: автомат "
         "делает, человек правит правила — так находятся крайние случаи.",
         "1–2 недели"),
        ("Замерить часы до и после",
         "Сколько уходило и сколько осталось. Только замер отличает «кажется, "
         "быстрее» от реальной экономии.", "2 недели"),
        ("Передать владельцу и задокументировать",
         "Кто правит сценарии, куда писать при сбое, как добавить операцию.",
         "1 день"),
        ("Тиражировать",
         f"По «{esc(first)}» есть цифра — добавляем «{esc(second)}» и остальное из "
         "расчёта: они встают на готовый контур дешевле.", "1–2 недели на операцию"),
    ]
    lines = ["<b>Чек-лист внедрения</b>", ""]
    for i, (title, body, when) in enumerate(steps, start=1):
        lines.append(f"<b>{i}. {title}</b> — {body}"
                     f"\n   <i>{when}</i>\n")
    return "\n".join(lines).rstrip()


def text_rollout(sav):
    what = "\n".join(f"• <b>{esc(t['label'])}</b>: {esc(t['what'])}"
                      for t in sav["tools"])
    support = ""
    if sav["support_high"]:
        support = (f"\n<b>Сопровождение:</b> {money(sav['support_low'])}–"
                   f"{money(sav['support_high'])}/мес.")
    payback = ""
    if sav["payback"]:
        months = sav["payback"]
        pretty = (f"{months:.1f}".replace(".", ",").rstrip("0").rstrip(",")
                  if months < 10 else str(int(round(months))))
        payback = (f"\nСредний бюджет окупается за <b>~{pretty} мес</b> при экономии "
                   f"{money(sav['saved_money'])}/мес.")
    return [
        (
            "<b>Стоимость внедрения</b>\n\n"
            f"{what}\n\n<b>Примерный бюджет:</b> {money(sav['impl_low'])}–"
            f"{money(sav['impl_high'])} разово.{support}{payback}"
        ),
        DISCLAIMER + "\n\nХотите конкретный план под ваш процесс — отправьте заявку "
        "кнопкой ниже.",
    ]


TEXT_FEEDBACK_PROMPT = (
    "Опишите процесс одним сообщением: что делаете руками, в каких системах, "
    "сколько это времени. Контакт, если нужен ответ.\n\n"
    "Перешлю в штаб оптимизации: такие запросы он разбирает сам."
)

TEXT_FEEDBACK_DONE = (
    "Отправил в штаб оптимизации вместе с вашим расчётом — обычно отвечаем в "
    "течение дня.\n\nПересчитать другой процесс — «Оптимизатор заново» ниже."
)


# ── Диспетчер кнопок ─────────────────────────────────────────────────────────
TEXT_STALE = ("Похоже, это кнопка из прошлого расчёта. "
              "Нажмите «Начать заново» — пойдём с начала.")

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

    if kind == "reset" or kind == "again":
        return start(session)
    if kind == "start":
        return start(session)
    if kind == "cancel":
        return {**session, "step": "done"}, emit(
            "Оптимизатор остановил. Продолжить — «Начать заново» или /старт.")

    handler = HANDLERS.get(kind)
    if not handler:
        return session, emit("Не распознал кнопку — нажмите «Начать заново», "
                             "чтобы начать заново.")
    if kind in STALE_GUARDED and is_stale(session):
        # тап по клавиатуре из сброшенного/просроченного расчёта
        return session, emit(TEXT_STALE)
    return handler(session, arg)


def on_sphere(session, arg):
    sphere = sphere_by_id(arg)
    if not sphere:
        return session, emit("Такой сферы нет в списке — выберите из кнопок.")
    session.update(sphere=sphere["id"], sphere_label=sphere["label"], mode=None,
                   roles=[], role_pick=[], role_queue=[], pending_role=None,
                   last_role=None, ops=[], tools=[], calc=None, sav=None,
                   step="mode")
    return session, emit(text_mode(session), keyboard_mode())


def on_mode(session, arg):
    if session["step"] != "mode":
        return session, emit("Сначала выберите сферу — кнопки выше.")
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
            return session, emit("Отметьте хотя бы одного специалиста — кнопки выше, "
                                 "можно сразу несколько.")
        session["role_queue"] = [{"label": sphere["roles"][i]["label"],
                                  "rate": hour_rate(sphere["roles"][i]["salary"])}
                                 for i in picks]
        session["role_pick"] = []
        return ask_count(session)
    role = role_at(sphere, arg)
    if not role:
        return session, emit("Специальность не распознана — выберите из списка.")
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
        return session, emit(f"Сколько человек в роли «{esc(role['label'])}»? "
                             "Напишите число, например 7.")
    count = COUNT_RANGE.get(f"opt:c:{arg}")
    if count is None:
        try:
            count = int(arg)
        except ValueError:
            return session, emit("Не понял количество — выберите кнопку 1–5 "
                                 "или напишите число сообщением.")
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
    session["last_role"] = f"{label} × {count}"


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
        return session, emit(TEXT_STALE)
    if arg == "clear":
        session["ops"] = []
        return session, emit(text_ops(session), keyboard_ops(session))
    if arg == "ok":
        if not session["ops"]:
            return session, emit("Отметьте хотя бы одну операцию — иначе считать нечего. "
                                 "Можно начать заново кнопкой внизу.")
        calc = compute_routine(session)
        session["calc"] = calc
        session["step"] = "tools"
        return session, emit(text_routine(session, calc), keyboard_tools_marked(session))
    op = next((o for o in sphere["ops"] if o["id"] == arg), None)
    if not op:
        return session, emit("Такой операции нет в списке — выберите из кнопок.")
    ops = list(session["ops"])
    ops.remove(op["id"]) if op["id"] in ops else ops.append(op["id"])
    session["ops"] = ops
    return session, emit(text_ops(session), keyboard_ops(session))


def on_tool(session, arg):
    if session.get("step") not in ("tools", "savings", "rollout"):
        return session, emit(TEXT_STALE)
    calc = session.get("calc") or compute_routine(session)
    session["calc"] = calc
    if arg == "clear":
        session["tools"] = []
        return session, emit(text_tools(session, calc), keyboard_tools_marked(session))
    if arg == "ok":
        if not session["tools"]:
            return session, emit("Выберите инструмент 🙂 «Уволить всех» тоже считается.")
        sav = compute_savings(session, calc)
        session["sav"] = sav
        session["step"] = "savings"
        return session, emit(text_savings(session, calc, sav),
                             [[btn("Дальше: чек-лист внедрения", "opt:t:plan")],
                              [btn("Выбрать другие инструменты", "opt:t:back")]])
    if arg == "plan":
        sav = session.get("sav") or compute_savings(session, calc)
        session["step"] = "rollout"
        messages = emit(text_checklist(calc, sav))
        messages += emit(text_rollout(sav), keyboard_final())
        return session, messages
    if arg == "back":
        session["step"] = "tools"
        return session, emit(text_tools(session, calc), keyboard_tools_marked(session))
    tool = tool_by_id(arg)
    if not tool:
        return session, emit("Такого инструмента нет в списке.")
    if tool["id"] == "fire":
        return session, emit(text_fire(calc)) \
            + emit(text_tools(session, calc), keyboard_tools_marked(session))
    tools = list(session["tools"])
    tools.remove(tool["id"]) if tool["id"] in tools else tools.append(tool["id"])
    session["tools"] = tools
    return session, emit(text_tools(session, calc), keyboard_tools_marked(session))


def on_feedback(session, _arg=None):
    session["step"] = "feedback"
    return session, emit(TEXT_FEEDBACK_PROMPT)


HANDLERS = {
    "s": on_sphere,
    "m": on_mode,
    "r": on_role,
    "c": on_count,
    "n": on_more,
    "o": on_op,
    "t": on_tool,
    "fb": on_feedback,
}


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
            return session, emit("Нужно число — например «7» или «примерно 12». "
                                 "Кнопки 1–5 тоже работают.")
        record_role(session, role["label"], int(match.group()), role["rate"])
        session["pending_role"] = None
        return ask_count(session)

    if step == "feedback":
        calc = session.get("calc") or {}
        summary = ""
        if calc:
            summary = (f"\n\nЧерновик расчёта: {calc.get('sphere_label')} · "
                       f"{calc.get('people')} чел · {fmt(calc.get('hours', 0))} ч/мес "
                       f"рутины · ~{money(calc.get('cost_month', 0))}/мес.")
        messages = emit(
            TEXT_FEEDBACK_DONE + summary,
            [[btn("🔧 Оптимизатор заново", "opt:again")],
             [{"type": "link", "text": "Канал про автоматизацию",
               "url": CHANNEL_URL}]])
        # «admin» — сигнал транспорту переслать этот текст владельцу
        messages[-1]["admin"] = text
        return {**session, "step": "done"}, messages

    return session, []
