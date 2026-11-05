"""Сборка демо-PDF из сценариев симулятора — проверка отчёта без MAX.

Гоняет тот же клик-путь, что и simulate_optimizer.py, нажимает «Полный отчёт
(PDF)» и пишет файл на диск. Полезно, когда надо глазами оценить вёрстку:

    python3 tools/make_report_pdf.py                 # все сценарии в /tmp
    python3 tools/make_report_pdf.py --scenario team --out ./pdf
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import optimizer as opt          # noqa: E402
import report_pdf                # noqa: E402
from simulate_optimizer import SCENARIOS  # noqa: E402  (переиспользуем скрипты)


def walk(name, script):
    """Клики до финала, затем «PDF» — возвращаем структуру отчёта.

    Сценарий «ошибок» до расчёта не доходит — он и не должен: возвращаем None,
    инструмент такие пропустит.
    """
    session = None
    payload = None
    for step in script["clicks"] + ["opt:p:report"]:
        session, payload = opt.dispatch(step, session)
    for message in payload or []:
        if isinstance(message, dict) and message.get("pdf"):
            return message["pdf"], message.get("pdf_fallback", "")
    return None, ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="all")
    parser.add_argument("--out", default="/tmp/optimizer-reports")
    args = parser.parse_args()

    if not report_pdf.available():
        print("PDF недоступен: нет fpdf2 или кириллического шрифта — "
              "в проде бот отдаст чек-лист текстом")
        return 2
    os.makedirs(args.out, exist_ok=True)
    names = (list(SCENARIOS) if args.scenario == "all" else [args.scenario])
    for name in names:
        data, _ = walk(name, SCENARIOS[name])
        if not data:
            print(f"сценарий {name}: до расчёта не доходит — PDF не просится")
            continue
        blob = report_pdf.build(data)
        path = os.path.join(args.out, f"report-{name}.pdf")
        with open(path, "wb") as handle:
            handle.write(blob)
        pages = blob.count(b"/Type /Page") - blob.count(b"/Type /Pages")
        title = (data.get("header") or {}).get("title", "")
        blocks = len(data.get("blocks") or [])
        print(f"{path} · {len(blob)} байт · ~{max(pages, 1)} стр. · "
              f"{title} · блоков {blocks}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
