"""Risk register and audit program: the risks of material misstatement (ISA 315), each with
the procedures planned in response (ISA 330) and their results.

Risks can be added by hand or taken from an AI risk assessment draft (ai_writer.py). Every
change is logged, and the register exports to Excel through exports.finish_workbook.
"""

from __future__ import annotations

import io
from typing import Annotated, Literal

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints, field_validator
from sqlalchemy.orm import Session

from . import auth
from .ai_writer import AREAS, ASSERTIONS, LEVELS
from .auth import current_user, log, require
from .db import AiDraft, Engagement, Risk, RiskProcedure, User, get_session, utcnow
from .engagements import get_or_404, held_engagement, safe_stem
from .exports import attachment, finish_workbook
from .statements import LINE, LINES

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
STATUSES = {"planned": "Planned", "done": "Done"}
LEVEL_RANK = {"significant": 0, "higher": 1, "lower": 2}
MAX_RISKS = 500
MAX_PROCEDURES = 50  # per risk
Level = Literal["significant", "higher", "lower"]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
ProcText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


def area_label(area: str) -> str:
    return LINE[area]["label"] if area in LINE else AREAS.get(area, area)


def check_area(area: str) -> str:
    if area not in LINE and area not in AREAS:
        raise ValueError("Pick an area from the list.")
    return area


def check_assertions(values: list[str]) -> list[str]:
    unknown = [a for a in values if a not in ASSERTIONS]
    if unknown:
        raise ValueError(f"Unknown assertion: {unknown[0]}.")
    return list(dict.fromkeys(values))


class RiskIn(BaseModel):
    area: str
    title: Title
    level: Level
    assertions: list[str] = Field(default=[], max_length=len(ASSERTIONS))
    description: str = Field(default="", max_length=5000)
    response: str = Field(default="", max_length=5000)
    procedures: list[str] = Field(default=[], max_length=MAX_PROCEDURES)

    _area = field_validator("area")(classmethod(lambda cls, v: check_area(v)))
    _assertions = field_validator("assertions")(classmethod(lambda cls, v: check_assertions(v)))


class RiskPatch(BaseModel):
    area: str | None = None
    title: Title | None = None
    level: Level | None = None
    assertions: list[str] | None = Field(default=None, max_length=len(ASSERTIONS))
    description: str | None = Field(default=None, max_length=5000)
    response: str | None = Field(default=None, max_length=5000)

    _area = field_validator("area")(
        classmethod(lambda cls, v: v if v is None else check_area(v)))
    _assertions = field_validator("assertions")(
        classmethod(lambda cls, v: v if v is None else check_assertions(v)))


class ProcedureIn(BaseModel):
    text: ProcText


class ProcedurePatch(BaseModel):
    text: ProcText | None = None
    status: Literal["planned", "done"] | None = None
    conclusion: str | None = Field(default=None, max_length=5000)
    reference: str | None = Field(default=None, max_length=200)


def procedure_out(p: RiskProcedure) -> dict:
    return {"id": p.id, "risk_id": p.risk_id, "text": p.text, "status": p.status,
            "status_label": STATUSES[p.status], "conclusion": p.conclusion,
            "reference": p.reference, "done_by": p.done_by,
            "done_at": p.done_at.isoformat() if p.done_at else None}


def risk_out(r: Risk) -> dict:
    done = sum(1 for p in r.procedures if p.status == "done")
    return {"id": r.id, "engagement_id": r.engagement_id, "area": r.area,
            "area_label": area_label(r.area), "title": r.title, "level": r.level,
            "level_label": LEVELS[r.level], "assertions": r.assertions,
            "assertion_labels": [ASSERTIONS[a] for a in r.assertions],
            "description": r.description, "response": r.response, "source": r.source,
            "created_by": r.created_by, "created_at": r.created_at.isoformat(),
            "procedures": [procedure_out(p) for p in r.procedures],
            "done": done, "planned": len(r.procedures) - done}


def summary(risks: list[Risk]) -> dict:
    procs = [p for r in risks for p in r.procedures]
    done = sum(1 for p in procs if p.status == "done")
    return {
        "risks": len(risks),
        "by_level": {lv: sum(1 for r in risks if r.level == lv) for lv in LEVELS},
        "procedures": len(procs), "done": done, "planned": len(procs) - done,
        "without_procedures": sum(1 for r in risks if not r.procedures),
    }


def sorted_risks(e: Engagement) -> list[Risk]:
    return sorted(e.risks, key=lambda r: (LEVEL_RANK[r.level], r.id))


def register_out(e: Engagement) -> dict:
    risks = sorted_risks(e)
    return {"summary": summary(risks), "risks": [risk_out(r) for r in risks]}


@router.get("/risk-register/options")
def options() -> dict:
    return {
        "levels": [{"id": k, "label": v} for k, v in LEVELS.items()],
        "assertions": [{"id": k, "label": v} for k, v in ASSERTIONS.items()],
        "areas": [{"id": k, "label": v} for k, v in AREAS.items()]
                 + [{"id": id_, "label": label} for id_, label, _ in LINES],
    }


