"""Payroll tests on the client's payroll register, as DataSnipper and Inflo run them: ghost
employees, people paid twice or after they left, odd pay, and the payroll against the
employee costs in the trial balance.

The register is uploaded as its own kind of file. The auditor says which column holds the
employee number, name, bank account, pay date, gross and net pay and so on (guessed from the
headers until then). Nothing is saved; each look is logged, and the result downloads to
Excel. It only reads, so it works on locked files too.
"""

from __future__ import annotations

import io
import re
from typing import Annotated

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.orm import Session

from . import listings
from .auth import current_user, log
from .db import Engagement, Upload, User, get_session
from .engagements import get_or_404, safe_stem
from .exports import attachment, finish_workbook
from .fixed_assets import period, tb_figures
from .leases import threshold

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
KINDS = ("payroll",)
FIELDS = {
    "employee": "Employee number", "name": "Name", "bank": "Bank account",
    "department": "Department", "paid_on": "Pay date or month", "gross": "Gross pay",
    "deductions": "Deductions", "net": "Net pay", "joined": "Date joined", "left": "Date left",
}
SYNONYMS = {
    "employee": ["employeeno", "employeenumber", "employeeid", "employeecode", "empno", "empid",
                 "empcode", "staffno", "staffid", "id", "code"],
    "name": ["employeename", "name", "fullname", "staffname", "employee"],
    "bank": ["bankaccount", "bankaccountno", "bankaccountnumber", "accountno", "accountnumber",
             "iban", "bankiban", "account"],
    "department": ["department", "dept", "costcentre", "costcenter", "division", "section"],
    "paid_on": ["paydate", "paymentdate", "datepaid", "payperiod", "period", "month", "date"],
    "gross": ["grosspay", "gross", "grosssalary", "totalgross", "grossamount", "totalearnings",
              "earnings"],
    "deductions": ["deductions", "totaldeductions", "tax", "incometax", "paye"],
    "net": ["netpay", "net", "netsalary", "nettopay", "netamount", "amountpaid", "takehome"],
    "joined": ["datejoined", "joiningdate", "startdate", "hiredate", "dateofjoining",
               "employmentdate", "joined"],
    "left": ["dateleft", "leavingdate", "enddate", "terminationdate", "leftdate",
             "resignationdate", "left"],
}
MAX_SHOWN = 200
SPIKE = 2.0  # a month's pay at least twice the person's usual pay
MOSTLY = 0.5  # a test runs when at least half the lines have what it needs
MONTHS = {m: i for i, names in enumerate(
    [("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",),
     ("june", "jun"), ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"),
     ("october", "oct"), ("november", "nov"), ("december", "dec")], start=1) for m in names}
MONTHS |= {m: i for i, m in enumerate(["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
                                        "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر",
                                        "ديسمبر"], start=1)} | {"ابريل": 4, "اغسطس": 8,
                                                                "اكتوبر": 10}
MONTH_TEXT = re.compile(r"^([^\W\d_]{3,9})\.?(?:[\s\-/']*(\d{2}|\d{4}))?$")
NOTES = {
    "dates": "Most pay dates couldn't be read or fall outside the year, so the tests by date "
             "were skipped and the whole register is tied to the trial balance.",
    "bank": "Most lines have no bank account, so the bank account tests were skipped.",
}
Column = Annotated[str, StringConstraints(max_length=255)]


class PayrollIn(BaseModel):
    columns: dict[str, Column] = Field(default_factory=dict, max_length=len(FIELDS))


class PayrollError(ValueError):
    head: dict = {}


def money(v) -> float:
    return round(float(v), 2) + 0.0


def month_end(month: int, year: int | None, start, end) -> pd.Timestamp:
    """The last day of a month: in the year given, or else the one in the period audited."""
    years = [year] if year else range(start.year, end.year + 1)
    for y in years:
        d = pd.Timestamp(y, month, 1) + pd.offsets.MonthEnd(0)
        if year or pd.Timestamp(start) <= d <= pd.Timestamp(end) + pd.offsets.MonthEnd(0):
            return d
    return pd.Timestamp(end.year, month, 1) + pd.offsets.MonthEnd(0)


