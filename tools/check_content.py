"""Врата контента: что должно быть верно в content.json до коммита.

Админка правит JSON и коммитит его в main — этот чекер стоит между «сохранил»
и «уехало в прод». Он проверяет три вещи: файл полный и знакомый коду, каждый
текст рендерится с теми переменными, которые код действительно передаёт, и
структура каталога не сломана (уникальные id, числа в ставках, payload'ы
кнопок тех видов, которые dispatcher понимает).

Второй блок — поведение на плохом файле: опечатка в JSON, ключ, которого нет в
коде, {переменная}, которую код не подставляет, дубликат id в каталоге. Бот не
должен падать и не должен показывать пустое сообщение: он отдаёт код-дефолт и
пишет в предупреждения.

Третий блок — вёрстка пустыми строками: их число сверяется на каждом звене
цепочки (файл → оверлей → подстановка переменных → резка на сообщения). Теряются
они тихо, а в чате превращаются в склеенные строки.

    python3 tools/check_content.py            # проверки
    python3 tools/check_content.py --dump     # перегенерить content.json из кода

Заодно проверяет, что подписи кнопок не содержат HTML: MAX рисует клавиатуру
без разметки, тег там видно глазами как мусор.
"""
import argparse
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTENT_PATH = os.path.join(ROOT, "content.json")
# Явно до импорта: чекер обязан видеть тот же файл, что увидит цикл Actions.
os.environ["OPTIMIZER_CONTENT"] = CONTENT_PATH
sys.path.insert(0, ROOT)

import content  # noqa: E402
import optimizer  # noqa: E402

results = []


def check(label, condition, detail=""):
    ok = bool(condition)
    results.append(ok)
    print(f"{'✅' if ok else '❌'} {label}" + (f" — {detail}" if detail else ""))
    return ok


def paths_of(tree):
    return content._template_paths(tree)


def render(path, values=None):
    """Текст по ключу с демо-подстановками: так его увидит человек."""
    values = values if values is not None else content.demo_values()
    return content.format_key(path, values)


def leftovers(text):
    return [name for name in content.BRACE_RE.findall(text or "")]


# ── 1. Файл на месте и похож на ожидаемый ────────────────────────────────────
raw = None
try:
    with open(CONTENT_PATH, encoding="utf-8") as fh:
        raw = json.load(fh)
except (OSError, ValueError) as exc:
    check("content.json читается", False, str(exc)[:120])

check("content.json существует и это объект", isinstance(raw, dict))
check("content.json отдаёт предупреждений ноль", content.warnings() == [],
      str(content.warnings()[:2]))
check("schema совпадает с кодом", isinstance(raw, dict)
      and raw.get("schema") == content.SCHEMA,
      f"в файле {raw.get('schema')!r}, код ждёт {content.SCHEMA!r}")
for group in ("meta", "texts", "buttons", "words", "catalog"):
    check(f"группа {group} на месте", isinstance(raw.get(group), (dict,))
          if group != "meta" else isinstance(raw.get(group), dict),
          str(type(raw.get(group))))

# ── 2. Ни ключа лишнего, ни ключа недостающего ───────────────────────────────
sys.path.insert(0, os.path.join(ROOT, "tools"))
import dump_content  # noqa: E402

defaults = json.loads(json.dumps({"schema": content.SCHEMA,
                                 **{g: content.DEFAULTS[g] for g in dump_content.GROUPS}},
                                 ensure_ascii=False))
gaps = dump_content.missing(defaults, raw)
junk = dump_content.extra(defaults, raw)
check("все ключи кода есть в файле", not gaps, ", ".join(gaps[:4]))
check("в файле нет ключей, которых нет в коде", not junk, ", ".join(junk[:4]))
check("problems() пусто", content.problems() == [], str(content.problems()[:3]))

# ── 3. Каждый текст рендерится с переменными, которые код реально передаёт ───
demo = content.demo_values()
broken, empty, html_bad, unbalanced = [], [], [], []
for path in [f"texts.{p}" for p in paths_of(content.DEFAULTS["texts"])] + \
            [f"buttons.{p}" for p in paths_of(content.DEFAULTS["buttons"])]:
    allowed = content.variables(path)
    unknown = [v for v in allowed if v not in demo]
    if unknown:
        broken.append(f"{path}: в DEMO_VALUES нет {unknown}")
        continue
    out = render(path)
    if leftovers(out):
        broken.append(f"{path}: не подставилось {leftovers(out)}")
    if not out.strip():
        empty.append(path)
    if "None" in out:
        empty.append(f"{path}: в тексте None")
    if path.startswith("buttons.") and ("<" in out or ">" in out):
        html_bad.append(f"{path}: {out[:30]}")
    for tag in ("b", "i"):
        if out.count(f"<{tag}>") != out.count(f"</{tag}>"):
            unbalanced.append(f"{path}: незакрытый <{tag}>")

