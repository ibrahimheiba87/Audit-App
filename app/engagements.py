"""Clients, engagements, materiality, uploads and saved audit runs."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, object_session

from . import auth, checks, loader, materiality, register, report, templates
from .auth import current_user, log, require
from .db import (
    BankReconciliation,
    Client,
    Comparison,
    CrossMatch,
    Engagement,
    EngagementLock,
    EngagementMember,
    Evidence,
    Finding,
    RequestFile,
    Run,
    RunSummary,
    Sample,
    Upload,
    User,
    get_session,
)
from .exports import attachment
from .uploads import parse_mapping, read_upload

router = APIRouter(prefix="/api")

UPLOAD_KINDS = {"gl": "General ledger", "tb": "Trial balance", "prior_tb": "Prior-year trial balance",
                "bank": "Bank statement", "ar": "Receivables listing", "ap": "Payables listing",
                "fixed_assets": "Fixed asset register", "inventory": "Inventory listing",
                "list": "Other list (such as a vendor or employee master file)"}
# Lists have no amounts to map: they're kept as they are, to be matched against other files,
# and the fixed asset register and inventory listing are read by their own analyses, whose
# columns the auditor picks there.
LISTS = ("list", "fixed_assets", "inventory")
FRAMEWORKS = ["IFRS", "US GAAP", "IFRS for SMEs", "Local GAAP", "Other"]


# --- Schemas ----------------------------------------------------------------

class ClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    industry: str = ""
    notes: str = ""


class ClientPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    industry: str | None = None
    notes: str | None = None


class EngagementIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    period_end: date
    period_start: date | None = None
    framework: str = "IFRS"


class EngagementPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    period_end: date | None = None
    period_start: date | None = None
    framework: str | None = None


class MaterialityIn(BaseModel):
    benchmark: str
    benchmark_amount: float
    benchmark_pct: float | None = None
    performance_pct: float | None = None
    trivial_pct: float | None = None
    rationale: str = ""


class TeamIn(BaseModel):
    user_ids: list[int] = Field(max_length=500)


class RunIn(BaseModel):
    upload_id: int
    cutoff_days: int = Field(default=5, ge=0)
    round_threshold: float = Field(default=1000.0, gt=0, allow_inf_nan=False)
    weekend: Literal["sat-sun", "fri-sat", "fri"] = "sat-sun"
    # parse_keywords keeps the first MAX_KEYWORDS; this only bounds the request.
    keywords: list[str] | None = Field(default=None, max_length=1000)
    template_id: int | None = None  # the firm template these settings came from, if any


# --- Serialization ----------------------------------------------------------

def client_out(c: Client, engagements: list[Engagement] | None = None) -> dict:
    """The client, with `engagements` (the ones the viewer may see) when given."""
    out = {"id": c.id, "name": c.name, "industry": c.industry, "notes": c.notes,
           "created_at": c.created_at.isoformat()}
    if engagements is not None:
        out["engagements"] = [engagement_brief(e) for e in engagements]
    return out


def engagement_brief(e: Engagement) -> dict:
    return {"id": e.id, "client_id": e.client_id, "name": e.name,
            "period_end": e.period_end.isoformat(), "locked": e.lock is not None,
            "archived": e.lock is not None and e.lock.archived}


def lock_out(lock: EngagementLock | None) -> dict | None:
    """Whether the engagement is locked (locking.py), by whom and when, and archived."""
    if lock is None:
        return None
    return {"locked_by": lock.locked_by, "locked_at": lock.locked_at.isoformat(),
            "note": lock.note, "archived": lock.archived, "archived_by": lock.archived_by,
            "archived_at": lock.archived_at.isoformat() if lock.archived_at else None}


def refuse_locked(engagements: list[Engagement]) -> None:
    """For deletes: a locked engagement's file is kept."""
    locked = [e for e in engagements if e.lock is not None]
    if locked:
        raise HTTPException(409, f"{locked[0].name} is locked, so it can't be deleted.")