def pay_dates(values: pd.Series, parsed: pd.Series, start, end) -> pd.Series:
    """Pay dates, also from a month's name ("March", "Mar-25", "مارس") or its number (1 to
    12, when every pay date is one), read as the last day of that month."""
    text = values.map(lambda v: "" if pd.isna(v) else str(v).strip().casefold())
    nums = pd.to_numeric(values, errors="coerce")
    given = text != ""
    numbered = bool(given.any()) and bool(
        (nums[given].notna() & (nums[given] % 1 == 0) & nums[given].between(1, 12)).all())

    def one(t: str, n, d):
        if not t:
            return d
        if numbered:
            return month_end(int(n), None, start, end)
        hit = MONTH_TEXT.match(t)
        if hit and hit[1] in MONTHS:
            y = hit[2]
            year = None if not y else int(y) + (2000 if len(y) == 2 else 0)
            return month_end(MONTHS[hit[1]], year, start, end)
        return d

    return pd.to_datetime(pd.Series([one(t, n, d) for t, n, d in zip(text, nums, parsed, strict=True)],
                                    index=values.index, dtype="object"))


def analyse(raw: pd.DataFrame, cols: dict[str, str], e: Engagement,
            tb_lines: dict[str, float] | None) -> dict:
    if "gross" not in cols and "net" not in cols:
        raise PayrollError("Say which column holds the gross or the net pay.")
    if "employee" not in cols and "name" not in cols:
        raise PayrollError("Say which column holds the employee number or the name.")
    start, end = period(e)
    first, last = pd.Timestamp(start), pd.Timestamp(end)
    paid_on, joined, left = listings.dates(raw, [cols.get("paid_on"), cols.get("joined"),
                                                 cols.get("left")])
    if "paid_on" in cols:
        paid_on = pay_dates(raw[cols["paid_on"]], paid_on, start, end)
    gross = listings.numbers(raw, cols.get("gross"))
    net = listings.numbers(raw, cols.get("net"))
    df = pd.DataFrame({
        "row": raw["__row"], "employee": listings.text(raw, cols.get("employee")),
        "name": listings.text(raw, cols.get("name")),
        "bank": listings.text(raw, cols.get("bank")).str.replace(r"[\s-]", "", regex=True)
                .str.upper(),
        "department": listings.text(raw, cols.get("department")).replace("", "(none)"),
        "paid_on": paid_on, "gross": gross, "net": net,
        "deductions": listings.numbers(raw, cols.get("deductions")),
        "joined": joined, "left": left,
    })
    # Lines with neither a number nor a name are totals or blank lines, not people.
    named = (df["employee"] != "") | (df["name"] != "")
    skipped = int((~named).sum())
    df = df[named]
    if df.empty:
        raise PayrollError("No line has an employee number or name in the columns picked.")
    notes = []
    if skipped:
        notes.append(f"{skipped} lines with no employee number or name, such as totals, were "
                     "left out.")
    # Who someone is: their number, else their name.
    df["who"] = df["employee"].where(df["employee"] != "", df["name"].str.casefold())
    df["pay"] = df["gross"].where(df["gross"].notna(), df["net"]).fillna(0.0)
    df["month"] = df["paid_on"].dt.to_period("M").astype(str).where(df["paid_on"].notna(), "")

    issues: list[tuple[int, str]] = []

    def flag(mask: pd.Series, text: str) -> None:
        issues.extend((int(i), text) for i in df.index[mask.fillna(False)])

    # Tests on a column run only when most lines have it filled in, so a wrong guess or an
    # empty column doesn't flag every line.
    bank = df["bank"].where(df["bank"] != "")
    paid = df["pay"] > 0
    bank_ok = "bank" in cols and bank[paid].notna().sum() >= MOSTLY * max(1, int(paid.sum()))
    if "bank" in cols and not bank_ok:
        notes.append(NOTES["bank"])
    given = int(listings.text(raw, cols["paid_on"])[named].ne("").sum()) if "paid_on" in cols else 0
    in_period = (df["paid_on"] >= first) & (df["paid_on"] <= last)
    dates_ok = "paid_on" in cols and given > 0 and in_period.sum() >= MOSTLY * given
    if "paid_on" in cols and not dates_ok:
        notes.append(NOTES["dates"])
    if bank_ok:
        people = df[bank.notna()].groupby("bank")["who"].nunique()
        shared = set(people[people > 1].index)
        flag(bank.isin(shared), "The same bank account as another employee")
        flag(bank.isna() & (df["pay"] > 0), "No bank account")
    if "employee" in cols and "name" in cols:
        ids = df[df["employee"] != ""]
        names = ids.groupby("employee")["name"].apply(lambda s: s.str.casefold().nunique())
        flag(df["employee"].isin(set(names[names > 1].index)),
             "The same employee number with different names")
        numbers_ = ids[ids["name"] != ""].assign(key=ids["name"].str.casefold()) \
            .groupby("key")["employee"].nunique()
        flag(df["name"].str.casefold().isin(set(numbers_[numbers_ > 1].index)) & (df["name"] != ""),
             "The same name with different employee numbers")
    if "employee" in cols:
        flag((df["employee"] == "") & (df["pay"] > 0), "No employee number")
    if dates_ok:
        dup = df[df["month"] != ""].duplicated(["who", "month"], keep=False)
        flag(dup.reindex(df.index, fill_value=False), "Paid more than once in the month")
        flag(df["paid_on"].notna() & df["left"].notna()
             & (df["paid_on"].dt.to_period("M") > df["left"].dt.to_period("M")),
             "Paid after leaving")
        flag(df["paid_on"].notna() & df["joined"].notna()
             & (df["paid_on"].dt.to_period("M") < df["joined"].dt.to_period("M")),
             "Paid before joining")
        flag((df["paid_on"] < first) | (df["paid_on"] > last), "Paid outside the year")
    if "gross" in cols and "net" in cols:
        flag(df["net"] > df["gross"] + 0.005, "Net pay is more than gross pay")
        flag((df["gross"] > 0) & ((df["gross"] - df["net"]).abs() < 0.005),
             "Nothing deducted from gross pay")
        if "deductions" in cols:
            flag((df["gross"] - df["deductions"].fillna(0) - df["net"]).abs() > 1,
                 "Gross less deductions isn't the net pay")
    flag(df["pay"] < 0, "Negative pay")
    usual = df[df["pay"] > 0].groupby("who")["pay"].median()
    spike = df["pay"] >= SPIKE * df["who"].map(usual)
    counts = df.groupby("who")["pay"].transform("count")
    flag(spike & (counts >= 3), "At least twice this person's usual pay")

    by_row: dict[int, list[str]] = {}
    for i, text in issues:
        by_row.setdefault(i, []).append(text)

    def rows(frame: pd.DataFrame) -> list[dict]:
        return [{"row": int(r["row"]), "employee": r["employee"], "name": r["name"],
                 "bank": r["bank"], "department": r["department"],
                 "paid_on": r["paid_on"].date().isoformat() if pd.notna(r["paid_on"]) else None,
                 "gross": None if pd.isna(r["gross"]) else money(r["gross"]),
                 "net": None if pd.isna(r["net"]) else money(r["net"]),
                 "issues": by_row.get(i, [])} for i, r in frame.iterrows()]

    months = []
    if dates_ok:
        for month, g in df[df["month"] != ""].groupby("month", sort=True):
            months.append({"month": month, "people": int(g["who"].nunique()),
                           "gross": money(g["gross"].sum()) if "gross" in cols else None,
                           "net": money(g["net"].sum()) if "net" in cols else None})
    departments = [{"department": d, "people": int(g["who"].nunique()),
                    "pay": money(g["pay"].sum())}
                   for d, g in df.groupby("department", sort=True)]
    in_year = df[in_period] if dates_ok else df
    total = money(in_year["pay"].sum())
    tie = None
    if tb_lines is not None:
        books = tb_lines.get("staff_costs")
        diff = None if books is None else money(total - books)
        tie = {"register": total, "trial_balance": books, "difference": diff,
               "basis": "gross" if "gross" in cols else "net",
               "material": diff is not None and abs(diff) > threshold(e)}
    issue_counts: dict[str, int] = {}
    for texts in by_row.values():
        for t in texts:
            issue_counts[t] = issue_counts.get(t, 0) + 1
    flagged = df.loc[sorted(by_row)]
    return {
        "columns": cols, "period_start": start.isoformat(), "period_end": end.isoformat(),
        "rows": len(df), "skipped_rows": skipped, "notes": notes, "people": int(df["who"].nunique()),
        "gross": money(df["gross"].sum()) if "gross" in cols else None,
        "net": money(df["net"].sum()) if "net" in cols else None,
        "months": months, "departments": departments, "tie": tie,
        "issue_counts": [{"issue": k, "rows": v} for k, v in
                         sorted(issue_counts.items(), key=lambda kv: -kv[1])],
        "issues": {"count": len(by_row), "items": rows(flagged)},
    }


