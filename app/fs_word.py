"""The draft financial statements as a Word document, in English or Arabic, as Caseware's
financial statement production gives them: a cover, the statement of financial position, the
statement of profit or loss, the statement of cash flows (when last year's trial balance is
there) and notes, one for each line, listing its accounts.

The figures come from the adjusted trial balance (leadsheets.py), so they match the lead
sheets and the Excel download. Wording is short and marked [like this] where the team still
has to write it. Nothing is saved; each download is logged.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from . import docx
from .auth import current_user, log
from .db import Engagement, User, get_session
from .engagements import get_or_404, safe_stem
from .exports import attachment
from .fs_wording import LABELS_AR, TEXT
from .leadsheets import compute, entry_ref
from .report_draft import entity_name, framework_text

router = APIRouter(prefix="/api")
TEXT_COLUMN = 5000
NOTE_COLUMN = 900
FIGURE_COLUMN = 1650


def figure(x: float | None) -> str:
    """Whole units, negatives in brackets, a dash for nothing; Western digits either way."""
    if x is None:
        return ""
    n = round(x)
    if n == 0:
        return "-"
    return f"({abs(n):,})" if n < 0 else f"{n:,}"


def statement_tables(data: dict, language: str, has_prior: bool, years: tuple[str, str],
                     notes: dict[str, int]) -> dict[str, dict]:
    t = TEXT[language]

    def label(x: str) -> str:
        return LABELS_AR.get(x, x) if language == "ar" else x

    widths = [TEXT_COLUMN, NOTE_COLUMN, FIGURE_COLUMN] + ([FIGURE_COLUMN] if has_prior else [])
    head = ("head", ["", t["note"], years[0]] + ([years[1]] if has_prior else []))
    by_label = {ls["label"]: ls["line"] for ls in data["lead_sheets"]}

    def rows(lines: list[dict]) -> list:
        out = [head]
        for r in lines:
            note = notes.get(by_label.get(r["label"], ""), "") if r["kind"] == "line" else ""
            if r["kind"] == "heading":
                cells = [label(r["label"]), "", ""] + ([""] if has_prior else [])
            else:
                cells = [label(r["label"]), str(note) if note else "", figure(r["current"])] + (
                    [figure(r["prior"])] if has_prior else [])
            out.append((r["kind"], cells))
        return out

    st = data["statements"]
    tables = {"bs": {"widths": widths, "rows": rows(st["balance_sheet"])},
              "is": {"widths": widths, "rows": rows(st["income_statement"])}}
    cf = data.get("cash_flow")
    if cf:
        tables["cf"] = {"widths": [TEXT_COLUMN + NOTE_COLUMN, FIGURE_COLUMN],
                        "rows": [("head", ["", years[0]])] + [
                            (r["kind"], [label(r["label"]), figure(r["amount"])])
                            for r in cf["rows"]]}
    return tables


def paragraphs(e: Engagement, data: dict, language: str) -> list[tuple[str, str | dict]]:
    t = TEXT[language]
    end = docx.long_date(e.period_end, language)
    has_prior = bool(data.get("has_prior"))
    years = (str(e.period_end.year), str(e.period_end.year - 1))
    currency = (e.plan.currency if e.plan else "") or ""

    def label(x: str) -> str:
        return LABELS_AR.get(x, x) if language == "ar" else x

    # Notes 1 and 2 are general information and the basis of preparation; then one for each
    # line with accounts, in the order the lines appear on the statements.
    by_label = {ls["label"]: ls for ls in data["lead_sheets"]
                if abs(ls["totals"]["adjusted"]) >= 0.5 or abs(ls["totals"]["prior"]) >= 0.5}
    st = data["statements"]
    sheets = list({by_label[r["label"]]["line"]: by_label[r["label"]]
                   for r in st["balance_sheet"] + st["income_statement"]
                   if r["kind"] == "line" and r["label"] in by_label}.values())
    notes = {ls["line"]: i for i, ls in enumerate(sheets, start=3)}
    tables = statement_tables(data, language, has_prior, years, notes)

    out: list[tuple[str, str | dict]] = [
        ("title", entity_name(e)), ("title", t["subtitle"].format(end=end)),
        ("bold", t["draft"])]
    for year, key in ((years[0], "current"), (years[1], "prior")):
        difference = st["difference"][key]
        if abs(difference) >= 0.5 and (key == "current" or has_prior):
            out.append(("text", t["unbalanced"].format(amount=figure(difference), year=year)))
    cf = data.get("cash_flow")
    if cf and abs(cf["difference"]) >= 0.5:
        out.append(("text", t["cash_left"].format(amount=figure(cf["difference"]))))
    if data["unmapped"]:
        out.append(("text", t["unmapped"].format(n=len(data["unmapped"]))))
    out += [("subheading", t["contents"]), ("line", t["bs"].format(end=end)),
            ("line", t["is"].format(end=end))]
    if "cf" in tables:
        out.append(("line", t["cf"].format(end=end)))
    out.append(("line", t["notes"]))
    for key in ("bs", "is", "cf"):
        if key not in tables:
            continue
        out += [("page", ""), ("heading", t[key].format(end=end))]
        if currency:
            out.append(("line", t["in"].format(currency=currency)))
        out.append(("table", tables[key]))

    out += [("page", ""), ("heading", t["notes"]),
            ("subheading", f"1. {t['general']}"), ("text", t["general_text"]),
            ("subheading", f"2. {t['basis']}"),
            ("text", t["basis_text"].format(framework=framework_text(e, language))),
            ("text", t["policies"])]
    widths = [TEXT_COLUMN + NOTE_COLUMN, FIGURE_COLUMN] + ([FIGURE_COLUMN] if has_prior else [])
    for ls in sheets:
        rows = [("head", [t["account"], years[0]] + ([years[1]] if has_prior else []))]
        for a in ls["accounts"]:
            name = f"{a['account']} {a['name']}" if a["name"] else str(a["account"])
            rows.append(("line", [name, figure(a["adjusted"])]
                         + ([figure(a["prior"])] if has_prior else [])))
        rows.append(("subtotal", [t["total"], figure(ls["totals"]["adjusted"])]
                     + ([figure(ls["totals"]["prior"])] if has_prior else [])))
        out += [("subheading", f"{notes[ls['line']]}. {label(ls['label'])}"),
                ("table", {"widths": widths, "rows": rows})]
    return out


@router.get("/engagements/{engagement_id}/statements/word")
def download(engagement_id: int, language: Literal["en", "ar"] = "en",
             upload_id: int | None = None, prior_upload_id: int | None = None,
             user: User = Depends(current_user),
             session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    data, tb, prior = compute(session, e, upload_id, prior_upload_id)
    if tb is None:
        raise HTTPException(400, "Upload a trial balance first.")
    log(session, user, "statements.word",
        f"Downloaded the financial statements in Word ({language.upper()})", e,
        details={"language": language, "upload_id": tb.id, "sha256": tb.sha256,
                 "prior_sha256": prior.sha256 if prior else None,
                 "entries": [entry_ref(x) for x in e.adjusting_entries]})
    session.commit()
    stem = safe_stem(f"{e.client.name}-{e.name}-financial-statements-{language}")
    return Response(docx.build(paragraphs(e, data, language), rtl=language == "ar"),
                    media_type=docx.MIME, headers=attachment(f"{stem}.docx"))
