"""PDF-отчёт «Оптимизатора»: полная форма того же экрана, что и чат.

Модель отчёта живёт не здесь, а в optimizer.report_blocks(): пять блоков
(«01 · Исходные данные» … «05 · Контроль окупаемости»), где каждый — либо строки
«параметр — значение», либо настоящая таблица с колонками. Отсюда два следствия.
Во-первых, цифра из чата не может разойтись с цифрой из файла: рендеры читают
один и тот же список. Во-вторых, этот модуль отвечает только за вёрстку —
подписи, заголовки и дисклеймер приходят вместе с данными из content.json,
и правка в админке видна и в чате, и в PDF.

Здесь остаётся то, что нельзя отдать тексту: палитра логотипа и сайта, тёмная
плашка с логотипом, доли колонок, переносы строк и правило «блок не разъезжается
между страницами». Отчёт рассчитан на две страницы A4: таблицы печатаются
целиком, а если блок не влезает в остаток страницы, он уходит на следующую
целиком, с повтором шапки таблицы.

Только fpdf2 и системный TTF с кириллицей — никаких внешних сервисов и LaTeX.
Шрифт ищем по списку (на GitHub-раннере это DejaVu), пути можно переопределить
переменными MAX_PDF_FONT и MAX_PDF_FONT_BOLD. Если библиотеки или кириллического
шрифта нет, `available()` вернёт False, и транспорт честно отдаст тот же отчёт
текстом (optimizer.report_text) вместо битого файла.

Эмодзи в PDF не рисуем: в DejaVu/Arial нет таких глифов, они вырежутся
регулярным выражением ниже — эмодзи живут в чате.

    python3 tools/make_report_pdf.py     # собрать демо-отчёт в /tmp
"""
import functools
import glob
import html
import json
import os
import re
from datetime import datetime

try:
    from fpdf import FPDF
except ImportError:                          # pragma: no cover - зависит от окружения
    FPDF = None

try:  # dry_run отдаёт высоту блока, не рисуя его: по ней решаем перенос страницы
    from fpdf.enums import MethodReturnValue
    HEIGHT = MethodReturnValue.HEIGHT
except ImportError:                          # pragma: no cover - старые fpdf2
    HEIGHT = "HEIGHT"

ROOT = os.path.dirname(os.path.abspath(__file__))
LOGO_FILE = os.path.join(ROOT, "assets", "optimizator_logo.png")

# Диапазоны эмодзи и транспортных символов: в PDF их рисовать нечем.
EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"     # пиктограммы, транспорт, доп. блоки
        "\U00002190-\U000021FF"     # стрелки
    "\U00002300-\U000023FF"     # технические символы
    "\U00002600-\U000027BF"     # ☀-➿ : ✅, ✨, 
    "\U00002B00-\U00002BFF"     # Up/Down-квадраты
    "\U0000200D\U0000200B\U0000FE0F"  # нулевые пробелы и варь-селектор
    "]+"
)
TAG_RE = re.compile(r"</?[a-z][a-z0-9]*\s*/?>", re.IGNORECASE)