def run(session: Session, upload: Upload, columns: dict[str, str]) -> tuple[dict, dict]:
    raw = listings.read(upload, KINDS, "a payroll register")
    cols = listings.columns_for(raw, columns, SYNONYMS)
    e = upload.engagement
    head = {"upload_id": upload.id, "filename": upload.filename,
            "file_columns": [c for c in raw.columns if c != "__row"],
            "fields": [{"id": k, "label": v} for k, v in FIELDS.items()], "columns": cols}
    try:
        return head, analyse(raw, cols, e, tb_figures(session, e))
    except PayrollError as exc:
        exc.head = head
        raise


@router.post("/uploads/{upload_id}/payroll")
def payroll(upload_id: int, body: PayrollIn, user: User = Depends(current_user),
            session: Session = Depends(get_session)) -> dict:
    upload = get_or_404(session, Upload, upload_id, user)
    try:
        head, result = run(session, upload, body.columns)
    except PayrollError as exc:
        return {**exc.head, "problem": str(exc)}
    log(session, user, "analysis.payroll", f"Tested the payroll register {upload.filename}",
        upload.engagement,
        details={"upload_id": upload.id, "sha256": upload.sha256, "columns": result["columns"],
                 "rows": result["rows"], "issues": result["issues"]["count"]})
    session.commit()
    items = result["issues"]["items"]
    return {**head, **result, "issues": {**result["issues"], "items": items[:MAX_SHOWN]}}