def materiality_out(e: Engagement) -> dict | None:
    if e.overall_materiality is None:
        return None
    return {
        "benchmark": e.benchmark,
        "benchmark_label": materiality.BENCHMARKS.get(e.benchmark or "", {}).get("label"),
        "benchmark_amount": e.benchmark_amount,
        "benchmark_pct": e.benchmark_pct,
        "performance_pct": e.performance_pct,
        "trivial_pct": e.trivial_pct,
        "overall_materiality": e.overall_materiality,
        "performance_materiality": e.performance_materiality,
        "trivial_threshold": e.trivial_threshold,
        "rationale": e.materiality_rationale,
    }


def materiality_carried(e: Engagement) -> dict | None:
    """The method rolled forward from last year, until this year's materiality is set."""
    if e.overall_materiality is not None or not e.benchmark:
        return None
    return {"benchmark": e.benchmark, "benchmark_pct": e.benchmark_pct,
            "performance_pct": e.performance_pct, "trivial_pct": e.trivial_pct,
            "rationale": e.materiality_rationale}


def upload_out(u: Upload) -> dict:
    return {"id": u.id, "kind": u.kind, "kind_label": UPLOAD_KINDS[u.kind],
            "filename": u.filename, "sha256": u.sha256, "size": u.size, "sheet": u.sheet,
            "mapping": u.mapping, "import": u.import_report,
            "uploaded_at": u.uploaded_at.isoformat()}


def run_brief(r: Run) -> dict:
    s = {"rows": r.summary.rows, "findings_by_severity": r.summary.findings_by_severity} \
        if r.summary else RunSummary.of(r.result)
    return {"id": r.id, "upload_id": r.upload_id, "filename": r.upload.filename,
            "created_at": r.created_at.isoformat(), "settings": r.settings,
            "rows": s["rows"], "findings_by_severity": s["findings_by_severity"]}


def engagement_out(e: Engagement) -> dict:
    return {
        "id": e.id, "client": client_out(e.client), "name": e.name,
        "period_start": e.period_start.isoformat() if e.period_start else None,
        "period_end": e.period_end.isoformat(), "framework": e.framework,
        "created_at": e.created_at.isoformat(), "materiality": materiality_out(e),
        "materiality_carried": materiality_carried(e),
        "uploads": [upload_out(u) for u in e.uploads],
        "runs": [run_brief(r) for r in reversed(e.runs)],
        "comparisons": [comparison_brief(c) for c in reversed(e.comparisons)],
        "cross_matches": [cross_match_brief(m) for m in reversed(e.cross_matches)],
        "samples": [sample_brief(x) for x in reversed(e.samples)],
        "findings_summary": findings_summary(e),
        "team": team_out(e),
        "lock": lock_out(e.lock),
    }


def team_out(e: Engagement) -> list[dict]:
    members = [{"id": m.user.id, "name": m.user.name, "email": m.user.email,
                "role": m.user.role, "role_label": auth.ROLES[m.user.role],
                "active": m.user.active} for m in e.team]
    return sorted(members, key=lambda m: m["name"].lower())


def findings_summary(e: Engagement) -> dict:
    session = object_session(e)
    register.sync_engagement(session, e)  # runs saved before the register existed
    return register.summary_counts(session, e.id)


def sample_brief(x: Sample) -> dict:
    r = x.result
    return {
        "id": x.id, "created_at": x.created_at.isoformat(), "upload_id": x.upload_id,
        "filename": x.upload.filename, "method": x.method, "method_label": r["method_label"],
        "seed": x.seed, "population": r["population"], "sample_size": r["sample_size"],
        "sample_value": r["sample_value"],
        "evaluation": x.evaluation and x.evaluation.result["conclusion"],
    }