check("текстов достаточно (не меньше 60 ключей)",
      len(paths_of(content.DEFAULTS["texts"])) >= 60,
      f"{len(paths_of(content.DEFAULTS['texts']))}")
check("каждый шаблон рендерится без остаточных {переменных}", not broken,
      "; ".join(broken[:3]))
check("ни один рендер не пустой и без None", not empty, "; ".join(empty[:3]))
check("подписи кнопок без HTML-разметки", not html_bad, "; ".join(html_bad[:3]))
check("разметка <b>/<i> закрыта во всех текстах", not unbalanced,
      "; ".join(unbalanced[:3]))

# Ключ, который код ни разу не подставляет, — это ловушка: админ видит поле в
# файле, правит его, ждёт перемен в чате, а перемен не будет никогда. Такой ключ
# либо удаляют из content.py, либо забывают провести через renderer.
consumers = "".join(open(os.path.join(ROOT, f), encoding="utf-8").read()
                    for f in ("optimizer.py", "max_bot.py", "report_pdf.py"))
orphaned = []
for group in ("texts", "buttons", "words"):
    for path in paths_of(content.DEFAULTS[group]):
        if f'"{path}"' not in consumers and f'"{group}.{path}"' not in consumers:
            orphaned.append(f"{group}.{path}")
check("каждый ключ контента действительно куда-то подставляется", not orphaned,
      "; ".join(orphaned[:4]))

# Поля-справочники (ставка «я сам», описание сферы) в сообщениях не видны. Они
# должны быть перечислены в meta.internal — админка гасит их и пишет, что правка
# на чат не влияет. Каждый путь списка обязан вести в реальную структуру контента:
# «не ведёт» означает, что поле либо переименовали, либо начали показывать, и
# пора править сам список.
def resolves(path):
    """Существует ли путь вида «catalog.spheres[].blurb» в кодовых дефолтах."""
    def walk(node, keys):
        if not keys:
            return True
        key = keys[0]
        if key.endswith("[]"):
            items = node.get(key[:-2]) if isinstance(node, dict) else None
            if not isinstance(items, list) or not items:
                return False
            return all(walk(item, keys[1:]) for item in items)
        if not isinstance(node, dict) or key not in node:
            return False
        return walk(node[key], keys[1:])

    return walk(content.DEFAULTS, path.split("."))


internal = content.internal_fields()
check("meta.internal непустой и это список строк",
      isinstance(internal, list) and internal
      and all(isinstance(i, str) and i for i in internal), str(internal))
unresolved = [p for p in internal if not resolves(p)]
check("каждый путь meta.internal существует в контенте", not unresolved,
      "; ".join(unresolved))

# ── 4. Каталог ───────────────────────────────────────────────────────────────
errors = content._validate_catalog(content.effective()["catalog"], [])
check("каталог проходит валидацию", not errors, "; ".join(errors[:3]))
spheres = content.spheres()
check("семь сфер, как в калькуляторе", len(spheres) == 7, str(len(spheres)))
check("у каждой сферы есть роли и операции",
      all(s.get("roles") and s.get("ops") for s in spheres))
ids = [s["id"] for s in spheres]
check("id сфер уникальны", len(ids) == len(set(ids)))
check("payload сфер укладывается в лимит MAX (32 символа)",
      all(len(f"opt:s:{i}") <= 32 for i in ids))
tool_ids = [t["id"] for t in content.tools()]
check("id инструментов уникальны и «fire» не среди них",
      len(tool_ids) == len(set(tool_ids)) and "fire" not in tool_ids)
check("факторы инструментов в диапазоне 0…1",
      all(0 < t["factor"] <= 1 for t in content.tools()))
check("бюджеты инструментов не перевёрнуты",
      all(t["impl"][0] <= t["impl"][1] and t["support"][0] <= t["support"][1]
          for t in content.tools()))
check("зарплаты положительные",
      all(r["salary"] > 0 for s in spheres for r in s["roles"])
      and content.self_role()["salary"] > 0)
check("часы операций положительные",
      all(o["hours"] > 0 for s in spheres for o in s["ops"]))

