"""Turn audit results into downloadable CSV or Excel reports."""

from __future__ import annotations

import io

import pandas as pd

from .exports import csv_safe_frame, finish_workbook

FINDING_COLUMNS = ["severity", "check", "row", "entry_id", "date", "account",
                   "description", "amount", "detail"]


def findings_frame(result: dict) -> pd.DataFrame:
    return pd.DataFrame(result["findings"], columns=FINDING_COLUMNS)


RISK_COLUMNS = ["Rank", "Score", "Level", "Entry", "Row", "Lines", "Date", "Account",
                "Description", "Amount", "Why"]


def risk_frame(result: dict) -> pd.DataFrame:
    names = {c["id"]: c["name"] for c in result["checks"]}
    return pd.DataFrame([{
        "Rank": e["rank"], "Score": e["score"], "Level": e["level"], "Entry": e["entry_id"],
        "Row": e["row"], "Lines": e["lines"], "Date": e["date"],
        "Account": e["account"] or (f"{e['accounts']} accounts" if e["accounts"] else None),
        "Description": e["description"], "Amount": e["amount"],
        "Why": ", ".join(names.get(c, c) for c in e["reasons"]),
    } for e in result["risk"]["entries"]], columns=RISK_COLUMNS)


def to_csv(result: dict) -> bytes:
    return csv_safe_frame(findings_frame(result)).to_csv(index=False).encode("utf-8")


def to_excel(result: dict, source_name: str = "", context: dict | None = None) -> bytes:
    s = result["summary"]
    summary_rows = [(k, v) for k, v in (context or {}).items()]
    summary_rows += [
        ("Source file", source_name),
        ("Rows analysed", s["rows"]),
        ("Total debits", s["total_debits"]),
        ("Total credits", s["total_credits"]),
        ("Rows with findings", s["flagged_rows"]),
        ("High findings", s["findings_by_severity"]["high"]),
        ("Medium findings", s["findings_by_severity"]["medium"]),
        ("Low findings", s["findings_by_severity"]["low"]),
    ]
    mat = result.get("materiality") or {}
    if mat.get("performance_materiality"):
        summary_rows.append(("Performance materiality", mat["performance_materiality"]))
    if mat.get("trivial_threshold"):
        summary_rows.append(("Clearly trivial threshold", mat["trivial_threshold"]))
    imp = result.get("import")
    if imp:
        at = next(i for i, (k, _) in enumerate(summary_rows) if k == "Rows analysed")
        summary_rows[at:at] = [
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
        if result.get("risk"):  # runs saved before risk scoring have none
            risk_frame(result).to_excel(xl, sheet_name="Risk ranking", index=False)
        findings_frame(result).to_excel(xl, sheet_name="Findings", index=False)
        benford.to_excel(xl, sheet_name="Benford", index=False)
        finish_workbook(xl.book)
    return buf.getvalue()