def cross_match_brief(m: CrossMatch) -> dict:
    return {
        "id": m.id, "title": m.title, "created_at": m.created_at.isoformat(),
        "created_by": m.created_by, "settings": m.settings,
        "left_upload_id": m.left_upload_id, "right_upload_id": m.right_upload_id,
        "left_filename": m.left_upload.filename, "right_filename": m.right_upload.filename,
        "totals": m.totals,
    }


def comparison_brief(c: Comparison) -> dict:
    return {
        "id": c.id, "created_at": c.created_at.isoformat(), "settings": c.settings,
        "current_upload_id": c.current_upload_id, "prior_upload_id": c.prior_upload_id,
        "current_filename": c.current_upload.filename, "prior_filename": c.prior_upload.filename,
        "summary": c.result["summary"],
    }


def safe_stem(text: str) -> str:
    return "".join(ch for ch in text.replace(" ", "_") if ch.isalnum() or ch in "-_.")


# --- Lookups ----------------------------------------------------------------

def engagement_id_of(obj) -> int | None:
    """The engagement a record belongs to, or None for records that don't belong to one."""
    if isinstance(obj, Engagement):
        return obj.id
    if isinstance(obj, Evidence):
        return obj.finding.engagement_id
    if isinstance(obj, RequestFile):
        return obj.request.engagement_id
    return getattr(obj, "engagement_id", None)


def get_or_404(session: Session, model, id_: int, user: User | None = None):
    """The record, or a 404. Records of an engagement `user` isn't on get the same 404, so
    nobody can tell which engagements exist."""
    obj = session.get(model, id_)
    if obj is None:
        raise HTTPException(404, f"{model.__name__} {id_} not found.")
    engagement_id = engagement_id_of(obj)
    if engagement_id is not None:
        if user is None:
            raise RuntimeError(f"Pass the user to look up a {model.__name__}.")
        if not auth.on_engagement(session, user, engagement_id):
            raise HTTPException(404, f"{model.__name__} {id_} not found.")
    return obj


def assigned_ids(session: Session, user: User) -> set[int] | None:
    """Ids of the engagements the user is on, or None if they see every engagement."""
    if auth.sees_all_engagements(user):
        return None
    return set(session.scalars(auth.assigned_engagement_ids(user)))


def visible_engagements(session: Session, user: User, engagements: list[Engagement],
                        assigned: set[int] | None = None) -> list[Engagement]:
    """The engagements the user may see. Pass `assigned` (from assigned_ids) when calling this
    for many clients, so it is looked up once."""
    if auth.sees_all_engagements(user):
        return list(engagements)
    if assigned is None:
        assigned = assigned_ids(session, user)
    return [e for e in engagements if e.id in assigned]


def held_engagement(session: Session, user: User, engagement_id: int, key: str) -> Engagement:
    """The engagement, with the guard `key` held (see auth.hold). Access is checked before
    holding, so someone off the team can't hold up the team's work."""
    get_or_404(session, Engagement, engagement_id, user)
    auth.hold(session, key)
    session.expire_all()  # it may have changed before we held the guard
    return get_or_404(session, Engagement, engagement_id, user)


def get_client_or_404(session: Session, user: User, client_id: int) -> Client:
    """A client the user may see: one with an engagement they're on, or with none yet (so
    whoever adds a client can add its first engagement)."""
    client = session.get(Client, client_id)
    if client is None or (client.engagements
                          and not visible_engagements(session, user, client.engagements)):
        raise HTTPException(404, f"Client {client_id} not found.")
    return client


def hold_for_delete(session: Session, engagement_ids: list[int]) -> None:
    """Hold the engagements' document requests (portal.py) and teams before deleting them, so
    a request, file or team member can't be added to an engagement as it goes."""
    for eid in engagement_ids:
        auth.hold(session, f"request-number:{eid}")
        auth.hold(session, f"requests:{eid}")
        auth.hold(session, f"team:{eid}")
    session.expire_all()


