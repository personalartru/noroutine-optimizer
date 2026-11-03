"""PDF-отчёт «Оптимизатора»: полная версия чек-листа с расчётом.

В чате остаётся короткий список на семь строк, а здесь — то, что не стычно
переслать коллегам: рутина по операциям, экономия в часах и рублях, бюджет,
семь шагов внедрения с пояснениями и дисклеймер.

Только fpdf2 и системный TTF с кириллицей — никаких внешних сервисов и LaTeX.
Шрифт ищем по списку (на GitHub-раннере это DejaVu), пути можно переопределить
переменными MAX_PDF_FONT и MAX_PDF_FONT_BOLD. Если библиотеки или кириллического
шрифта нет, `available()` вернёт False, и транспорт честно отдаст чек-лист
текстом вместо битого файла.

Эмодзи в PDF не рисуем: в DejaVu/Arial нет таких глифов, они вырежутся
регулярным выражением ниже — эмодзи живут в чате.

    python3 tools/make_report_pdf.py     # собрать демо-отчёт в /tmp
"""
import functools
import glob
import json
import os
import re

try:
    from fpdf import FPDF
except ImportError:                          # pragma: no cover - зависит от окружения
    FPDF = None

# Диапазоны эмодзи и транспортных символов: в PDF их рисовать нечем.
EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"     # пиктограммы, транспорт, доп. блоки
        "\U00002190-\U000021FF"     # стрелки
    "\U00002300-\U000023FF"     # технические символы
    "\U00002600-\U000027BF"     # ☀-➿ : ✅, ✨, ➡
    "\U00002B00-\U00002BFF"     # Up/Down-квадраты
    "\U0000200D\U0000200B\U0000FE0F"  # нулевые пробелы и варь-селектор
    "]+"
)

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
    """В одну строку: без эмодзи, переносов и двойных пробелов."""
    return re.sub(r"\s+", " ", EMOJI_RE.sub("", str(text))).strip()


class Report(FPDF):
    """Пару мелочей задаём классом, чтобы не тянуть global-состояние."""

    def footer(self):
        self.set_y(-12)
        self.set_font("body", size=7.5)
        self.set_text_color(150, 150, 150)
        self.cell(0, 4, f"No! Routine · оптимизатор рутины · стр. {self.page_no()}")


# Не во всех системных TTF есть эти глифы (Arial на macOS — без ₽).
RUBLO_FALLBACK = {"\u20bd": "руб.", "\u2192": "->"}

GREY = (110, 110, 110)
INK = (25, 25, 25)
ACCENT = (20, 90, 160)


def _heading(pdf, text):
    pdf.set_font("body", "B", size=11.5)
    pdf.set_text_color(*ACCENT)
    pdf.cell(0, 7, clean(text), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*INK)
    pdf.set_font("body", size=10)


def _para(pdf, text, size=10, grey=False, height=5.0):
    pdf.set_font("body", size=size)
    pdf.set_text_color(*(GREY if grey else INK))
    pdf.multi_cell(0, height, clean(text), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*INK)


def _row(pdf, label, value):
    """Две колонки: пояснение слева, цифра справа жирным.

    multi_cell сам переносит курсор на новую строку, поэтому обе колонки стартуют
    от одной y, а вниз уходим по более высокой.
    """
    width = pdf.w - pdf.l_margin - pdf.r_margin
    label_w = round(width * 0.52, 2)
    y0 = pdf.get_y()
    pdf.set_font("body", size=10)
    pdf.set_xy(pdf.l_margin, y0)
    pdf.multi_cell(label_w, 5.0, clean(label), new_x="LMARGIN", new_y="NEXT")
    y_label = pdf.get_y()
    pdf.set_font("body", "B", size=10)
    pdf.set_xy(pdf.l_margin + label_w, y0)
    pdf.multi_cell(width - label_w, 5.0, clean(value), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("body", size=10)
    pdf.set_y(max(y_label, pdf.get_y()) + 0.8)


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


def build(data):
    """bytes с отчётом. `data` — структура из optimizer.report_data()."""
    if not available():
        raise RuntimeError("PDF недоступен: нет fpdf2 или кириллического шрифта")
    data = _adapt_fonts(data)
    pdf = Report(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_margins(16, 16, 16)
    pdf.add_font("body", "", font_regular())
    bold = font_bold()
    if bold:
        pdf.add_font("body", "B", bold)
    pdf.add_page()

    # шапка
    pdf.set_font("body", size=9)
    pdf.set_text_color(*GREY)
    pdf.cell(0, 5, f"{clean(data.get('date', ''))} · расчёт «Оптимизатора»",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*INK)
    pdf.set_font("body", "B", size=17)
    pdf.multi_cell(0, 8, clean(data.get("title") or "Отчёт оптимизатора"),
                 new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("body", size=10)
    subtitle = " · ".join(part for part in (clean(data.get("who", "")),
                                            f"{clean(data.get('people', ''))} чел",
                                            clean(data.get("rate", "")),
                                            clean(data.get("headline", ""))) if part)
    pdf.set_text_color(*GREY)
    pdf.multi_cell(0, 5.5, subtitle, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*INK)
    pdf.ln(3)

    _heading(pdf, "Рутина по операциям")
    ops = data.get("ops") or []
    if ops:
        for op in ops:
            _row(pdf, op.get("title", ""),
                 " · ".join(p for p in (clean(op.get("value", "")),
                                        clean(op.get("note", ""))) if p))
    else:
        _para(pdf, "Операции не выбраны.", grey=True)

    _heading(pdf, "Экономия")
    for label, value in (data.get("figures") or []):
        _row(pdf, label, value)

    _heading(pdf, "Инструменты и бюджет")
    for tool in (data.get("tools") or []):
        _row(pdf, tool.get("title", ""), clean(tool.get("value", "")))
        if tool.get("note"):
            _para(pdf, tool["note"], size=9, grey=True, height=4.4)
    for label, value in (data.get("budget") or []):
        _row(pdf, label, value)

    _heading(pdf, "Чек-лист внедрения")
    for step in (data.get("steps") or []):
        pdf.set_font("body", "B", size=10.5)
        pdf.multi_cell(0, 5.2,
                       f"{step.get('num', '')}. {clean(step.get('title', ''))} "
                       f"— {clean(step.get('when', ''))}",
                       new_x="LMARGIN", new_y="NEXT")
        _para(pdf, step.get("body", ""), size=9.5, grey=True, height=4.6)
        pdf.ln(1.4)

    pdf.ln(1)
    _heading(pdf, "Как читать цифры")
    _para(pdf, data.get("disclaimer", ""), size=9, grey=True, height=4.6)
    channel = clean(data.get("channel", ""))
    if channel:
        pdf.ln(1.5)
        _para(pdf, f"Разборы и готовые связки: {channel}", size=9)

    return bytes(pdf.output())