kinds = {p.split(":")[1] for row in content.count_buttons() for _, p in row}
check("виды payload'ов кнопок количества знает dispatcher",
      kinds <= set(optimizer.HANDLERS) | {"reset", "again", "start", "cancel"},
      str(kinds))
range_payloads = set(content.count_range())
buttons_payloads = {p for row in content.count_buttons() for _, p in row}
check("count_range ссылается только на существующие кнопки",
      range_payloads <= buttons_payloads, str(range_payloads - buttons_payloads))
check("значения диапазонов совпадают с серединой оценки",
      all(content.count_range()[p] == int(p.rsplit(":", 1)[1])
          for p in range_payloads if p.rsplit(":", 1)[1].isdigit()))
check("кнопка «Своё число» ведёт на opt:c:x",
      any(p == "opt:c:x" for row in content.count_buttons() for _, p in row))

check("чек-лист внедрения — семь шагов", len(content.checklist()) == 7)
check("у каждого шага чек-листа есть эмодзи, срок и пояснение",
      all(step.get("emoji") and step.get("when") and step.get("body")
          and step.get("short") and step.get("title") for step in content.checklist()))
fill_bad = []
for i, step in enumerate(content.checklist()):
    for field in ("title", "short", "body", "when"):
        out = content.fill(step.get(field), content.demo_values())
        if leftovers(out):
            fill_bad.append(f"[{i}].{field}: {leftovers(out)}")
check("поля чек-листа рендерятся демо-значениями", not fill_bad, "; ".join(fill_bad[:3]))

# ── 5. Клавиатуры после правки контента ──────────────────────────────────────
session = optimizer.new_session()
session.update(sphere="ecom", sphere_label="E-commerce", mode="team",
               role_pick=[0, 1], roles=[{"label": "Оператор", "count": 4, "rate": 375}])
rows = []
for keyboard in (optimizer.keyboard_sphere(), optimizer.keyboard_mode(),
                 optimizer.keyboard_roles(session), optimizer.keyboard_count(),
                 optimizer.keyboard_ops(session),
                 optimizer.keyboard_tools_marked(session), optimizer.keyboard_final(),
                 optimizer.keyboard_after_lead()):
    rows.extend(row for row in keyboard)
buttons_flat = [b for row in rows for b in row]
check("у каждой кнопки есть type",
      all(b.get("type") in ("callback", "link") for b in buttons_flat))
check("у callback-кнопок payload вида opt:… и не длиннее 32",
      all(b.get("type") != "callback"
          or (b["payload"].startswith("opt:") and len(b["payload"]) <= 32)
          for b in buttons_flat))
check("кнопок не больше 7 в ряду", all(len(row) <= 7 for row in rows))
check("ни одна подпись кнопки не пустая",
      all(str(b.get("text") or "").strip() for b in buttons_flat))
unknown_kinds = {b["payload"].split(":")[1] for b in buttons_flat
                 if b.get("payload")} - (set(optimizer.HANDLERS)
                                         | {"reset", "again", "start", "cancel"})
check("все кнопки ведут на известный обработчик", not unknown_kinds, str(unknown_kinds))

# ── 6. Ссылка канала и приоритет окружения ───────────────────────────────────
url = content.channel_url()
check("ссылка канала — https", url.startswith("https://"), url)
os.environ["MAX_CHANNEL_URL"] = "https://max.ru/other"
content.reload()
check("переменная окружения сильнее content.json",
      content.channel_url() == "https://max.ru/other")
del os.environ["MAX_CHANNEL_URL"]
content.reload()
check("без переменной — значение из файла", content.channel_url() == url)

# ── 7. Поведение на битом файле: бот обязан остаться на дефолтах ─────────────
backup = open(CONTENT_PATH, encoding="utf-8").read()
tmp_path = os.path.join(tempfile.mkdtemp(), "content.json")
DEFAULT_INTRO = content.DEFAULTS["texts"]["intro"]


def swap(tree_or_text):
    if isinstance(tree_or_text, str):
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(tree_or_text)
    else:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(tree_or_text, fh, ensure_ascii=False)
    saved, content.CONTENT_FILE = content.CONTENT_FILE, tmp_path
    content.reload()
    # Прод этого не делает: круг Actions — свежий процесс, каталог читается на
    # импорте. Чекеру нужно, иначе optimizer.SPHERES останется из прошлого файла.
    optimizer.refresh_catalog()
    return saved


def restore(saved):
    content.CONTENT_FILE = saved
    content.reload()
    optimizer.refresh_catalog()


saved = swap("{ это не json")
check("битый JSON → предупреждение, а не исключение",
      any("не разобран" in w for w in content.warnings()), str(content.warnings()[:1]))