def request_files(engagements: list[Engagement]) -> list[dict]:
    """The documents sent for the engagements' requests, for the log of their deletion."""
    return [{"request": r.number, "filename": f.filename, "sha256": f.sha256}
            for e in engagements for r in e.document_requests for f in r.files]


def check_period(start: date | None, end: date) -> None:
    if start and start > end:
        raise HTTPException(400, "Period start must be on or before period end.")


def check_framework(framework: str) -> None:
    if framework not in FRAMEWORKS:
        raise HTTPException(400, f"Framework must be one of: {', '.join(FRAMEWORKS)}.")


# --- Materiality ------------------------------------------------------------

@router.get("/materiality/benchmarks")
def benchmarks() -> dict:
    return {
        "benchmarks": [{"id": k, **v} for k, v in materiality.BENCHMARKS.items()],
        "default_performance_pct": materiality.DEFAULT_PERFORMANCE_PCT,
        "default_trivial_pct": materiality.DEFAULT_TRIVIAL_PCT,
        "frameworks": FRAMEWORKS,
        "upload_kinds": [{"id": k, "label": v} for k, v in UPLOAD_KINDS.items()],
    }


def calc(body: MaterialityIn) -> materiality.Materiality:
    try:
        return materiality.calculate(body.benchmark, body.benchmark_amount, body.benchmark_pct,
                                     body.performance_pct, body.trivial_pct)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/materiality/calculate")
def calculate(body: MaterialityIn) -> dict:
    return calc(body).to_dict()


# --- Clients ----------------------------------------------------------------

@router.get("/clients")
def list_clients(user: User = Depends(current_user),
                 session: Session = Depends(get_session)) -> list[dict]:
    out, assigned = [], assigned_ids(session, user)
    for c in session.scalars(select(Client).order_by(Client.name)):
        engagements = visible_engagements(session, user, c.engagements, assigned)
        if engagements or not c.engagements:
            out.append(client_out(c, engagements))
    return out


@router.post("/clients", status_code=201)
def create_client(body: ClientIn, user: User = Depends(require("review")),
                  session: Session = Depends(get_session)) -> dict:
    client = Client(name=body.name.strip(), industry=body.industry, notes=body.notes)
    session.add(client)
    log(session, user, "client.create", f"Added client {client.name}")
    try:
        session.commit()
    except IntegrityError as exc:
        raise HTTPException(409, f"A client named {body.name!r} already exists.") from exc
    return client_out(client, [])


@router.get("/clients/{client_id}")
def get_client(client_id: int, user: User = Depends(current_user),
               session: Session = Depends(get_session)) -> dict:
    client = get_client_or_404(session, user, client_id)
    return client_out(client, visible_engagements(session, user, client.engagements))


@router.patch("/clients/{client_id}")
def update_client(client_id: int, body: ClientPatch, user: User = Depends(require("review")),
                  session: Session = Depends(get_session)) -> dict:
    client = get_client_or_404(session, user, client_id)
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    for key, value in changes.items():
        setattr(client, key, value.strip() if key == "name" else value)
    log(session, user, "client.update", f"Edited client {client.name}", details=changes)
    try:
        session.commit()
    except IntegrityError as exc:
        raise HTTPException(409, f"A client named {body.name!r} already exists.") from exc
    return client_out(client, visible_engagements(session, user, client.engagements))


@router.delete("/clients/{client_id}", status_code=204)
def delete_client(client_id: int, user: User = Depends(require("manage")),
                  session: Session = Depends(get_session)) -> Response:
    def deletable() -> Client:
        client = get_client_or_404(session, user, client_id)
        if len(visible_engagements(session, user, client.engagements)) < len(client.engagements):
            raise HTTPException(403, "This client has engagements you aren't assigned to, so an "
                                     "admin has to delete it.")
        return client

    deletable()  # before holding anything, then again in case it changed meanwhile
    ids = sorted(session.scalars(select(Engagement.id).where(Engagement.client_id == client_id)))
    hold_for_delete(session, ids)
    client = deletable()
    refuse_locked(client.engagements)
    log(session, user, "client.delete",
        f"Deleted client {client.name} and its {len(client.engagements)} engagements",
        details={"request_files": request_files(client.engagements)})
    session.delete(client)
    session.commit()
    return Response(status_code=204)