@router.get("/engagements/{engagement_id}/risks")
def list_risks(engagement_id: int, user: User = Depends(current_user),
               session: Session = Depends(get_session)) -> dict:
    return register_out(get_or_404(session, Engagement, engagement_id, user))


def held(session: Session, user: User, model, record_id: int):
    """The risk or procedure, with its engagement's register guard held, so two people
    changing the same register (or one removing a risk another adds to) take turns."""
    engagement_id = get_or_404(session, model, record_id, user).engagement_id
    auth.hold(session, f"risks:{engagement_id}")
    session.expire_all()  # another request may have changed it before we held the guard
    return get_or_404(session, model, record_id, user)


def add_risk(session: Session, e: Engagement, user: User, body: RiskIn,
             source: str = "") -> Risk:
    r = Risk(engagement=e, area=body.area, title=body.title.strip(), level=body.level,
             assertions=body.assertions, description=body.description.strip(),
             response=body.response.strip(), source=source, created_by=user.name)
    for text in body.procedures:
        if text.strip():
            r.procedures.append(RiskProcedure(text=text.strip()[:2000]))
    session.add(r)
    session.flush()
    return r


def room_for(e: Engagement, n: int) -> None:
    if len(e.risks) + n > MAX_RISKS:
        raise HTTPException(400, f"An engagement can have at most {MAX_RISKS} risks.")


@router.post("/engagements/{engagement_id}/risks", status_code=201)
def create_risk(engagement_id: int, body: RiskIn, user: User = Depends(require("prepare")),
                session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"risks:{engagement_id}")
    room_for(e, 1)
    r = add_risk(session, e, user, body)
    log(session, user, "risk.create", f"Added risk #{r.id}: {r.title}", e,
        details={"risk_id": r.id, "area": r.area, "level": r.level,
                 "procedures": len(r.procedures)})
    session.commit()
    return risk_out(r)


@router.post("/engagements/{engagement_id}/risks/from-draft/{draft_id}", status_code=201)
def import_draft(engagement_id: int, draft_id: int, user: User = Depends(require("prepare")),
                 session: Session = Depends(get_session)) -> dict:
    """Add the risks of an AI risk assessment to the register, skipping any already there
    (same area and title)."""
    e = held_engagement(session, user, engagement_id, f"risks:{engagement_id}")
    d = get_or_404(session, AiDraft, draft_id, user)
    if d.engagement_id != e.id or d.kind != "risk":
        raise HTTPException(400, "Pick a risk assessment from this engagement.")
    have = {(r.area, r.title.strip().lower()) for r in e.risks}
    new = []
    for item in d.content.get("risks", []):
        key = (item["area"], item["title"].strip().lower())
        if key in have:
            continue
        have.add(key)
        new.append(RiskIn(area=item["area"], title=item["title"][:300], level=item["level"],
                          assertions=item.get("assertions", []),
                          description=item.get("why", "")[:5000],
                          procedures=item.get("procedures", [])[:MAX_PROCEDURES]))
    room_for(e, len(new))
    added = [add_risk(session, e, user, body, f"ai-draft:{d.id}") for body in new]
    log(session, user, "risk.import",
        f"Added {len(added)} risks from AI risk assessment #{d.id}", e,
        details={"draft_id": d.id, "risk_ids": [r.id for r in added],
                 "skipped": len(d.content.get("risks", [])) - len(added)})
    session.commit()
    return {"added": len(added), "skipped": len(d.content.get("risks", [])) - len(added),
            **register_out(e)}


@router.patch("/risks/{risk_id}")
def update_risk(risk_id: int, body: RiskPatch, user: User = Depends(require("prepare")),
                session: Session = Depends(get_session)) -> dict:
    r = held(session, user, Risk, risk_id)
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    for field, value in changes.items():
        setattr(r, field, value.strip() if isinstance(value, str) else value)
    log(session, user, "risk.update", f"Changed risk #{r.id}: {r.title}", r.engagement,
        details={"risk_id": r.id, "changed": sorted(changes)})
    session.commit()
    return risk_out(r)


@router.delete("/risks/{risk_id}", status_code=204)
def delete_risk(risk_id: int, user: User = Depends(require("review")),
                session: Session = Depends(get_session)) -> Response:
    r = held(session, user, Risk, risk_id)
    log(session, user, "risk.delete", f"Removed risk #{r.id}: {r.title}", r.engagement,
        details={"risk_id": r.id, "procedures": len(r.procedures),
                 "done": sum(1 for p in r.procedures if p.status == "done")})
    unlinked = [c for c in r.engagement.controls if c.risk_id == r.id]
    for c in unlinked:  # controls that addressed it no longer point at it
        c.risk_id = None
    if unlinked:  # a change to the controls section too
        log(session, user, "control.update",
            f"Unlinked {len(unlinked)} controls from removed risk #{r.id}", r.engagement,
            details={"control_ids": [c.id for c in unlinked], "risk_id": r.id})
    session.delete(r)
    session.commit()
    return Response(status_code=204)