check("битый JSON → тексты из кода", content.text("intro") == DEFAULT_INTRO)
restore(saved)

saved = swap({"schema": content.SCHEMA, "texts": {"intro": "Привет {who_is_this}"}})
check("чужая переменная в тексте → код-дефолт",
      content.text("intro") == DEFAULT_INTRO)
check("чужая переменная видна в problems()",
      any("who_is_this" in p for p in content.problems()), str(content.problems()[:1]))
restore(saved)

saved = swap({"schema": content.SCHEMA, "texts": {"nope": {"nope": "x"}}})
check("незнакомый ключ пропущен с предупреждением",
      any("незнаком коду" in w for w in content.warnings()), str(content.warnings()[:1]))
check("незнакомый ключ не ломает остальные тексты",
      content.text("intro") == DEFAULT_INTRO)
restore(saved)

bad_catalog = json.loads(json.dumps(content.DEFAULTS, ensure_ascii=False))
bad_catalog["catalog"]["spheres"][0]["roles"][0]["salary"] = "много"
saved = swap(bad_catalog)
check("невалидный каталог → каталог из кода целиком",
      content.spheres() == content.DEFAULTS["catalog"]["spheres"])
check("невалидный каталог описан в предупреждении",
      any("валидацию" in w for w in content.warnings()), str(content.warnings()[:1]))
restore(saved)

dup = json.loads(json.dumps(content.DEFAULTS, ensure_ascii=False))
dup["catalog"]["spheres"][1]["id"] = dup["catalog"]["spheres"][0]["id"]
saved = swap(dup)
check("дубликат id сферы не принимается",
      any("дубликат" in w for w in content.warnings()), str(content.warnings()[:1]))
restore(saved)

fire = json.loads(json.dumps(content.DEFAULTS, ensure_ascii=False))
fire["catalog"]["fire_tool"]["id"] = "fire2"
saved = swap(fire)
check("переименованный fire_tool отклонён (на него смотрит расчёт)",
      any("fire" in w for w in content.warnings()), str(content.warnings()[:1]))
restore(saved)

# Оверлей, который правки не ломает: подпись сменяется, payload остаётся.
edited = json.loads(backup)
edited["texts"]["intro"] = "<b>Тестовая шапка</b>"
edited["catalog"]["spheres"][0]["label"] = "Digital / тест"
edited["buttons"]["reset"] = "🔄 Заново"
saved = swap(edited)
check("оверлей меняет текст", content.text("intro") == "<b>Тестовая шапка</b>")
check("оверлей меняет подпись кнопки", content.label("reset") == "🔄 Заново")
keyboard = optimizer.keyboard_sphere()
check("оверлей меняет подпись сферы, payload остаётся прежним",
      keyboard[0][0]["text"] == "Digital / тест" and keyboard[0][0]["payload"] == "opt:s:agency")
check("права строка источника показывает оверлей",
      "content.json" in content.source_summary(), content.source_summary())
restore(saved)

saved = swap(backup if isinstance(backup, str) else json.loads(backup))
# файл восстановлен из исходного текста — перечитаем и убедимся
content.CONTENT_FILE = saved
content.reload()
check("content.json в репозитории не изменился чекером",
      open(CONTENT_PATH, encoding="utf-8").read() == backup)
check("после восстановления предупреждений нет", content.warnings() == [],
      str(content.warnings()[:1]))

# ── 8. Сообщения целиком: ни один рендер не больше резки MAX ─────────────────
long_texts = []
for path in [f"texts.{p}" for p in paths_of(content.DEFAULTS["texts"])]:
    out = render(path)
    if len(out) > optimizer.TEXT_CHUNK and "\n\n" not in out:
        long_texts.append(f"{path}: {len(out)}")
check("каждый длинный текст режется по абзацам", not long_texts,
      "; ".join(long_texts[:3]))

# ── 9. Страница админки не должна терять пустые строки сообщений ─────────────
# Значение внутри <textarea>…</textarea> HTML-парсер читает с выбрасыванием
# первого перевода строки: каждая правка съедала ведущую пустую строку у всех
# полей открытой вкладки. Значение обязано приходить свойством value.
PAGE = os.path.join(ROOT, "admin", "index.html")
src = open(PAGE, encoding="utf-8").read()
inline = re.findall(r"<textarea[^>]*>\s*\$\{[^}]*\}", src)
check("админка ставит значение textarea свойством, не через innerHTML",
      not inline, "; ".join(inline[:2]))