# --- Engagements ------------------------------------------------------------

@router.post("/clients/{client_id}/engagements", status_code=201)
def create_engagement(client_id: int, body: EngagementIn, user: User = Depends(require("review")),
                      session: Session = Depends(get_session)) -> dict:
    client = get_client_or_404(session, user, client_id)
    check_period(body.period_start, body.period_end)
    check_framework(body.framework)
    engagement = Engagement(client=client, name=body.name.strip(), period_end=body.period_end,
                            period_start=body.period_start, framework=body.framework)
    if not auth.sees_all_engagements(user):
        engagement.team.append(EngagementMember(user_id=user.id))  # or they'd lose it at once
    session.add(engagement)
    session.flush()
    log(session, user, "engagement.create",
        f"Created engagement {engagement.name} (period end {body.period_end})", engagement)
    session.commit()
    return engagement_out(engagement)


@router.get("/engagements/{engagement_id}")
def get_engagement(engagement_id: int, user: User = Depends(current_user),
                   session: Session = Depends(get_session)) -> dict:
    return engagement_out(get_or_404(session, Engagement, engagement_id, user))


@router.patch("/engagements/{engagement_id}")
def update_engagement(engagement_id: int, body: EngagementPatch,
                      user: User = Depends(require("review")),
                      session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id, user)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("framework") is not None:
        check_framework(changes["framework"])
    for key, value in changes.items():
        if value is not None or key == "period_start":
            setattr(e, key, value)
    check_period(e.period_start, e.period_end)
    log(session, user, "engagement.update", "Edited engagement details", e,
        details={k: str(v) for k, v in changes.items()})
    session.commit()
    return engagement_out(e)


@router.delete("/engagements/{engagement_id}", status_code=204)
def delete_engagement(engagement_id: int, user: User = Depends(require("manage")),
                      session: Session = Depends(get_session)) -> Response:
    get_or_404(session, Engagement, engagement_id, user)  # before holding anything
    hold_for_delete(session, [engagement_id])
    e = get_or_404(session, Engagement, engagement_id, user)
    refuse_locked([e])
    log(session, user, "engagement.delete", f"Deleted engagement {e.name} with "
        f"{len(e.uploads)} files and {len(e.findings)} findings", e,
        details={"request_files": request_files([e])})
    session.delete(e)
    session.commit()
    return Response(status_code=204)


@router.put("/engagements/{engagement_id}/team")
def set_team(engagement_id: int, body: TeamIn, user: User = Depends(require("admin")),
             session: Session = Depends(get_session)) -> dict:
    """Choose who on the audit team works on the engagement (and so can see it). Admins see
    every engagement, so they aren't added; one who joined before becoming an admin stays on,
    and keeps the engagement if they stop being an admin."""
    auth.hold(session, "admins")  # so nobody becomes a client user while joining the team
    auth.hold(session, f"team:{engagement_id}")  # two admins editing at once take turns
    session.expire_all()
    e = get_or_404(session, Engagement, engagement_id, user)
    wanted = set(body.user_ids)
    users = {u.id: u for u in session.scalars(select(User).where(User.id.in_(wanted)))}
    if wanted - users.keys():
        raise HTTPException(400, f"User {min(wanted - users.keys())} not found.")
    if any(u.role == "client" for u in users.values()):
        raise HTTPException(400, "Client users can't join the audit team; they see their "
                                 "client's requests in the portal.")
    current = {m.user_id: m for m in e.team}
    if any(u.role == "admin" and uid not in current for uid, u in users.items()):
        raise HTTPException(400, "Admins already see every engagement, so they aren't added "
                                 "to teams.")
    kept_admins = {uid for uid, m in current.items() if m.user.role == "admin"}
    added = sorted(wanted - current.keys())
    removed = sorted(current.keys() - wanted - kept_admins)
    removed_names = [current[uid].user.name for uid in removed]
    for uid in removed:
        e.team.remove(current[uid])
    for uid in added:
        e.team.append(EngagementMember(user_id=uid))
    if added or removed:
        parts = ([f"added {', '.join(users[uid].name for uid in added)}"] if added else []) + (
            [f"removed {', '.join(removed_names)}"] if removed else [])
        log(session, user, "engagement.team", f"Changed the team: {'; '.join(parts)}", e,
            details={"added": added, "removed": removed})
    session.commit()
    session.refresh(e)
    return {"team": team_out(e)}


