"""The IFRS disclosure checklist, with the AI assistant reading the draft financial statements.

The team marks each disclosure as given, missing or not applicable, with where it is in the
statements. The AI assistant can read a PDF of the draft statements and suggest an answer for
each item, quoting where it found it; its suggestions are kept beside the team's answers and
only become answers when someone accepts them.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from typing import Annotated, Literal

import pandas as pd
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.orm import Session

from . import assistant
from .ai_tools import (
    DATA_ONLY,
    DOCUMENT_TIMEOUT,
    MAX_DOCUMENT_BYTES,
    _list,
    _text,
    ask,
    document_block,
    require_ai,
)
from .auth import current_user, log, require
from .db import DisclosureChecklist, Engagement, User, get_session, utcnow
from .engagements import get_or_404, held_engagement, safe_stem
from .exports import attachment, finish_workbook

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# (id, standard, the disclosure). The ones most often missed or most often asked about, not
# every paragraph of every standard: the checklist points the team at them, and the team
# adds what the client's circumstances need.
ITEMS = [
    ("ias1_compliance", "IAS 1", "An explicit and unreserved statement of compliance with IFRS."),
    ("ias1_going_concern", "IAS 1", "Material uncertainties about going concern, or the "
                                    "basis used if the statements aren't on a going concern basis."),
    ("ias1_policies", "IAS 1", "Material accounting policy information."),
    ("ias1_judgements", "IAS 1", "The judgements management made in applying the accounting "
                                 "policies that most affect the amounts recognised."),
    ("ias1_estimates", "IAS 1", "The key sources of estimation uncertainty, with the assets "
                                "and liabilities at risk of a material adjustment next year."),
    ("ias1_comparatives", "IAS 1", "Comparative figures for the previous period."),
    ("ias1_capital", "IAS 1", "The entity's objectives, policies and processes for managing "
                              "capital."),
    ("ias1_dividends", "IAS 1", "Dividends proposed or declared before the statements were "
                                "authorised, and the amount per share."),
    ("ias1_entity", "IAS 1", "The entity's domicile, legal form, registered office and the "
                             "nature of its operations."),
    ("ias7_cash", "IAS 7", "The components of cash and cash equivalents, reconciled to the "
                           "statement of financial position."),
    ("ias7_financing", "IAS 7", "Changes in liabilities arising from financing activities."),
    ("ias7_noncash", "IAS 7", "Significant investing and financing transactions that used no "
                              "cash."),
    ("ias8_new_standards", "IAS 8", "New standards issued but not yet effective, and their "
                                    "expected effect."),
    ("ias8_changes", "IAS 8", "Changes in accounting policies or estimates, and corrections of "
                              "prior period errors."),
    ("ias10_date", "IAS 10", "The date the statements were authorised for issue and who "
                             "authorised them."),
    ("ias10_events", "IAS 10", "Material events after the reporting period that need no "
                               "adjustment, with their effect."),
    ("ias12_tax", "IAS 12", "The major components of the tax expense, and a reconciliation of "
                            "it to the accounting profit times the tax rate."),
    ("ias12_deferred", "IAS 12", "Deferred tax assets and liabilities by type of temporary "
                                 "difference, and unused tax losses."),
    ("ias16_ppe", "IAS 16", "A reconciliation of property, plant and equipment from the start "
                            "to the end of the year, by class."),
    ("ias16_methods", "IAS 16", "Depreciation methods, useful lives or rates for each class."),
    ("ias2_inventory", "IAS 2", "The inventory policy (cost formula), carrying amounts by "
                                "classification and any write-downs."),
    ("ias19_benefits", "IAS 19", "Employee benefit plans: the defined benefit obligation, the "
                                 "assumptions used and the amounts recognised."),
    ("ias24_related", "IAS 24", "Related party relationships, transactions and outstanding "
                                "balances, and key management personnel compensation."),
    ("ias24_parent", "IAS 24", "The name of the parent and the ultimate controlling party."),
    ("ias36_impairment", "IAS 36", "Impairment losses or reversals, and the key assumptions "
                                   "for goodwill and other assets tested."),
    ("ias37_provisions", "IAS 37", "A reconciliation of each class of provision, with its "
                                   "nature and expected timing."),
    ("ias37_contingent", "IAS 37", "Contingent liabilities, and contingent assets where an "
                                   "inflow is probable."),
    ("ias38_intangibles", "IAS 38", "A reconciliation of intangible assets by class, with "
                                    "amortisation methods and useful lives."),
    ("ifrs7_risks", "IFRS 7", "Credit, liquidity and market risk: the exposures and how they "
                              "are managed."),
    ("ifrs7_maturity", "IFRS 7", "A maturity analysis of financial liabilities."),
    ("ifrs7_categories", "IFRS 7", "Carrying amounts of financial assets and liabilities by "
                                   "category."),
    ("ifrs7_ecl", "IFRS 7", "Expected credit losses: the method, inputs and a reconciliation "
                            "of the loss allowance."),
    ("ifrs13_fair_value", "IFRS 13", "Fair value measurements by level of the fair value "
                                     "hierarchy, with the techniques and inputs."),
    ("ifrs15_revenue", "IFRS 15", "Revenue disaggregated into categories, and how "
                                  "performance obligations are satisfied."),
    ("ifrs15_balances", "IFRS 15", "Contract assets, contract liabilities and receivables from "
                                   "contracts with customers."),
    ("ifrs16_leases", "IFRS 16", "Right-of-use assets by class, lease liabilities, and the "
                                 "lease expenses and cash outflows."),
    ("ifrs8_segments", "IFRS 8", "Segment information, where the entity's shares or debt are "
                                 "traded publicly."),
    ("commitments", "IAS 16 / IAS 38", "Commitments to buy property, plant and equipment or "
                                       "intangible assets."),
]
ITEM = {i: (standard, text) for i, standard, text in ITEMS}
STATUSES = {"yes": "Given", "no": "Missing", "na": "Not applicable"}
AI_STATUSES = {"present": "yes", "missing": "no", "not_applicable": "na"}
AI_TOKENS = 16000
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]

SYSTEM = (
    "You help a financial statement auditor check that a set of draft financial statements "
    "gives the disclosures IFRS requires. " + DATA_ONLY +
    "For each checklist item you are given, decide from the document whether the disclosure "
    "is present, missing, or not applicable (the entity plainly has nothing it would apply "
    "to, such as no leases). Only say present if you can point to it. Answer with JSON only, "
    "no other text, in exactly this shape: "
    '{"items": [{"id": "<item id>", "status": "present|missing|not_applicable", '
    '"where": "<note number, heading or page where it is, or empty>", '
    '"evidence": "<a short quote or reason, at most two sentences>"}]}. '
    "Include every item once. Write the evidence in the language of the statements.")


class AnswerIn(BaseModel):
    status: Literal["yes", "no", "na"] | None = None
    reference: Text = ""
    comment: Text = ""


class ChecklistIn(BaseModel):
    answers: dict[str, AnswerIn] = Field(default={}, max_length=len(ITEMS))


def answered(c: DisclosureChecklist | None) -> dict[str, dict]:
    raw = (c.answers if c else None) or {}
    return {i: {"status": (raw.get(i) or {}).get("status"),
                "reference": (raw.get(i) or {}).get("reference", ""),
                "comment": (raw.get(i) or {}).get("comment", "")} for i, _, _ in ITEMS}


def counts(answers: dict[str, dict]) -> dict:
    statuses = [a["status"] for a in answers.values()]
    return {"items": len(ITEMS), "answered": sum(1 for s in statuses if s),
            "given": statuses.count("yes"), "missing": statuses.count("no"),
            "na": statuses.count("na")}


def state(e: Engagement) -> dict:
    c = e.disclosures
    answers = answered(c)
    ai = (c.ai if c else None) or None
    suggestions = (ai or {}).get("items", {})
    items = []
    for i, standard, text in ITEMS:
        s = suggestions.get(i)
        a = answers[i]
        items.append({"id": i, "standard": standard, "text": text, **a,
                      "status_label": STATUSES.get(a["status"]),
                      "ai": s and {**s, "status_label": STATUSES[AI_STATUSES[s["status"]]]},
                      # The AI read it differently from the answer given.
                      "differs": bool(s and a["status"] and AI_STATUSES[s["status"]] != a["status"])})
    return {"framework": e.framework, "items": items, "counts": counts(answers),
            "differs": sum(1 for x in items if x["differs"]),
            "ai_pending": sum(1 for x in items if x["ai"] and not x["status"]),
            "ai": ai and {k: v for k, v in ai.items() if k != "items"},
            "updated_by": c.updated_by if c else "",
            "updated_at": c.updated_at.isoformat() if c else None}


def summary(e: Engagement) -> dict:
    """For the dashboard."""
    c = e.disclosures
    return {"started": c is not None and bool(c.answers), **counts(answered(c))}


def save(session: Session, user: User, e: Engagement, updates: dict[str, dict],
         action: str, summary_text: str, extra: dict | None = None) -> None:
    """Merge `updates` into the answers, logging and saving only if anything changed."""
    before = answered(e.disclosures)
    after = {**before, **updates}
    changed = sorted(i for i in ITEM if after[i] != before[i])
    if not changed:
        return
    c = e.disclosures or DisclosureChecklist(engagement=e)
    c.answers = {i: a for i, a in after.items() if a["status"] or a["reference"] or a["comment"]}
    c.updated_by, c.updated_at = user.name, utcnow()
    session.add(c)
    log(session, user, action, summary_text, e,
        details={"changed": changed, "statuses": {i: after[i]["status"] for i in changed},
                 **(extra or {})})
    session.commit()


def clean_ai(answer: dict) -> dict:
    out = {}
    for x in _list(answer.get("items")):
        if not isinstance(x, dict):
            continue
        i, status = x.get("id"), x.get("status")
        if i in ITEM and status in AI_STATUSES and i not in out:
            out[i] = {"status": status, "where": _text(x.get("where"), 200),
                      "evidence": _text(x.get("evidence"), 600)}
    if not out:
        raise ValueError("The AI's answer had no checklist items in it. Try again.")
    return out


# --- Routes ---------------------------------------------------------------------------


@router.get("/engagements/{engagement_id}/disclosures")
def get_disclosures(engagement_id: int, user: User = Depends(current_user),
                    session: Session = Depends(get_session)) -> dict:
    return state(get_or_404(session, Engagement, engagement_id, user))


@router.put("/engagements/{engagement_id}/disclosures")
def save_disclosures(engagement_id: int, body: ChecklistIn,
                     user: User = Depends(require("prepare")),
                     session: Session = Depends(get_session)) -> dict:
    unknown = sorted(set(body.answers) - set(ITEM))
    if unknown:
        raise HTTPException(400, f"Unknown checklist item: {unknown[0]}.")
    e = held_engagement(session, user, engagement_id, f"disclosures:{engagement_id}")
    save(session, user, e, {i: a.model_dump() for i, a in body.answers.items()},
         "disclosure.update", "Updated the disclosure checklist")
    return state(e)


@router.post("/engagements/{engagement_id}/disclosures/ai-review", status_code=201)
def ai_review(  # not async: FastAPI runs it in a thread, so waiting on the AI blocks nothing
    engagement_id: int,
    file: UploadFile = File(...),
    language: Literal["en", "ar"] = Form("en"),
    user: User = Depends(require("prepare")),
    session: Session = Depends(get_session),
) -> dict:
    """The AI assistant reads a PDF of the draft statements and suggests an answer for each
    item. The suggestions are kept beside the answers; nothing is answered for the team."""
    e = get_or_404(session, Engagement, engagement_id, user)
    require_ai()
    content = file.file.read(MAX_DOCUMENT_BYTES + 1)
    if len(content) > MAX_DOCUMENT_BYTES:
        raise HTTPException(413, "The file is larger than 15 MB.")
    block = document_block(content)
    if block["type"] != "document":
        raise HTTPException(400, "Upload the draft financial statements as a PDF.")
    filename = Path(file.filename or "statements.pdf").name
    sha = hashlib.sha256(content).hexdigest()
    checklist = "\n".join(f"{i} ({standard}): {text}" for i, standard, text in ITEMS)
    prompt = (f"The statements are prepared under {e.framework}, for the period ending "
              f"{e.period_end.isoformat()}.\n\n<checklist>\n{checklist}\n</checklist>\n\n"
              "Check the attached statements against each item."
              + (" Write the evidence in Arabic." if language == "ar" else ""))
    items = ask(session, user, e.id, "disclosure.ai_review",
                f"Asked the AI assistant to check the disclosures in {filename}",
                {"filename": filename, "sha256": sha, "size": len(content)}, SYSTEM,
                [block, {"type": "text", "text": prompt}], check=clean_ai,
                max_tokens=AI_TOKENS, timeout=DOCUMENT_TIMEOUT)
    e = held_engagement(session, user, engagement_id, f"disclosures:{engagement_id}")
    c = e.disclosures or DisclosureChecklist(engagement=e, answers={})
    c.ai = {"filename": filename, "sha256": sha, "model": assistant.model(),
            "by": user.name, "at": utcnow().isoformat(), "items": items,
            "missing_items": len(ITEMS) - len(items)}
    session.add(c)
    session.commit()
    return state(e)


@router.post("/engagements/{engagement_id}/disclosures/accept-ai")
def accept_ai(engagement_id: int, user: User = Depends(require("prepare")),
              session: Session = Depends(get_session)) -> dict:
    """Take the AI's suggestion for each item not answered yet; answers given stay."""
    e = held_engagement(session, user, engagement_id, f"disclosures:{engagement_id}")
    c = e.disclosures
    suggestions = ((c.ai if c else None) or {}).get("items", {})
    if not suggestions:
        raise HTTPException(400, "Ask the AI assistant to read the statements first.")
    answers = answered(c)
    updates = {i: {"status": AI_STATUSES[s["status"]], "reference": s["where"],
                   "comment": f"AI: {s['evidence']}" if s["evidence"] else ""}
               for i, s in suggestions.items() if i in ITEM and not answers[i]["status"]}
    save(session, user, e, updates, "disclosure.accept_ai",
         f"Accepted the AI's answers for {len(updates)} disclosure items",
         extra={"source": c.ai.get("filename"), "source_sha256": c.ai.get("sha256")})
    return state(e)