def to_excel(e: Engagement, upload: Upload, r: dict) -> bytes:
    about = [("Client", e.client.name), ("Engagement", e.name),
             ("Period", f"{r['period_start']} to {r['period_end']}"),
             ("File", f"{upload.filename} (SHA-256 {upload.sha256})"),
             *((f"Column for {FIELDS[k].lower()}", v) for k, v in r["columns"].items()),
             ("Lines in the register", r["rows"]), ("Employees", r["people"]),
             ("Gross pay", r["gross"]), ("Net pay", r["net"]),
             ("Lines to look at", r["issues"]["count"])]
    if r["tie"]:
        about += [("Paid in the year (register)", r["tie"]["register"]),
                  ("Employee costs (trial balance)", r["tie"]["trial_balance"]),
                  ("Difference (register less trial balance)", r["tie"]["difference"])]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame([{"Month": m["month"], "Employees": m["people"], "Gross pay": m["gross"],
                       "Net pay": m["net"]} for m in r["months"]],
                     columns=["Month", "Employees", "Gross pay", "Net pay"]).to_excel(
            xl, sheet_name="By month", index=False)
        pd.DataFrame([{"Department": d["department"], "Employees": d["people"],
                       "Pay": d["pay"]} for d in r["departments"]],
                     columns=["Department", "Employees", "Pay"]).to_excel(
            xl, sheet_name="By department", index=False)
        pd.DataFrame([{"Row": x["row"], "Employee": x["employee"], "Name": x["name"],
                       "Bank account": x["bank"], "Department": x["department"],
                       "Paid on": x["paid_on"], "Gross pay": x["gross"], "Net pay": x["net"],
                       "Points to look at": "; ".join(x["issues"])}
                      for x in r["issues"]["items"]],
                     columns=["Row", "Employee", "Name", "Bank account", "Department",
                              "Paid on", "Gross pay", "Net pay", "Points to look at"]).to_excel(
            xl, sheet_name="Points to look at", index=False)
        finish_workbook(xl.book)
    return buf.getvalue()


@router.post("/uploads/{upload_id}/payroll/export")
def export_payroll(upload_id: int, body: PayrollIn, user: User = Depends(current_user),
                   session: Session = Depends(get_session)) -> Response:
    upload = get_or_404(session, Upload, upload_id, user)
    try:
        _, result = run(session, upload, body.columns)
    except PayrollError as exc:
        raise HTTPException(400, str(exc)) from exc
    e = upload.engagement
    log(session, user, "analysis.download",
        f"Downloaded the payroll workpaper for {upload.filename}", e,
        details={"upload_id": upload.id, "columns": result["columns"]})
    session.commit()
    stem = safe_stem(f"{e.client.name}-{e.name}-payroll")
    return Response(to_excel(e, upload, result), media_type=XLSX,
                    headers=attachment(f"{stem}.xlsx"))