@router.put("/engagements/{engagement_id}/materiality")
def set_materiality(engagement_id: int, body: MaterialityIn,
                    user: User = Depends(require("manage")),
                    session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id, user)
    m = calc(body)
    for key, value in m.to_dict().items():
        setattr(e, key, value)
    e.materiality_rationale = body.rationale
    log(session, user, "materiality.set",
        f"Set overall materiality {m.overall_materiality:,.2f}, performance "
        f"{m.performance_materiality:,.2f}", e,
        details={**m.to_dict(), "rationale": body.rationale})
    session.commit()
    return engagement_out(e)


# --- Uploads and runs -------------------------------------------------------

@router.post("/engagements/{engagement_id}/uploads", status_code=201)
async def create_upload(
    engagement_id: int,
    file: UploadFile = File(...),
    kind: str = Form("gl"),
    sheet: str = Form(""),
    mapping: str = Form(""),
    user: User = Depends(require("prepare")),
    session: Session = Depends(get_session),
) -> dict:
    e = get_or_404(session, Engagement, engagement_id, user)
    if kind not in UPLOAD_KINDS:
        raise HTTPException(400, f"Kind must be one of: {', '.join(UPLOAD_KINDS)}.")
    content = await read_upload(file)
    filename = file.filename or "upload"
    try:
        if kind in LISTS:
            report, mapping_used = list_report(filename, content, sheet or None), None
        else:
            loaded = loader.load(filename, content, parse_mapping(mapping), sheet or None)
            report, mapping_used = loaded.report.to_dict(), loaded.report.mapping
    except loader.LoadError as exc:
        raise HTTPException(400, str(exc)) from exc
    upload = Upload(
        engagement=e, kind=kind, filename=filename, content=content, size=len(content),
        sha256=hashlib.sha256(content).hexdigest(), sheet=sheet or None,
        mapping=mapping_used, import_report=report,
    )
    session.add(upload)
    session.flush()
    log(session, user, "upload.create", f"Uploaded {filename} ({UPLOAD_KINDS[kind]})", e,
        details={"upload_id": upload.id, "filename": filename, "kind": kind,
                 "sha256": upload.sha256, "size": upload.size, "sheet": upload.sheet,
                 "mapping": upload.mapping, "rows_used": report["rows_used"]})
    session.commit()
    return upload_out(upload)