FONT_CANDIDATES = (
    os.environ.get("MAX_PDF_FONT", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
)
BOLD_CANDIDATES = (
    os.environ.get("MAX_PDF_FONT_BOLD", ""),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
)
FONT_GLOBS = ("/usr/share/fonts/**/DejaVuSans.ttf",
              "/usr/share/fonts/**/LiberationSans-Regular.ttf")


def _first_existing(paths):
    for path in paths:
        if path and os.path.isfile(path):
            return path
    for pattern in FONT_GLOBS:
        for hit in sorted(glob.glob(pattern, recursive=True)):
            if os.path.isfile(hit):
                return hit
    return ""


def font_regular():
    return _first_existing(FONT_CANDIDATES)


def font_bold():
    return _first_existing(BOLD_CANDIDATES) or font_regular()


def logo_file():
    """Логотип для шапки: файл лежит в репозитории, в сборке он обязателен."""
    return LOGO_FILE if os.path.isfile(LOGO_FILE) else ""


# Пути ищутся при каждой сборке отчёта (и в цикле уплотнения ниже), а набор
# шрифтов и файл логотипа за жизнь цикла не меняются.
font_regular = functools.lru_cache(maxsize=1)(font_regular)
font_bold = functools.lru_cache(maxsize=1)(font_bold)
logo_file = functools.lru_cache(maxsize=1)(logo_file)


def available():
    """PDF соберём только если есть fpdf2 и шрифт с кириллицей."""
    return bool(FPDF and font_regular())


@functools.lru_cache(maxsize=8)
def _cmap(path):
    """Символы, которые шрифт реально умеет рисовать (через fontTools из fpdf2)."""
    if not path:
        return set()
    try:
        from fontTools.ttLib import TTFont
        with TTFont(path, lazy=True) as font:
            return set(font.getBestCmap() or {})
    except Exception:               # сломанный/редкий шрифт — не роняем отчёт
        return set()


def clean(text):
    """Текст для печати: HTML-разметка и сущности — не про PDF.

    Значения ячеек приходят уже экранированными (в чате они уходят с
    format=html), поэтому сначала разворачиваем &amp;/&quot; обратно, снимаем
    случайные теги — админка правит формулировки, а не разметку, — и выбрасываем
    то, чему нет глифов.
    """
    raw = html.unescape(str(text))
    raw = TAG_RE.sub("", raw)
    return re.sub(r"\s+", " ", EMOJI_RE.sub("", raw)).strip()


# Палитра логотипа и калькулятора: чернила, бумага, зелёный акцент и маджента.
INK = (14, 19, 31)          # #0E131F — фон шапки, заголовки таблиц
PAPER = (247, 251, 251)     # #F7FBFB — подложка блоков и полосатых строк
GREEN = (8, 181, 131)       # #08B583 — плашки, номера блоков
ACCENT_TEXT = (4, 120, 87)  # #047857 — зелёный, который читаем на белом
MAGENTA = (253, 63, 216)    # #FD3FD8 — одна акцентная строка (окупаемость)
MUTED = (75, 85, 99)        # #4B5563 — подписи параметров
OUTLINE = (213, 221, 212)   # #D5DDD4 — сетка таблиц

MARGIN = 14.0
BAND_H = 32.0               # тёмная плашка с логотипом
LINE = 4.3                  # межстрочная высота текста
TLINE = 4.1                 # межстрочная высота в таблице


class Report(FPDF):
    """Шапка-плашка на первой странице и продолжение на остальных."""

    # add_page() вызывает header() сразу, поэтому значения по умолчанию:
    # отчёт, собранный без шапки, не должен падать на пустом атрибуте.
    report_title = ""
    report_subtitle = ""
    report_meta = ""

    def header(self):
        self.set_fill_color(*INK)
        self.set_draw_color(*INK)
        if self.page_no() == 1:
            self.rect(0, 0, self.w, BAND_H, style="F")
            x = MARGIN
            logo = logo_file()
            if logo:
                size = 22.0
                self.image(logo, x=MARGIN, y=(BAND_H - size) / 2, w=size, h=size)
                x = MARGIN + size + 6
            width = self.w - MARGIN - x
            y = 7.5
            self.set_font("body", "B", size=15)
            self.set_text_color(*PAPER)
            self.set_xy(x, y)
            self.multi_cell(width, 6.2, clean(self.report_title),
                            new_x="LMARGIN", new_y="NEXT")
            y = self.get_y() + 0.6
            self.set_font("body", size=8.4)
            self.set_text_color(157, 190, 186)   # приглушённый зелёный с логотипа
            self.set_xy(x, y)
            self.multi_cell(width, 4.2, clean(self.report_subtitle),
                            new_x="LMARGIN", new_y="NEXT")
            self.set_xy(x, self.get_y() + 0.4)
            self.multi_cell(width, 4.2, clean(self.report_meta),
                            new_x="LMARGIN", new_y="NEXT")
            self.set_y(BAND_H + 8)
        else:
            self.set_y(MARGIN)
            self.set_font("body", size=8)
            self.set_text_color(*MUTED)
            self.cell(0, 4, clean(self.report_meta), new_x="LMARGIN", new_y="NEXT")
            self.set_draw_color(*OUTLINE)
            self.line(MARGIN, self.get_y() + 1.2, self.w - MARGIN, self.get_y() + 1.2)
            self.set_y(self.get_y() + 5)
        self.set_text_color(*INK)

    def footer(self):
        self.set_y(-13)
        self.set_draw_color(*OUTLINE)
        self.line(MARGIN, self.get_y(), self.w - MARGIN, self.get_y())
        self.set_font("body", size=7.5)
        self.set_text_color(*MUTED)
        self.cell(0, 4, "No! Routine · «Оптимизатор» · стр. "
                        f"{self.page_no()}", new_x="LMARGIN", new_y="NEXT")
        self.set_text_color(*INK)


# Не во всех системных TTF есть эти глифы (Arial на macOS — без ₽).
RUBLO_FALLBACK = {"\u20bd": "руб.", "\u2192": "->"}


def _adapt_fonts(data):
    """Если выбранного шрифта хватает на всё — не трогаем текст.

    Знак рубля есть не во всех системных TTF (на macOS Arial его нет, на
    Ubuntu DejaVu — да). Пропущенный глиф fpdf молча съедает, поэтому меняем
    «₽» на «руб.» во всём отчёте одним проходом.
    """
    cmap = _cmap(font_regular())
    if not cmap:
        return data
    text = json.dumps(data, ensure_ascii=False)
    changed = False
    for char, replacement in RUBLO_FALLBACK.items():
        if ord(char) not in cmap and char in text:
            text = text.replace(char, replacement)
            changed = True
    return json.loads(text) if changed else data


def _creation_date(data):
    """Дата в заголовке PDF — только день, без часов и секунд.

    fpdf2 пишет в файл /CreationDate с точностью до секунды, и два вызова подряд
    дают разные байты. Транспорт кэширует загрузку по хэшу файла, поэтому второй
    клик по «Полному отчёту» через минуту снова грузил бы тот же PDF в MAX. С
    фиксированной датой на полночь ночи отчёт побайтово одинаковый, и кэш работает.
    """
    raw = str(data.get("date") or "")
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    today = datetime.now()
    return today.replace(hour=0, minute=0, second=0, microsecond=0)


# ── Примитивы вёрстки ────────────────────────────────────────────────────────
def _height(pdf, width, text, line):
    """Сколько миллиметров займёт текст в колонке шириной `width`.

    Две тонкости. multi_cell считает переносы от текущего курсора, а после
    предыдущей ячейки он у правого края — сух просил бы «недостаточно места»;
    замер всегда ведём от левого поля и возвращаем курсор на место. И у ячейки
    есть собственные внутренние поля (c_margin), поэтому ширину текста берём с
    вычетом, но не меньше разумного минимума: колонка «№» иначе не измеряется
    вовсе, а не становится уже.
    """
    x0, y0 = pdf.get_x(), pdf.get_y()
    pdf.set_xy(MARGIN, y0)
    try:
        return pdf.multi_cell(max(width - 2.0, 6.0), line, text,
                              dry_run=True, output=HEIGHT)
    finally:
        pdf.set_xy(x0, y0)


def _room(pdf, need):
    """Хватает ли места на странице; если нет — новая страница (шапка сама перерисуется).

    Перенос включаем сами, с запасом на заголовок блока: иначе полутора
    строчками ниже fpdf2 разорвал бы таблицу посередине строки.
    """
    if pdf.get_y() + need > pdf.page_break_trigger:
        pdf.add_page()
        return True
    return False


def _block_head(pdf, template, num, title):
    """Номер и название блока: «01» зелёным, остальное чернилами, на подложке."""
    line = clean(str(template).replace("{num}", str(num)).replace("{title}", title))
    need = 9.0 + LINE * 2
    _room(pdf, need)
    y0 = pdf.get_y()
    pdf.set_fill_color(*PAPER)
    pdf.rect(MARGIN, y0, pdf.epw, 8.0, style="F")
    pdf.set_fill_color(*GREEN)
    pdf.rect(MARGIN, y0, 1.5, 8.0, style="F")
    pdf.set_font("body", "B", size=11)
    pdf.set_text_color(*ACCENT_TEXT)
    head = clean(str(num))
    pdf.set_xy(MARGIN + 4, y0 + 1.6)
    pdf.cell(pdf.get_string_width(head) + 1, 5.2, head, new_x="RIGHT", new_y="TOP")
    pdf.set_text_color(*INK)
    pdf.multi_cell(pdf.epw - 6 - pdf.get_string_width(head) - 1, 5.2,
                   line[len(head):].lstrip(), new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(y0 + 8.0 + 3.2)
    pdf.set_font("body", size=9)


def _rows(pdf, rows, k=1.0):
    """Блок «параметр — значение»: подпись слева приглушённая, цифра справа жирная."""
    line = LINE * k
    width = pdf.epw
    label_w = round(width * 0.40, 2)
    value_w = round(width - label_w, 2)
    for row in rows:
        label = clean(row.get("label", ""))
        value = clean(row.get("value", ""))
        pdf.set_font("body", size=9)
        height = max(_height(pdf, label_w - 2, label, line),
                     _height(pdf, value_w - 2, value, line))
        _room(pdf, height + 3)
        y0 = pdf.get_y()
        pdf.set_xy(MARGIN, y0)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(label_w - 2, line, label, new_x="LMARGIN", new_y="NEXT")
        pdf.set_xy(MARGIN + label_w, y0)
        pdf.set_font("body", "B", size=9.4 if row.get("strong") else 9)
        pdf.set_text_color(*(MAGENTA if row.get("strong") else INK))
        pdf.multi_cell(value_w - 2, line, value, align="R",
                       new_x="LMARGIN", new_y="NEXT")
        y1 = max(pdf.get_y(), y0 + height)
        pdf.set_y(y1)
        pdf.set_draw_color(*OUTLINE)
        pdf.line(MARGIN, y1 + 0.4, pdf.w - MARGIN, y1 + 0.4)
        pdf.set_y(y1 + 2.2 * k)
        pdf.set_text_color(*INK)
        pdf.set_font("body", size=9)


def _note(pdf, text, k=1.0):
    """Сноска под блоком — серым и мельче, но не отдельным разделом."""
    line = 3.9 * k
    text = clean(text)
    pdf.set_font("body", size=8)
    height = _height(pdf, pdf.epw - 4, text, line)
    _room(pdf, height + 3)
    pdf.set_text_color(*MUTED)
    pdf.set_fill_color(*PAPER)
    y0 = pdf.get_y()
    pdf.rect(MARGIN, y0, 1.0, height + 2.4, style="F")
    pdf.set_xy(MARGIN + 3.5, y0 + 1.2)
    pdf.multi_cell(pdf.epw - 6, line, text, new_x="LMARGIN", new_y="NEXT")
    pdf.set_y(max(pdf.get_y(), y0 + height + 2.4) + 2.5)
    pdf.set_text_color(*INK)
    pdf.set_font("body", size=9)


def _table(pdf, columns, rows, total=None, empty="", k=1.0):
    """Таблица с шапкой на чернилах; при переносе страницы шапка печатается снова.

    Рамку рисуем сами, а не border'ом каждой ячейки: multi_cell обводит только
    собственный текст, и в одной строке ячейки оказались бы разной высоты —
    ровно та «рваная» сетка, которая портила таблицу 04. Сначала фон строки
    целиком, потом текст по колонкам, потом сетка по реальной высоте строки.
    """
    line = TLINE * k
    parts = [float(column.get("width") or 0) or 100.0 / max(1, len(columns))
             for column in columns]
    scale = sum(parts) or 100.0
    widths = [round(pdf.epw * share / scale, 2) for share in parts]
    aligns = [(column.get("align") or "left").upper() for column in columns]
    labels = [clean(column.get("label", "")) for column in columns]
    edges = [MARGIN]
    for width in widths:
        edges.append(round(edges[-1] + width, 2))

    def grid(y0, height, color=OUTLINE):
        pdf.set_draw_color(*color)
        for x in edges:
            pdf.line(x, y0, x, y0 + height)
        pdf.line(MARGIN, y0, MARGIN + pdf.epw, y0)
        pdf.line(MARGIN, y0 + height, MARGIN + pdf.epw, y0 + height)

    def cells_row(values, height, *, fill, ink, bold, rule=OUTLINE, inset=1.2):
        y0 = pdf.get_y()
        pdf.set_fill_color(*fill)
        pdf.rect(MARGIN, y0, pdf.epw, height, style="F")
        pdf.set_font("body", "B" if bold else "", size=8)
        pdf.set_text_color(*ink)
        for i, value in enumerate(values):
            pdf.set_xy(edges[i] + 1, y0 + inset)
            pdf.multi_cell(max(widths[i] - 2, 4), line, value, align=aligns[i],
                           new_x="LMARGIN", new_y="NEXT")
        grid(y0, height, rule)
        pdf.set_y(y0 + height)
        pdf.set_text_color(*INK)

    def measure(values):
        pdf.set_font("body", size=8)
        return max(6.2, max(_height(pdf, widths[i], values[i], line)
                            for i in range(len(widths))) + 2 * 1.2)

    def head():
        cells_row(labels, measure(labels), fill=INK, ink=PAPER, bold=True,
                  rule=INK, inset=1.4)

    if not rows:
        _room(pdf, 18)
        head()
        y0 = pdf.get_y()
        height = 8.0
        pdf.set_font("body", size=8.6)
        pdf.set_text_color(*MUTED)
        pdf.set_xy(MARGIN + 2, y0 + 2)
        pdf.multi_cell(pdf.epw - 4, line, clean(empty), new_x="LMARGIN", new_y="NEXT")
        grid(y0, height)
        pdf.set_y(y0 + height + 2)
        pdf.set_text_color(*INK)
        return

    first = [clean(cell) if cell is not None else "" for cell in rows[0]]
    # Шапка рисуется один раз перед первой строкой. Перенос проверяем сразу для
    # шапки и первой строки: иначе на предыдущей странице осталась бы одна шапка,
    # а «повтор шапки» ниже нужен только для строк, которые уехали на продолжение.
    _room(pdf, measure(labels) + measure(first) + 4)
    head()
    for index, row in enumerate(rows):
        values = first if index == 0 else [clean(cell) if cell is not None else ""
                                           for cell in row]
        height = measure(values)
        if index and _room(pdf, height + 4):
            head()
        # «зебра» читается в длинной таблице 04; шапка остаётся чернильной
        cells_row(values, height,
                  fill=PAPER if index % 2 == 0 else (255, 255, 255),
                  ink=INK, bold=False)

    if total and any(cell for cell in total if cell):
        values = [clean(cell) if cell is not None else "" for cell in total]
        height = measure(values)
        if _room(pdf, height + 4):
            head()
        cells_row(values, height, fill=PAPER, ink=INK, bold=True, rule=INK)


def _plain(pdf, text, size=8.6, grey=False, line=LINE, k=1.0):
    pdf.set_font("body", size=size)
    pdf.set_text_color(*(MUTED if grey else INK))
    pdf.multi_cell(pdf.epw, line * k, clean(text), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*INK)


def _render(data, k):
    """Один проход вёрстки с коэффициентом межстрочных высот `k`."""
    pdf = Report(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=False, margin=16)
    pdf.set_margins(MARGIN, MARGIN, MARGIN)
    pdf.set_creation_date(_creation_date(data))
    pdf.add_font("body", "", font_regular())
    bold = font_bold()
    if bold:
        pdf.add_font("body", "B", bold)

    header = data.get("header") or {}
    # шапку рисует Report.header(), ей нужны только три строки из контента
    pdf.report_title = header.get("title", "")
    pdf.report_subtitle = header.get("subtitle", "")
    pdf.report_meta = header.get("meta", "")
    pdf.add_page()

    template = data.get("block") or "{num} · {title}"
    for spec in (data.get("blocks") or []):
        _block_head(pdf, template, spec.get("num", ""), clean(spec.get("title", "")))
        if spec.get("kind") == "table":
            _table(pdf, spec.get("columns") or [], spec.get("rows") or [],
                   total=spec.get("total"), empty=spec.get("empty", ""), k=k)
        else:
            _rows(pdf, spec.get("rows") or [], k=k)
        if spec.get("note"):
            _note(pdf, spec["note"], k=k)
        pdf.ln(2.5 * k)

    howto = data.get("howto") or {}
    if howto.get("title"):
        _room(pdf, 24)
        _block_head(pdf, "{title}", "", howto["title"])
        _plain(pdf, howto.get("body", ""), size=8.6, grey=True, line=LINE, k=k)
        pdf.ln(2 * k)

    channel = data.get("channel") or {}
    if channel.get("text"):
        url = channel.get("url") or ""
        text = clean(channel["text"])
        width = pdf.epw - 5
        pdf.set_font("body", "B", size=8.8)
        # замер — той же шириной, чем рисуем, и без выравнивания по ширине:
        # иначе плашка окажется короче текста, а последняя строка вылезет за неё
        height = _height(pdf, width, text, LINE * k)
        _room(pdf, height + 8)
        y0 = pdf.get_y()
        pdf.set_fill_color(*INK)
        pdf.rect(MARGIN, y0, pdf.epw, height + 5, style="F")
        pdf.set_xy(MARGIN + 2.5, y0 + 2.5)
        pdf.set_text_color(*PAPER)
        pdf.multi_cell(width, LINE * k, text, align="L", link=url,
                       new_x="LMARGIN", new_y="NEXT")
        pdf.set_y(max(pdf.get_y(), y0 + height + 5))
        pdf.set_text_color(*INK)
    return pdf


def build(data):
    """bytes с отчётом. `data` — структура из optimizer.report_data().

    Отчёт задуман на две страницы A4: таблицы печатаются целиком, поэтому
    уплотнение включаем только если вторая страница осталась почти пустой, а
    третья вообще не нужна — тогда уменьшаем межстрочные высоты (шрифт не
    трогаем) и пересобираем.
    """
    if not available():
        raise RuntimeError("PDF недоступен: нет fpdf2 или кириллического шрифта")
    data = _adapt_fonts(data)
    for k in (1.0, 0.9, 0.82):
        pdf = _render(data, k)
        last = pdf.get_y()
        # на последней странице осталось меньше трети высоты — стоит уплотниться
        sparse = (pdf.pages_count > 2
                  or (pdf.pages_count == 2 and last < 16 + (297 - 32) * 0.35))
        if not sparse:
            break
    return bytes(pdf.output())