@router.post("/risks/{risk_id}/procedures", status_code=201)
def add_procedure(risk_id: int, body: ProcedureIn, user: User = Depends(require("prepare")),
                  session: Session = Depends(get_session)) -> dict:
    r = held(session, user, Risk, risk_id)
    if len(r.procedures) >= MAX_PROCEDURES:
        raise HTTPException(400, f"A risk can have at most {MAX_PROCEDURES} procedures.")
    p = RiskProcedure(risk=r, text=body.text.strip())
    session.add(p)
    session.flush()
    log(session, user, "procedure.create", f"Planned a procedure for risk #{r.id}",
        r.engagement, details={"risk_id": r.id, "procedure_id": p.id})
    session.commit()
    return risk_out(r)


@router.patch("/procedures/{procedure_id}")
def update_procedure(procedure_id: int, body: ProcedurePatch,
                     user: User = Depends(require("prepare")),
                     session: Session = Depends(get_session)) -> dict:
    p = held(session, user, RiskProcedure, procedure_id)
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if changes.get("status") == "done" and p.status != "done":
        p.done_by, p.done_at = user.name, utcnow()
    elif changes.get("status") == "planned":
        p.done_by, p.done_at = "", None
    for field, value in changes.items():
        setattr(p, field, value.strip() if isinstance(value, str) else value)
    r = p.risk
    what = {"done": "Completed", "planned": "Reopened"}.get(changes.get("status", ""), "Changed")
    log(session, user, "procedure.update", f"{what} a procedure for risk #{r.id}",
        r.engagement, details={"risk_id": r.id, "procedure_id": p.id,
                               "changed": sorted(changes), "status": p.status})
    session.commit()
    return risk_out(r)


@router.delete("/procedures/{procedure_id}", status_code=204)
def delete_procedure(procedure_id: int, user: User = Depends(require("prepare")),
                     session: Session = Depends(get_session)) -> Response:
    p = held(session, user, RiskProcedure, procedure_id)
    if p.status == "done" and user.role not in ("reviewer", "partner", "admin"):
        raise HTTPException(403, "Only a reviewer can remove a procedure that has been done.")
    r = p.risk
    log(session, user, "procedure.delete", f"Removed a procedure from risk #{r.id}",
        r.engagement, details={"risk_id": r.id, "procedure_id": p.id, "status": p.status})
    session.delete(p)
    session.commit()
    return Response(status_code=204)


def to_excel(e: Engagement, context: dict) -> bytes:
    risks = sorted_risks(e)
    s = summary(risks)
    about = list(context.items()) + [
        ("Risks", s["risks"]), *[(LEVELS[k], v) for k, v in s["by_level"].items()],
        ("Procedures planned", s["procedures"]), ("Procedures done", s["done"]),
    ]
    rows, procs = [], []
    for n, r in enumerate(risks, 1):
        rows.append({"Ref": f"R{n}", "Level": LEVELS[r.level], "Area": area_label(r.area),
                     "Risk": r.title,
                     "Assertions": ", ".join(ASSERTIONS[a] for a in r.assertions),
                     "Why": r.description, "Overall response": r.response,
                     "Procedures": len(r.procedures),
                     "Done": sum(1 for p in r.procedures if p.status == "done")})
        for m, p in enumerate(r.procedures, 1):
            procs.append({"Ref": f"R{n}.{m}", "Risk": r.title, "Procedure": p.text,
                          "Status": STATUSES[p.status], "Workpaper": p.reference,
                          "Conclusion": p.conclusion, "Done by": p.done_by,
                          "Done at": f"{p.done_at:%Y-%m-%d %H:%M} UTC" if p.done_at else ""})
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame(rows, columns=["Ref", "Level", "Area", "Risk", "Assertions", "Why",
                                    "Overall response", "Procedures", "Done"]).to_excel(
            xl, sheet_name="Risk register", index=False)
        pd.DataFrame(procs, columns=["Ref", "Risk", "Procedure", "Status", "Workpaper",
                                     "Conclusion", "Done by", "Done at"]).to_excel(
            xl, sheet_name="Audit program", index=False)
        finish_workbook(xl.book)
    return buf.getvalue()


@router.get("/engagements/{engagement_id}/risks/export")
def export_risks(engagement_id: int, user: User = Depends(current_user),
                 session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    log(session, user, "risk.download", "Downloaded the risk register and audit program", e,
        details={"risks": len(e.risks)})
    session.commit()
    name, content = workbook(e)
    return Response(content, media_type=XLSX, headers=attachment(name))


def workbook(e: Engagement) -> tuple[str, bytes]:
    """The risk register and audit program as an Excel file, and its name."""
    context = {"Client": e.client.name, "Engagement": e.name,
               "Period end": e.period_end.isoformat()}
    stem = safe_stem(f"{e.client.name}-{e.name}-risk-register")
    return f"{stem}.xlsx", to_excel(e, context)