@router.delete("/uploads/{upload_id}", status_code=204)
def delete_upload(upload_id: int, user: User = Depends(require("review")),
                  session: Session = Depends(get_session)) -> Response:
    get_or_404(session, Upload, upload_id, user)
    # Taken in turn with saving sample results, so results can't land on a file being deleted.
    auth.hold(session, f"upload:{upload_id}")
    session.expire_all()
    upload = get_or_404(session, Upload, upload_id, user)
    findings = list(session.scalars(select(Finding).where(Finding.upload_id == upload_id)))
    worked = [f for f in findings if register.is_worked(f)]
    if worked:
        raise HTTPException(409, f"{len(worked)} findings from this file have been reviewed or "
                                 "have notes or evidence, so the file is kept as support.")
    samples = list(session.scalars(select(Sample).where(Sample.upload_id == upload_id)))
    tested = [x for x in samples if x.evaluation is not None]
    if tested:
        raise HTTPException(409, f"{len(tested)} samples from this file have test results "
                                 "recorded, so the file is kept as support.")
    affects = set()  # review sections whose work goes with the file (review.detail_effects)
    for f in findings:
        session.delete(f)
        affects.add("findings")
    for run in session.scalars(select(Run).where(Run.upload_id == upload_id)):
        session.delete(run)
    for x in samples:
        session.delete(x)
        affects.add("sampling")
    for c in session.scalars(select(Comparison).where(
            (Comparison.current_upload_id == upload_id) | (Comparison.prior_upload_id == upload_id))):
        session.delete(c)
        affects.add("comparison")
    for r in session.scalars(select(BankReconciliation).where(
            (BankReconciliation.bank_upload_id == upload_id)
            | (BankReconciliation.gl_upload_id == upload_id))):
        session.delete(r)
        affects.add("bank")
    for m in session.scalars(select(CrossMatch).where(
            (CrossMatch.left_upload_id == upload_id) | (CrossMatch.right_upload_id == upload_id))):
        session.delete(m)
        affects.add("comparison")
    if upload.kind in ("tb", "prior_tb"):
        affects.add("statements")
    for party in upload.engagement.related_parties:
        if party.scan_upload_id == upload_id:  # its counts and review rested on this ledger
            forget_scan(party)
            affects.add("related")
    count = upload.engagement.stock_count
    if count is not None and count.upload_id == upload_id:
        count.upload_id = None  # the items picked keep their figures; no more look-ups
    log(session, user, "upload.delete", f"Deleted {upload.filename}", upload.engagement,
        details={"upload_id": upload.id, "sha256": upload.sha256, "kind": upload.kind,
                 "affects": sorted(affects)})
    session.delete(upload)
    session.commit()
    return Response(status_code=204)


def forget_scan(party) -> None:
    """A related party's last ledger scan no longer counts: it needs scanning and reviewing
    again. Its conclusion is kept to work from."""
    party.scanned_at, party.matches, party.matched_amount = None, 0, 0.0
    party.scan_upload_id, party.scan_fingerprint = None, ""
    party.reviewed_by, party.reviewed_at = "", None


def list_report(filename: str, content: bytes, sheet: str | None) -> dict:
    """What a list file holds: its columns and how many rows."""
    raw = loader.read_file(filename, content, sheet).dropna(how="all")
    if raw.empty:
        raise loader.LoadError("The file has no rows.")
    return {"rows_used": len(raw), "columns": list(raw.columns), "warnings": []}


def load_upload(upload: Upload) -> loader.Loaded:
    if upload.kind in LISTS:
        where = {"fixed_assets": "Look at it under Fixed asset register.",
                 "inventory": "Use it under Inventory count."}.get(
            upload.kind, "Match it against another file under Match two files.")
        raise HTTPException(400, f"{upload.filename} is a list with no amounts to check. {where}")
    try:
        return loader.load(upload.filename, upload.content, upload.mapping, upload.sheet)
    except loader.LoadError as exc:
        raise HTTPException(400, str(exc)) from exc


def engagement_settings(e: Engagement, body: RunIn) -> dict:
    return {
        "period_end": e.period_end.isoformat(),
        "cutoff_days": body.cutoff_days,
        "round_threshold": body.round_threshold,
        "performance_materiality": e.performance_materiality,
        "trivial_threshold": e.trivial_threshold,
        "weekend": body.weekend,
        "keywords": list(checks.parse_keywords(body.keywords)),
    }


