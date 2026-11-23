"""Export an answer's result table to Excel or PDF.

Exports are built from the result the user was already shown (same rows, same row
security), never by re-running a query. The gateway checks ownership and writes the audit
row; this module only formats.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

FORMATS = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
}
FOOTER = "Internal use only. Figures cover only the branches the requester is entitled to."
_ARABIC = re.compile(r"[؀-ۿ]")
_FONTS = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",  # macOS
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Debian (fonts-dejavu-core)
)


@dataclass
class ExportDoc:
    question: str
    answer: str
    definition: str
    columns: list[str]
    rows: list[list[Any]]
    unit: str = ""
    label: str = ""
    totals_columns: list[str] = field(default_factory=list)
    totals: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    requested_by: str = ""
    created_at: datetime = field(default_factory=datetime.now)

    def header(self, col: str) -> str:
        if col == "value":
            return self.label or "Value"
        return col.replace("_", " ").capitalize()


def filename(fmt: str, when: datetime | None = None) -> str:
    return f"data-export-{(when or datetime.now()):%Y%m%d-%H%M}.{fmt}"


def _num(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v) if re.fullmatch(r"-?\d+(\.\d+)?", v) else v
        except ValueError:
            return v
    return v


def _fmt(v: Any, unit: str) -> str:
    v = _num(v)
    if v is None:
        return ""
    if isinstance(v, bool) or not isinstance(v, int | float):
        return str(v)
    if unit == "ratio":
        return f"{v * 100:.2f}%"
    if isinstance(v, int) or float(v).is_integer() and unit != "KWD":
        return f"{int(v):,}"
    return f"{v:,.3f}" if unit == "KWD" else f"{v:,.2f}"


# ---- Excel ---------------------------------------------------------------------------
def to_xlsx(doc: ExportDoc) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    number_format = {"KWD": "#,##0.000", "ratio": "0.00%"}.get(doc.unit, "#,##0.##")
    head_font, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="0F766E")
    wb = Workbook()
    ws = wb.active
    assert ws is not None  # noqa: S101 - a new workbook always has a sheet
    ws.title = "Data"

    def table(start: int, cols: list[str], rows: list[list[Any]]) -> int:
        for j, c in enumerate(cols, 1):
            cell = ws.cell(row=start, column=j, value=doc.header(c))
            cell.font, cell.fill = head_font, head_fill
        for i, r in enumerate(rows, start + 1):
            for j, v in enumerate(r, 1):
                cell = ws.cell(row=i, column=j, value=_num(v))
                if j == len(r) and isinstance(cell.value, int | float):
                    cell.number_format = number_format
        return start + len(rows) + 1

    nxt = table(1, doc.columns, doc.rows)
    if doc.totals:
        ws.cell(row=nxt + 1, column=1, value="Total over all periods").font = Font(bold=True)
        table(nxt + 2, doc.totals_columns, doc.totals)
    for j in range(1, max(len(doc.columns), len(doc.totals_columns)) + 1):
        ws.column_dimensions[get_column_letter(j)].width = 22
    ws.freeze_panes = "A2"

    about = wb.create_sheet("About")
    rtl = bool(_ARABIC.search(doc.question + doc.answer))
    facts = [
        ("Question", doc.question),
        ("Answer", doc.answer),
        ("Definition", doc.definition),
        ("Rows", f"{doc.row_count:,}" + (" (truncated at the row limit)" if doc.truncated else "")),
        ("Requested by", doc.requested_by),
        ("Created", f"{doc.created_at:%Y-%m-%d %H:%M}"),
        ("Note", FOOTER),
    ]
    for i, (k, v) in enumerate(facts, 1):
        about.cell(row=i, column=1, value=k).font = Font(bold=True)
        c = about.cell(row=i, column=2, value=v)
        c.alignment = Alignment(wrap_text=True, vertical="top", horizontal="right" if rtl else None)
    about.column_dimensions["A"].width = 16
    about.column_dimensions["B"].width = 100
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---- PDF -----------------------------------------------------------------------------
def _font() -> str:
    """A TTF with Arabic glyphs if one is available (PDF_FONT overrides), else Helvetica."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for path in (os.getenv("PDF_FONT"), *_FONTS):
        if path and Path(path).is_file():
            if "ExportFont" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("ExportFont", path))
            return "ExportFont"
    return "Helvetica"


def _shape(text: str) -> str:
    """Arabic needs letter joining and right-to-left ordering before reportlab draws it."""
    if not _ARABIC.search(text):
        return text
    import arabic_reshaper
    from bidi.algorithm import get_display

    return str(get_display(arabic_reshaper.reshape(text)))


def to_pdf(doc: ExportDoc) -> bytes:
    from xml.sax.saxutils import escape

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    font = _font()
    wide = max(len(doc.columns), len(doc.totals_columns)) > 4
    page = landscape(A4) if wide else A4

    def style(size: float, bold: bool = False, rtl: bool = False) -> ParagraphStyle:
        return ParagraphStyle(
            "s",
            fontName=font if font != "Helvetica" or not bold else "Helvetica-Bold",
            fontSize=size,
            leading=size * 1.35,
            alignment=TA_RIGHT if rtl else TA_LEFT,
            textColor=colors.HexColor("#0f172a"),
        )

    def para(text: str, size: float = 10, bold: bool = False) -> Paragraph:
        rtl = bool(_ARABIC.search(text))
        return Paragraph(escape(_shape(text)), style(size, bold, rtl))

    def grid(cols: list[str], rows: list[list[Any]]) -> Table:
        data = [[_shape(doc.header(c)) for c in cols]]
        data += [
            [_shape(_fmt(v, doc.unit) if j == len(r) - 1 else str(v)) for j, v in enumerate(r)]
            for r in rows
        ]
        t = Table(data, repeatRows=1, hAlign="LEFT")
        t.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), font),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f766e")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.white, colors.HexColor("#f1f5f9")],
                    ),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        return t

    def footer(canvas: Any, d: Any) -> None:
        canvas.saveState()
        canvas.setFont(font, 7.5)
        canvas.setFillColor(colors.HexColor("#64748b"))
        canvas.drawString(d.leftMargin, 11.5 * mm, FOOTER)
        canvas.drawString(
            d.leftMargin,
            8 * mm,
            f"Requested by {doc.requested_by} on {doc.created_at:%Y-%m-%d %H:%M}.",
        )
        canvas.drawRightString(page[0] - d.rightMargin, 8 * mm, f"Page {d.page}")
        canvas.restoreState()

    story: list[Any] = [para(doc.question, 14, bold=True), Spacer(1, 4 * mm)]
    if doc.answer:
        story += [para(doc.answer, 10.5), Spacer(1, 3 * mm)]
    if doc.totals:
        story += [para("Total over all periods", 11, bold=True), Spacer(1, 1.5 * mm)]
        story += [grid(doc.totals_columns, doc.totals), Spacer(1, 5 * mm)]
    story += [grid(doc.columns, doc.rows), Spacer(1, 4 * mm)]
    note = f"{doc.row_count:,} row(s)" + (", truncated at the row limit" if doc.truncated else "")
    story += [para(f"{doc.definition} {note}.", 8.5)]

    buf = io.BytesIO()
    SimpleDocTemplate(
        buf,
        pagesize=page,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=18 * mm,
        title="Data export",
        author="Bank Data Assistant",
    ).build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def render(doc: ExportDoc, fmt: str) -> bytes:
    if fmt == "xlsx":
        return to_xlsx(doc)
    if fmt == "pdf":
        return to_pdf(doc)
    raise ValueError(f"unknown export format {fmt!r}")
