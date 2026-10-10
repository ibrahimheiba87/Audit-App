"""Turn audit results into downloadable CSV or Excel reports."""

from __future__ import annotations

import io

import pandas as pd

FINDING_COLUMNS = ["severity", "check", "row", "entry_id", "date", "account",
                   "description", "amount", "detail"]


def findings_frame(result: dict) -> pd.DataFrame:
    return pd.DataFrame(result["findings"], columns=FINDING_COLUMNS)


def to_csv(result: dict) -> bytes:
    return findings_frame(result).to_csv(index=False).encode("utf-8")


def to_excel(result: dict, source_name: str = "") -> bytes:
    s = result["summary"]
    summary_rows = [
        ("Source file", source_name),
        ("Rows analysed", s["rows"]),
        ("Total debits", s["total_debits"]),
        ("Total credits", s["total_credits"]),
        ("Rows with findings", s["flagged_rows"]),
        ("High findings", s["findings_by_severity"]["high"]),
        ("Medium findings", s["findings_by_severity"]["medium"]),
        ("Low findings", s["findings_by_severity"]["low"]),
    ]
    imp = result.get("import")
    if imp:
        summary_rows[1:1] = [
            ("Rows read", imp["rows_read"]),
            ("Rows skipped", imp["rows_skipped"]),
        ]
        summary_rows += [("Import warning", w) for w in imp["warnings"]]
        summary_rows += [(f"Column for {f}", c) for f, c in imp["mapping"].items()]
    checks = pd.DataFrame(result["checks"])[["name", "status", "flagged", "note"]]
    checks.columns = ["Check", "Status", "Flagged", "Notes"]
    benford = pd.DataFrame(result["benford"]["digits"])

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(summary_rows, columns=["Item", "Value"]).to_excel(
            xl, sheet_name="Summary", index=False)
        checks.to_excel(xl, sheet_name="Summary", index=False, startrow=len(summary_rows) + 2)
        findings_frame(result).to_excel(xl, sheet_name="Findings", index=False)
        benford.to_excel(xl, sheet_name="Benford", index=False)
        for ws in xl.book.worksheets:
            for column in ws.columns:
                width = max(len(str(c.value or "")) for c in column)
                ws.column_dimensions[column[0].column_letter].width = min(max(width + 2, 10), 60)
    return buf.getvalue()
