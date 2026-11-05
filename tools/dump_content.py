"""Собрать content.json из код-дефолтов — заготовка файла для админки.

content.py умеет накладывать content.json поверх дефолтов, но самому файлу нужно
с чего-то-start: дамп содержит все ключи сразу, с текущими формулировками, —
админка показывает их и правит, а не гадает, какой ключ существует.

Вместе с файлом пишется content.schema.json — справочник для админки: какие
переменные допустимы в каждом шаблоне, чем их подставить в предпросмотре, какие
поля вообще не видны читателю, как подписывать служебные поля каталога и какие
пределы у сообщения MAX. Держать его в head приходится здесь, а не в голове
человека: правка в content.py без обновления схемы — это сломанный предпросмотр.

    python3 tools/dump_content.py            # перезаписать content.json и схему
    python3 tools/dump_content.py --keep     # долить только ключи, которых нет в файле
    python3 tools/dump_content.py --check    # ничего не писать, сказать, есть ли расхождения
    python3 tools/dump_content.py --schema   # только content.schema.json

--keep нужен после того, как в коде появилась новая строка: правки владельца в
файле остаются нетронутыми, а недостающий ключ доезжает со значением из кода.
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import content  # noqa: E402
import optimizer  # noqa: E402

GROUPS = ("meta", "texts", "buttons", "words", "catalog")
SCHEMA_FILE = os.path.join(ROOT, "content.schema.json")

# Как админка раскладывает ключи по вкладкам: совпадение по префиксу пути.
# Порядок важен — проверяется сверху вниз, поэтому сначала частные группы.
UI_GROUPS = [
    {"id": "greeting", "title": "Приветствие и выбор режима",
     "prefixes": ["texts.intro", "texts.mode", "texts.people_phrase", "words."]},
    {"id": "people", "title": "Шаг 1 — кого оптимизируем",
     "prefixes": ["texts.roles", "texts.count", "texts.team", "buttons.mode",
                  "buttons.roles", "buttons.more", "buttons.reset", "buttons.start"]},
    {"id": "ops", "title": "Шаг 2 — рутина",
     "prefixes": ["texts.ops", "buttons.ops"]},
    {"id": "routine", "title": "Результат 1 — часы и стоимость",
     "prefixes": ["texts.routine", "texts.savings"]},
    {"id": "tools", "title": "Шаг 3 — инструменты",
     "prefixes": ["texts.tools", "texts.fire", "buttons.tools", "buttons.marks"]},
    {"id": "brief", "title": "Отчёт в чате (короткая форма)",
     "prefixes": ["texts.brief"]},
    {"id": "fallback", "title": "Отчёт без PDF (текстом в чате)",
     "prefixes": ["texts.fallback"]},
    {"id": "checklist", "title": "Чек-лист внедрения",
     "prefixes": ["texts.checklist"]},
    {"id": "money", "title": "Стоимость, окупаемость, дисклеймер",
     "prefixes": ["texts.rollout"]},
    {"id": "report", "title": "Отчёт (PDF и заявка владельцу)",
     "prefixes": ["texts.report"]},
    {"id": "lead", "title": "Заявка и финальные кнопки",
     "prefixes": ["texts.feedback", "texts.pdf", "buttons.final",
                  "buttons.after_lead"]},
    {"id": "errors", "title": "Ошибки и подсказки",
     "prefixes": ["texts.errors", "texts.stale", "texts.cancel",
                  "texts.unknown_button"]},
]

# Названия полей каталога: в JSON они служебные, человеку надо подписывать.
FIELD_LABELS = {
    "id": "Служебный ключ (его видит только расчёт)",
    "label": "Название",
    "blurb": "Описание сферы",
    "salary": "Зарплата в месяц, ₽",
    "hours": "Часов в месяц",
    "detail": "Как это выглядит руками",
    "factor": "Какую часть забирает автоматизация (0…1)",
    "impl": "Бюджет внедрения, ₽ [от, до]",
    "support": "Поддержка в месяц, ₽ [от, до]",
    "what": "Что делает",
    "pilot": "С чего начать",
    "emoji": "Эмодзи",
    "title": "Заголовок",
    "short": "Строка в чате",
    "body": "Пояснение",
    "when": "Срок",
    "result": "Результат шага",
    "owner": "Кто отвечает",
    "point": "Название точки",
    "check": "Что проверяем",
    "channel_url": "Ссылка на канал",
    "internal": "Служебное",
}


def current():
    """Эффективный контент: дефолты кода плюс то, что уже лежит в файле."""
    return content.effective()


def missing(base, overlay):
    """Ключи дефолтов, которых нет в overlay (глубина по словарям)."""
    out = []
    for key, value in (base or {}).items():
        if not isinstance(key, str):
            continue
        if key not in (overlay or {}):
            out.append(key)
        elif isinstance(value, dict) and isinstance(overlay[key], dict):
            out.extend(f"{key}.{sub}" for sub in missing(value, overlay[key]))
    return out


def extra(base, overlay):
    """Ключи файла, которых нет в коде — их правка не применит никогда."""
    out = []
    for key, value in (overlay or {}).items():
        if key not in (base or {}):
            out.append(key)
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            out.extend(f"{key}.{sub}" for sub in extra(base[key], value))
    return out


def write(tree, path):
    ordered = {"schema": content.SCHEMA}
    for group in GROUPS:
        ordered[group] = tree[group]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(ordered, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


def group_of(path):
    """Вкладке админки — id группы по префиксу пути (первые совпадение важнее)."""
    for group in UI_GROUPS:
        if any(path.startswith(prefix) for prefix in group["prefixes"]):
            return group["id"]
    return "other"


def build_schema():
    """Справочник для админки: предпросмотр, допустимые переменные, пределы MAX.

    Берётся из кода, а не из content.json: админка сверяет правку с тем, что
    умеет бот, а не с тем, что кто-то сохранил в прошлый раз.
    """
    variables = {}
    for group in ("texts", "buttons"):
        for leaf in content._template_paths(content.DEFAULTS[group]):
            path = f"{group}.{leaf}"
            variables[path] = content.variables(path)
    # Слова-формы (люди/роли/операции) — списки, их _template_paths не видит,
    # а админка их показывает: подписи «один специалист / несколько …» правятся.
    for kind, forms in content.DEFAULTS["words"].items():
        for i in range(len(forms)):
            variables[f"words.{kind}.{i}"] = content.variables(f"words.{kind}.{i}")
    paths = [f"{group}.{leaf}"
             for group in ("texts", "buttons")
             for leaf in content._leaf_paths(content.DEFAULTS[group])]
    paths += [f"words.{kind}.{i}"
              for kind, forms in content.DEFAULTS["words"].items()
              for i in range(len(forms))]
    grouped = {}
    for group in UI_GROUPS + [{"id": "other", "title": "Прочее", "prefixes": []}]:
        grouped[group["id"]] = group["title"]
    layout = [{"id": g["id"], "title": g["title"],
               "paths": [p for p in paths if group_of(p) == g["id"]]}
              for g in UI_GROUPS]
    layout.append({"id": "other", "title": "Прочее",
                   "paths": [p for p in paths if group_of(p) == "other"]})
    return {
        "schema": content.SCHEMA,
        "limits": {
            "text_chunk": optimizer.TEXT_CHUNK,      # резка длинных сообщений
            "button_label": 64,                      # подпись кнопки, MAX обрежет
            "payload": 32,                           # callback-данные
            "row_buttons": 7,                        # кнопок в ряду
            "headcount_max": optimizer.MAX_HEADCOUNT,
            "hours_per_month": optimizer.WORK_HOURS,  # из него считается ставка
        },
        "internal": content.internal_fields(),
        "demo": content.DEMO_VALUES,
        "variables": variables,
        # Подстановки каталога едины для всех его полей: их набор — ключи,
        # которые код кладёт в values, а не догадка страницы. Чек-лист подставляет
        # выбранные операции и инструменты, точка контроля — расчётную экономию.
        "catalog_variables": {
            "checklist": sorted(optimizer.checklist_values({"ops": []}, {})),
            "controls": sorted(optimizer.control_values({}, {})),
        },
        # Виды payload'ов, которые dispatcher разбирает: страница сверяет подписи
        # кнопок количества с этим списком, а не с зашитым перечнем.
        "payload_kinds": optimizer.payload_kinds(),
        "groups": layout,
        "group_titles": grouped,
        "field_labels": FIELD_LABELS,
        "defaults": {"texts": content.DEFAULTS["texts"],
                     "buttons": content.DEFAULTS["buttons"],
                     "words": content.DEFAULTS["words"],
                     "catalog": content.DEFAULTS["catalog"],
                     "meta": {"channel_url": content.DEFAULTS["meta"]["channel_url"]}},
    }


def write_schema(tree, path=SCHEMA_FILE):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(tree, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def schema_on_disk(path=SCHEMA_FILE):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return "битый"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=content.CONTENT_FILE,
                        help="куда писать (по умолчанию рядом с content.py)")
    parser.add_argument("--keep", action="store_true",
                        help="сохранить уже написанное, долить только новые ключи")
    parser.add_argument("--schema", action="store_true",
                        help="перезаписать только content.schema.json")
    parser.add_argument("--check", action="store_true",
                        help="только проверить состояние, файл не трогать")
    args = parser.parse_args()

    fresh_schema = build_schema()

    if args.schema:
        path = write_schema(fresh_schema)
        print(f"записана {path} ({os.path.getsize(path)} байт) — "
              f"шаблонов {len(fresh_schema['variables'])}, групп "
              f"{len(fresh_schema['groups'])}")
        return 0

    defaults = json.loads(json.dumps(
        {"schema": content.SCHEMA, **{g: content.DEFAULTS[g] for g in GROUPS}},
        ensure_ascii=False))
    on_disk = None
    if os.path.exists(args.out):
        try:
            with open(args.out, encoding="utf-8") as fh:
                on_disk = json.load(fh)
        except (ValueError, OSError) as exc:
            print(f"::error::{args.out} не читается: {exc}")
            return 1

    if args.check:
        if on_disk is None:
            print(f"::error::{args.out} нет в репозитории — админке нечего показывать")
            return 1
        gaps = missing(defaults, on_disk)
        junk = extra(defaults, on_disk)
        problems = content.problems()
        leaves = sum(len(content._leaf_paths(on_disk.get(g) or {})) for g in GROUPS)
        print(f"content.json: {os.path.getsize(args.out)} байт, значений {leaves}, "
              f"не хватает ключей {len(gaps)}, лишних {len(junk)}, "
              f"шаблонов с чужими переменными {len(problems)}")
        for line in gaps[:10]:
            print("  не хватает:", line)
        for line in junk[:10]:
            print("  лишнее (код его не читает):", line)
        for line in problems[:10]:
            print("  шаблон:", line)
        stored = schema_on_disk()
        schema_stale = None
        if stored is None:
            schema_stale = "content.schema.json нет — админке неоткуда знать " \
                           "переменные и пределы"
        elif stored == "битый":
            schema_stale = "content.schema.json не разобран"
        elif json.dumps(stored, sort_keys=True, ensure_ascii=False) != \
                json.dumps(fresh_schema, sort_keys=True, ensure_ascii=False):
            diff = [k for k in fresh_schema
                    if json.dumps(stored.get(k), sort_keys=True, ensure_ascii=False)
                    != json.dumps(fresh_schema[k], sort_keys=True, ensure_ascii=False)]
            schema_stale = "content.schema.json устарел, расходится по: " + ", ".join(diff)
        if schema_stale:
            print("  схема:", schema_stale)
        return 1 if (gaps or junk or problems or schema_stale) else 0

    if args.keep and on_disk is not None:
        merged = current()
        path = write(merged, args.out)
        schema_path = write_schema(fresh_schema)
        print(f"долито ключей из кода: {len(missing(on_disk, merged))} → {path}")
        print(f"схема админки обновлена: {schema_path}")
        return 0

    path = write(defaults if on_disk is None else current(), args.out)
    schema_path = write_schema(fresh_schema)
    print(f"записан {path} ({os.path.getsize(path)} байт) — "
          f"текстов {len(content._template_paths(defaults['texts']))}, "
          f"подписей кнопок {len(content._template_paths(defaults['buttons']))}, "
          f"сфер {len(defaults['catalog']['spheres'])}")
    print(f"записана {schema_path} ({os.path.getsize(schema_path)} байт) — "
          f"справочник для админки")
    return 0


if __name__ == "__main__":
    sys.exit(main())