check("страница читает content.json и справочник",
      "content.json" in src and "content.schema.json" in src)

# ── 10. Ведущие пустые строки не потеряны ─────────────────────────────────────
# Пустая строка перед текстом — часть вёрстки сообщения: она отделяет ответ бота
# от строк, которые он написал до этого. Старая сборка админки съедала её на
# каждом сохранении, и бот начинал печатать «Сопровождение:» впритык к предыдущей
# строке. Чек ловит такие правки в файле, какой бы путь они ни пришли.
flat = json.load(open(CONTENT_PATH, encoding="utf-8"))


def string_paths(node, prefix=""):
    out = []
    if isinstance(node, str):
        return [prefix]
    if isinstance(node, list):
        for i, value in enumerate(node):
            out += string_paths(value, f"{prefix}.{i}")
    elif isinstance(node, dict):
        for key, value in node.items():
            out += string_paths(value, f"{prefix}.{key}" if prefix else key)
    return out


def at(tree, dotted):
    cur = tree
    for part in dotted.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)] if int(part) < len(cur) else None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def lead_count(text):
    """Сколько пустых строк в начале текста — это вёрстка, а не украшение."""
    return len(text) - len(text.lstrip("\n"))


flattened = []
for group in ("texts", "buttons", "words", "catalog"):
    for rel in string_paths(content.DEFAULTS[group]):
        path = f"{group}.{rel}"
        base, value = at(content.DEFAULTS, path), at(flat, path)
        if not isinstance(base, str) or not isinstance(value, str):
            continue
        if lead_count(base) and lead_count(value) != lead_count(base):
            flattened.append(f"{path}: ведущих пустых строк {lead_count(value)} "
                             f"вместо {lead_count(base)}")
check("ни один текст не потерял ведущие пустые строки", not flattened,
      "; ".join(flattened[:4]))

# Тот же признак проверяем дальше по цепочке: файл → оверлей → подстановка
# переменных → резка на сообщения. На каждом шаге пустая строка в начале может
# исчезнуть, и в чате текст прилипнет к предыдущему сообщению.
eaten = []
for rel in string_paths(content.DEFAULTS["texts"]):
    path = f"texts.{rel}"
    value = at(flat, path)
    if not isinstance(value, str) or not lead_count(value):
        continue
    loaded = content.get(path) or ""
    if lead_count(loaded) != lead_count(value):
        eaten.append(f"{path}: оверлей отдал {lead_count(loaded)} строк вместо "
                     f"{lead_count(value)}")
    rendered = render(path)
    if lead_count(rendered) != lead_count(value):
        eaten.append(f"{path}: после подстановки {lead_count(rendered)} вместо "
                     f"{lead_count(value)}")
check("оверлей и подстановка сохраняют ведущие пустые строки", not eaten,
      "; ".join(eaten[:3]))

mangled = []
for rel in string_paths(content.DEFAULTS["texts"]):
    text = render(f"texts.{rel}")
    if len(text) <= optimizer.TEXT_CHUNK:
        continue
    lead = lead_count(text)
    parts = optimizer.chunk(text)
    budget = optimizer.TEXT_CHUNK - lead
    if any(not p for p in parts):
        mangled.append(f"texts.{rel}: резка отдала пустое сообщение")
    if any(len(p) > optimizer.TEXT_CHUNK for p in parts):
        mangled.append(f"texts.{rel}: часть длиннее лимита MAX")
    if lead and lead_count(parts[0]) != lead:
        mangled.append(f"texts.{rel}: резка съела {lead} ведущих пустых строк")
    if all(len(b) <= budget for b in text[lead:].split("\n\n")) and \
            "\n\n".join(parts) != text:
        mangled.append(f"texts.{rel}: резка переставила абзацы")
check("резка длинных сообщений сохраняет пустые строки и порядок абзацев",
      not mangled, "; ".join(mangled[:3]))

# Отдельно на синтетическом тексте: в боевом файле длинных сообщений с ведущей
# пустой строкой сейчас нет, а сломать резку легче всего именно на них.
long_lead = optimizer.chunk("\n\n" + "абзац " * 300)
check("резка текста длиннее лимита оставляет ведущую пустую строку",
      long_lead and lead_count(long_lead[0]) == 2
      and all(p and len(p) <= optimizer.TEXT_CHUNK for p in long_lead),
      f"частей {len(long_lead)}, начало {long_lead[0][:8]!r}")

print(f"\n{sum(results)}/{len(results)} проверок пройдено")
sys.exit(0 if all(results) else 1)
