from __future__ import annotations

import io
from decimal import Decimal

from openpyxl import load_workbook

from aiplatform.export import ExportDoc, filename, render, to_pdf, to_xlsx


def doc(**kw: object) -> ExportDoc:
    base: dict[str, object] = {
        "question": "Mobile vs web transaction value by month",
        "answer": "Mobile 12.750 KWD and Web 4.000 KWD in total.",
        "definition": "Sum of transaction amounts.",
        "columns": ["period", "channel", "value"],
        "rows": [["2026-01-01", "Mobile", "10.5"], ["2026-02-01", "Web", 4]],
        "unit": "KWD",
        "label": "Transaction value (KWD)",
        "totals_columns": ["channel", "value"],
        "totals": [["Mobile", Decimal("12.75")], ["Web", 4]],
        "row_count": 2,
        "requested_by": "HO (ho_analyst)",
    }
    return ExportDoc(**{**base, **kw})  # type: ignore[arg-type]


def test_xlsx_has_data_totals_and_about() -> None:
    wb = load_workbook(io.BytesIO(to_xlsx(doc())))
    ws = wb["Data"]
    assert [c.value for c in ws[1]] == ["Period", "Channel", "Transaction value (KWD)"]
    assert ws["C2"].value == 10.5 and ws["C2"].number_format == "#,##0.000"
    values = [c.value for row in ws.iter_rows() for c in row]
    assert "Total over all periods" in values and 12.75 in values
    assert wb["About"]["B7"].value.startswith("Internal use only")


def test_pdf_renders_english_and_arabic() -> None:
    assert to_pdf(doc()).startswith(b"%PDF")
    ar = doc(question="قيمة المعاملات حسب القناة", answer="الإجمالي 12.75", totals=[], rows=[])
    assert render(ar, "pdf").startswith(b"%PDF")


def test_ratio_and_wide_tables() -> None:
    wide = doc(
        columns=["period", "branch", "channel", "segment", "region", "value"],
        rows=[["2026-01-01", "Salmiya", "Mobile", "Retail", "Capital", Decimal("0.0181")]],
        unit="ratio",
        totals=[],
    )
    assert to_pdf(wide).startswith(b"%PDF")
    assert load_workbook(io.BytesIO(to_xlsx(wide)))["Data"]["F2"].number_format == "0.00%"


def test_filename() -> None:
    assert filename("xlsx").startswith("data-export-") and filename("pdf").endswith(".pdf")