@router.post("/engagements/{engagement_id}/runs", status_code=201)
def create_run(engagement_id: int, body: RunIn, user: User = Depends(require("prepare")),
               session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id, user)
    upload = get_or_404(session, Upload, body.upload_id, user)
    if upload.engagement_id != e.id:
        raise HTTPException(400, "That upload belongs to another engagement.")
    if upload.kind == "bank":
        raise HTTPException(400, "The audit checks run on ledgers and trial balances. "
                                 "Reconcile a bank statement in the bank reconciliation panel.")
    loaded = load_upload(upload)
    settings = engagement_settings(e, body)
    template = templates.matching(session, body.template_id, settings)
    if template is not None:  # only when used as it is, so the report doesn't mislead
        settings["template"] = template.name
    result = checks.run_all(
        loaded.df,
        checks.Settings(**{**{k: v for k, v in settings.items() if k != "template"},
                           "period_end": e.period_end, "kind": upload.kind}),
        loaded.report.to_dict(),
    )
    # The report shows the engagement as it was when the checks ran, even if it's renamed.
    snapshot = {"client": e.client.name, "engagement": e.name, "framework": e.framework}
    run = Run(engagement=e, upload=upload, settings={**settings, "context": snapshot},
              result=result, summary=RunSummary(**RunSummary.of(result)))
    session.add(run)
    session.flush()
    added = register.sync_run_findings(session, run)
    sev = result["summary"]["findings_by_severity"]
    log(session, user, "run.create", f"Ran the audit checks on {upload.filename}: "
        f"{sev['high']} high, {sev['medium']} medium, {sev['low']} low "
        f"({added} new on the register)", e,
        details={"run_id": run.id, "upload_id": upload.id, "sha256": upload.sha256,
                 "settings": settings})
    session.commit()
    return {**run_brief(run), "result": result}


@router.get("/runs/{run_id}")
def get_run(run_id: int, user: User = Depends(current_user),
            session: Session = Depends(get_session)) -> dict:
    run = get_or_404(session, Run, run_id, user)
    return {**run_brief(run), "engagement_id": run.engagement_id, "result": run.result}


@router.get("/runs/{run_id}/report")
def run_report(run_id: int, format: str = "xlsx", user: User = Depends(current_user),
               session: Session = Depends(get_session)) -> Response:
    run = get_or_404(session, Run, run_id, user)
    e = run.engagement
    if format in ("csv", "xlsx"):
        log(session, user, "run.download", f"Downloaded run #{run.id} report ({format})", e,
            details={"run_id": run.id, "upload_id": run.upload_id,
                     "sha256": run.upload.sha256,
                     "settings": {k: v for k, v in (run.settings or {}).items() if k != "context"}})
        session.commit()
    if format == "csv":
        stem = safe_stem(f"{e.client.name}-{e.name}-run{run.id}")
        return Response(
            report.to_csv(run.result), media_type="text/csv",
            headers=attachment(f"{stem}-findings.csv"),
        )
    if format != "xlsx":
        raise HTTPException(400, "Format must be csv or xlsx.")
    name, content = run_workbook(run)
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=attachment(name),
    )


def run_workbook(run: Run) -> tuple[str, bytes]:
    """A run's audit report as an Excel file, and its name."""
    e = run.engagement
    then = (run.settings or {}).get("context", {})  # older runs fall back to the live names
    context = {"Client": then.get("client", e.client.name),
               "Engagement": then.get("engagement", e.name),
               "Period end": (run.settings or {}).get("period_end") or e.period_end.isoformat(),
               "Framework": then.get("framework", e.framework),
               "Run": f"#{run.id} at {run.created_at:%Y-%m-%d %H:%M} UTC",
               "File SHA-256": run.upload.sha256}
    if (run.settings or {}).get("template"):
        context["Firm test template"] = run.settings["template"]
    stem = safe_stem(f"{e.client.name}-{e.name}-run{run.id}")
    return (f"{stem}-audit-report.xlsx",
            report.to_excel(run.result, Path(run.upload.filename).name, context))