@router.get("/engagements/{engagement_id}/disclosures/export")
def export_disclosures(engagement_id: int, user: User = Depends(current_user),
                       session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    s = state(e)
    log(session, user, "disclosure.download", "Downloaded the disclosure checklist", e)
    session.commit()
    n = s["counts"]
    about = [("Client", e.client.name), ("Engagement", e.name),
             ("Period end", e.period_end.isoformat()), ("Framework", e.framework),
             ("Items", n["items"]), ("Answered", n["answered"]), ("Given", n["given"]),
             ("Missing", n["missing"]), ("Not applicable", n["na"]),
             ("Last changed by", s["updated_by"]), ("Last changed at", s["updated_at"] or "")]
    if s["ai"]:
        about += [("AI read", s["ai"]["filename"]), ("AI read by", s["ai"]["by"]),
                  ("AI read at", s["ai"]["at"]), ("AI model", s["ai"]["model"])]
    rows = [{"Standard": x["standard"], "Disclosure": x["text"],
             "Answer": x["status_label"] or "", "Where": x["reference"],
             "Comment": x["comment"],
             "AI suggestion": x["ai"]["status_label"] if x["ai"] else "",
             "AI found it at": x["ai"]["where"] if x["ai"] else "",
             "AI evidence": x["ai"]["evidence"] if x["ai"] else ""} for x in s["items"]]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame(rows).to_excel(xl, sheet_name="Disclosure checklist", index=False)
        finish_workbook(xl.book)
    stem = safe_stem(f"{e.client.name}-{e.name}-disclosures")
    return Response(buf.getvalue(), media_type=XLSX, headers=attachment(f"{stem}.xlsx"))
