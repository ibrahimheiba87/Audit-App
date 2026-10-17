"""Review notes and sign-off by section of the engagement file.

Each section is signed off twice: by whoever prepared it and then by a different person who
reviewed it. A sign-off goes stale when the section changes afterwards, judged from the
activity log, so the reviewer can see what moved since they looked. Reviewers raise notes on
a section; the preparer answers and a reviewer clears them. A section can't be signed off as
reviewed while it has notes that aren't cleared, and completion needs every other section
reviewed first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from . import auth
from .auth import current_user, log, require
from .db import Activity, Engagement, ReviewNote, ReviewReply, SignOff, User, get_session, utcnow
from .engagements import get_or_404, held_engagement

router = APIRouter(prefix="/api")


@dataclass(frozen=True)
class Section:
    id: str
    label: str
    description: str
    actions: tuple[str, ...]  # activity log actions that change the section


# Actions that change a section as a side effect: a run adds findings to the register, and
# materiality is what proposed entries are measured against.
SIDE_EFFECTS = {
    "run.create": ("findings",),
    "materiality.set": ("statements",),
}
TRIAL_BALANCES = ("tb", "prior_tb")  # the uploads the statements are built from


def detail_effects(action: str, details: dict) -> tuple[str, ...]:
    """Side effects that depend on what the action touched, read from its log details."""
    if action == "upload.create":  # a new trial balance feeds the statements
        if details.get("kind") == "bank":  # a new statement is there to reconcile
            return ("bank",)
        return ("statements",) if details.get("kind") in TRIAL_BALANCES else ()
    # Deleting a file deletes its findings, samples, comparisons and bank reconciliations.
    # Older log entries don't say which there were, so they count as touching them all
    # (they predate bank reconciliations).
    return tuple(details.get("affects", ("comparison", "sampling", "findings", "statements")))


DETAIL_EFFECTS = ("upload.create", "upload.delete")


SECTIONS = [
    Section("acceptance", "Acceptance and independence",
            "The acceptance and continuance checklist, its decision, and the team's "
            "independence declarations.",
            ("acceptance.update", "acceptance.decide", "independence.declare")),
    Section("planning", "Planning and materiality", "Engagement details and materiality.",
            ("engagement.update", "materiality.set", "plan.update")),
    Section("risks", "Risks and audit program",
            "The risk register and the procedures planned and done in response.",
            ("risk.create", "risk.import", "risk.update", "risk.delete", "procedure.create",
             "procedure.update", "procedure.delete")),
    Section("controls", "Internal controls",
            "The controls register, the design of each control and the tests of the key ones.",
            ("control.create", "control.update", "control.design", "control.plan",
             "control.test", "control.delete", "control.carry")),
    Section("data", "Files and checks", "Uploaded files and the audit check runs.",
            ("upload.create", "upload.delete", "run.create")),
    Section("comparison", "Comparisons", "Comparisons with last year's balances, and files "
            "matched against each other.",
            ("comparison.create", "crossmatch.create", "crossmatch.delete")),
    Section("sampling", "Sampling", "Samples drawn for testing and their results.",
            ("sample.create", "sample.evaluate", "sample.vouch", "sample.unvouch")),
    Section("bank", "Bank reconciliation",
            "Bank statements matched to the cash account, and the reconciling items.",
            ("bankrec.create",)),
    Section("analytics", "Analytical procedures and proofs",
            "Expectations of statement lines and proofs in total, compared with the balances.",
            ("expectation.create", "expectation.update", "expectation.delete",
             "expectation.carry")),
    Section("estimates", "Leases, loans and estimates",
            "The leases recalculated under IFRS 16, the expected credit loss matrix for "
            "receivables, the loans and their covenants, and the accounting estimates.",
            ("lease.create", "lease.update", "lease.delete", "lease.carry", "ecl.setup",
             "loan.create", "loan.update", "loan.delete", "covenant.create",
             "covenant.update", "covenant.delete", "estimate.create", "estimate.update",
             "estimate.delete", "estimate.carry", "loan.carry")),
    Section("stock", "Inventory count",
            "Attendance at the inventory count: the items counted both ways and the "
            "differences found.",
            ("stock.pick", "stock.update", "stock.delete", "stock.add", "stock.count",
             "stock.remove")),
    Section("confirmations", "External confirmations",
            "Requests to banks, customers, suppliers and lawyers, and their replies.",
            ("confirmation.create", "confirmation.import", "confirmation.update",
             "confirmation.delete")),
    Section("related", "Related parties and documents",
            "The related parties register, the ledger scan for their names, and AI summaries "
            "of contracts and minutes.",
            ("related.create", "related.update", "related.delete", "related.scan",
             "related.carry",
             "summary.create", "summary.update", "summary.delete")),
    Section("prior", "Last year's points",
            "The points raised last year and how each was followed up this year.",
            ("prior.import", "prior.create", "prior.update", "prior.delete", "prior.finding",
             "prior.carry")),
    Section("findings", "Findings register", "Findings, their notes, evidence and status.",
            ("finding.create", "finding.delete", "finding.note", "finding.severity",
             "finding.status", "evidence.add", "evidence.remove", "finding.ask_client",
             "finding.withdraw_client", "finding.response", "finding.follow_up")),
    Section("statements", "Lead sheets and statements",
            "Account mapping, adjusting entries and the draft statements.",
            ("mapping.update", "entry.create", "entry.update", "entry.delete", "tick.add",
             "tick.delete", "translation.save", "translation.delete",
             "group.component_add", "group.component_update", "group.component_remove",
             "group.entry_add", "group.entry_delete", "vat.setup", "vat.return_add",
             "vat.return_delete")),
    Section("checklists", "Firm checklists",
            "The firm's own checklists added to this engagement, and their answers.",
            ("checklist.add", "checklist.update", "checklist.complete", "checklist.latest",
             "checklist.remove", "checklist.carry")),
    Section("disclosures", "Disclosure checklist",
            "The disclosures in the draft financial statements, checked item by item.",
            ("disclosure.update", "disclosure.accept_ai")),
    Section("finalisation", "Completion procedures and report",
            "Subsequent events, going concern, written representations, the final review and "
            "the draft auditor's report.",
            ("completion.update", "completion.event", "completion.signoff", "report.update")),
    Section("quality", "Engagement quality review",
            "Whether a quality review is needed, and the reviewer's checklist and sign-off.",
            ("quality.setup", "quality.update", "quality.signoff", "quality.unsign")),
    Section("completion", "Completion",
            "The overall sign-off, once every other section is reviewed with no open notes.",
            ()),
]
SECTION = {s.id: s for s in SECTIONS}
# Sections only some engagements have work in. Until one has (or is signed or noted), it
# isn't needed for completion, so engagements without a bank statement (or that keep their
# risk assessment, acceptance, independence, disclosure, controls, confirmation, analytics or
# inventory count records outside the app) aren't held up.
OPTIONAL = {"bank", "risks", "acceptance", "disclosures", "controls", "confirmations",
            "finalisation", "related", "analytics", "stock", "prior", "quality",
            "estimates", "checklists"}
WORK = [s for s in SECTIONS if s.id != "completion"]
SECTIONS_CHANGED: dict[str, set[str]] = {}  # action -> the sections it changes
for _s in WORK:
    for _a in _s.actions:
        SECTIONS_CHANGED.setdefault(_a, set()).add(_s.id)
for _a, _ids in SIDE_EFFECTS.items():
    SECTIONS_CHANGED.setdefault(_a, set()).update(_ids)

SectionId = Literal["acceptance", "planning", "risks", "controls", "data", "comparison",
                    "sampling", "bank", "analytics", "estimates", "stock", "confirmations",
                    "related", "prior", "findings",
                    "statements", "checklists", "disclosures", "finalisation", "quality", "completion"]
Kind = Literal["prepared", "reviewed"]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)]


class SignOffIn(BaseModel):
    section: SectionId
    kind: Kind


class NoteIn(BaseModel):
    section: SectionId
    text: Text


class ReplyIn(BaseModel):
    text: Text


class NotePatch(BaseModel):
    status: Literal["open", "cleared"]  # open also sends an answered note back


def iso(at: datetime | None) -> str | None:
    return at.isoformat() if at else None


def last_changes(session: Session, engagement_id: int) -> dict[str, datetime | None]:
    """When each section last changed, from the activity log."""
    rows = session.execute(
        select(Activity.action, func.max(Activity.at)).where(
            Activity.engagement_id == engagement_id,
            Activity.action.in_(list(SECTIONS_CHANGED))).group_by(Activity.action)).all()
    out: dict[str, datetime | None] = {s.id: None for s in WORK}

    def changed(sections, at):
        for sid in sections:
            if out[sid] is None or at > out[sid]:
                out[sid] = at

    for action, at in rows:
        changed(SECTIONS_CHANGED[action], at)
    for action, at, details in session.execute(  # uploads are few, so read each one
            select(Activity.action, Activity.at, Activity.details).where(
                Activity.engagement_id == engagement_id,
                Activity.action.in_(DETAIL_EFFECTS))):
        changed(detail_effects(action, details or {}), at)
    times = [t for t in out.values() if t]
    out["completion"] = max(times) if times else None
    return out


def section_state(e: Engagement, changes: dict[str, datetime | None]) -> list[dict]:
    signoffs = {(s.section, s.kind): s for s in e.signoffs}
    notes: dict[str, int] = {}
    for n in e.review_notes:
        if n.status != "cleared":
            notes[n.section] = notes.get(n.section, 0) + 1
    out: list[dict] = []
    for s in SECTIONS:
        changed = changes.get(s.id)
        prep, rev = signoffs.get((s.id, "prepared")), signoffs.get((s.id, "reviewed"))
        needed = not (s.id in OPTIONAL and changed is None and prep is None and rev is None
                      and not notes.get(s.id))
        prep_stale = bool(prep and changed and changed > prep.at)
        rev_stale = bool(rev and (prep is None or (changed and changed > rev.at)
                                  or prep.at > rev.at))
        if s.id == "completion":  # it stands only while every other section does
            # A section signed again after completion was (say, reviewed again after a note)
            # has changed since, even if it is back to done.
            others = [x for x in e.signoffs if x.section != "completion"]
            prepared_since = prep and any(x.kind == "prepared" and x.at > prep.at
                                          for x in others)
            signed_since = rev and any(x.at > rev.at for x in others)
            work = [x for x in out if x["needed"]]
            prep_stale = bool(prep and (prep_stale or prepared_since or any(
                not x["prepared"] or x["prepared"]["stale"] for x in work)))
            rev_stale = bool(rev and (rev_stale or signed_since or any(
                not x["done"] for x in work)))
        if prep is None:
            status = "Not started"
        elif prep_stale:
            status = "Changed since prepared"
        elif rev is None:
            status = "Ready for review"
        elif rev_stale:
            status = "Changed since review"
        else:
            status = "Reviewed"
        out.append({
            "id": s.id, "label": s.label, "description": s.description,
            "last_change_at": iso(changed), "open_notes": notes.get(s.id, 0), "status": status,
            "done": status == "Reviewed" and not notes.get(s.id), "needed": needed,
            "prepared": signoff_out(prep, prep_stale), "reviewed": signoff_out(rev, rev_stale),
        })
    return out


def signoff_out(s: SignOff | None, stale: bool) -> dict | None:
    if s is None:
        return None
    return {"user_id": s.user_id, "user_name": s.user_name, "at": iso(s.at), "stale": stale}


def note_out(n: ReviewNote) -> dict:
    return {"id": n.id, "section": n.section, "section_label": SECTION[n.section].label,
            "text": n.text, "status": n.status, "author_id": n.author_id, "author": n.author,
            "created_at": iso(n.created_at), "cleared_by": n.cleared_by,
            "cleared_at": iso(n.cleared_at),
            "replies": [{"id": r.id, "text": r.text, "author": r.author, "at": iso(r.at)}
                        for r in n.replies]}


def review_out(session: Session, e: Engagement) -> dict:
    sections = section_state(e, last_changes(session, e.id))
    notes = session.scalars(select(ReviewNote).where(ReviewNote.engagement_id == e.id)
                            .options(selectinload(ReviewNote.replies))
                            .order_by(ReviewNote.id.desc()))
    return {"sections": sections,
            "notes": [note_out(n) for n in notes],
            "open_notes": sum(s["open_notes"] for s in sections),
            "reviewed": sum(1 for s in sections if s["done"] and s["needed"]),
            "total": sum(1 for s in sections if s["needed"])}


@router.get("/engagements/{engagement_id}/review")
def get_review(engagement_id: int, user: User = Depends(current_user),
               session: Session = Depends(get_session)) -> dict:
    return review_out(session, get_or_404(session, Engagement, engagement_id, user))


def withdraw_review(session: Session, user: User, e: Engagement, section: str,
                    why: str) -> bool:
    """Take back the review sign-off on `section`, e.g. when a note is raised on it. Completion
    then shows as changed by itself, since it stands only while every section is reviewed."""
    rev = next((s for s in e.signoffs if s.section == section and s.kind == "reviewed"), None)
    if rev is None:
        return False
    e.signoffs.remove(rev)
    log(session, user, "signoff.remove",
        f"Took back the reviewed sign-off on {SECTION[section].label} ({why})", e,
        details={"section": section, "kinds": ["reviewed"], "signed_by": [rev.user_name]})
    return True


def permission_to_give(section: str, kind: str) -> str:
    if kind == "prepared":
        return "prepare"
    return "manage" if section == "completion" else "review"


def preparer_id(e: Engagement, section: str) -> int | None:
    prep = next((s for s in e.signoffs if s.section == section and s.kind == "prepared"), None)
    return prep.user_id if prep else None


@router.post("/engagements/{engagement_id}/signoffs", status_code=201)
def sign_off(engagement_id: int, body: SignOffIn, user: User = Depends(require("prepare")),
             session: Session = Depends(get_session)) -> dict:
    auth.ensure(user, permission_to_give(body.section, body.kind))
    # Notes and sign-offs change one at a time.
    e = held_engagement(session, user, engagement_id, f"review:{engagement_id}")
    section = SECTION[body.section]
    state = {s["id"]: s for s in section_state(e, last_changes(session, e.id))}
    mine = state[section.id]
    if body.section == "completion":
        if body.kind == "prepared":
            todo = [s["label"] for s in state.values() if s["id"] != "completion"
                    and s["needed"] and (not s["prepared"] or s["prepared"]["stale"])]
        else:
            todo = [s["label"] for s in state.values()
                    if s["id"] != "completion" and s["needed"] and not s["done"]]
        if todo:
            need = "prepared" if body.kind == "prepared" else "reviewed with no open notes"
            raise HTTPException(400, f"Sign off completion once every section is {need}. "
                                     f"Still to do: {', '.join(todo)}.")
    if body.kind == "reviewed":
        prep = mine["prepared"]
        if prep is None or prep["stale"]:
            raise HTTPException(400, f"{section.label} needs to be signed off as prepared "
                                     "(again, if it changed) before it can be reviewed.")
        if prep["user_id"] == user.id:
            raise HTTPException(400, "You prepared this section, so someone else needs to "
                                     "review it.")
        if mine["open_notes"]:
            raise HTTPException(400, f"{section.label} has {mine['open_notes']} review "
                                     f"note{'s' if mine['open_notes'] != 1 else ''} still to "
                                     "clear.")
    current = mine[body.kind]
    if current and not current["stale"] and current["user_id"] != user.id:
        # Otherwise whoever signed first could then review work they helped prepare.
        raise HTTPException(400, f"{current['user_name']} has already signed off "
                                 f"{section.label} as {body.kind}. They or a reviewer can take "
                                 "that back first.")
    existing = next((s for s in e.signoffs if s.section == section.id and s.kind == body.kind),
                    None)
    replaced = existing.user_name if existing else None
    if existing is None:
        e.signoffs.append(SignOff(section=section.id, kind=body.kind, user_id=user.id,
                                  user_name=user.name, at=utcnow()))
    else:
        existing.user_id, existing.user_name, existing.at = user.id, user.name, utcnow()
    log(session, user, "signoff.add", f"Signed off {section.label} as {body.kind}"
        + (" again" if replaced == user.name else ""), e,
        details={"section": section.id, "kind": body.kind, "replaced": replaced})
    session.commit()
    return review_out(session, e)


@router.delete("/engagements/{engagement_id}/signoffs/{section}/{kind}")
def remove_sign_off(engagement_id: int, section: SectionId, kind: Kind,
                    user: User = Depends(require("prepare")),
                    session: Session = Depends(get_session)) -> dict:
    """Take back a sign-off: your own, or someone else's if you could have given it yourself
    (and are at least a reviewer). Taking back the preparer's sign-off leaves the review
    showing as changed, since there is no longer prepared work for it to stand on."""
    e = held_engagement(session, user, engagement_id, f"review:{engagement_id}")
    target = next((s for s in e.signoffs if s.section == section and s.kind == kind), None)
    if target is None:
        raise HTTPException(404, "That section isn't signed off.")
    if target.user_id != user.id:
        auth.ensure(user, "review")
        auth.ensure(user, permission_to_give(section, kind))
    e.signoffs.remove(target)
    log(session, user, "signoff.remove", f"Took back the {kind} sign-off on "
        f"{SECTION[section].label}" + ("" if target.user_id == user.id
                                       else f" by {target.user_name}"), e,
        details={"section": section, "kinds": [kind], "signed_by": [target.user_name]})
    session.commit()
    return review_out(session, e)


@router.post("/engagements/{engagement_id}/review-notes", status_code=201)
def add_note(engagement_id: int, body: NoteIn, user: User = Depends(require("review")),
             session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"review:{engagement_id}")
    note = ReviewNote(section=body.section, text=body.text, author_id=user.id,
                      author=user.name)
    e.review_notes.append(note)
    withdrawn = withdraw_review(session, user, e, body.section, "a review note was raised")
    session.flush()
    label = SECTION[body.section].label
    log(session, user, "note.create", f"Raised a review note on {label}: {body.text[:200]}", e,
        details={"note_id": note.id, "section": body.section, "text": body.text,
                 "withdrew_review": withdrawn})
    session.commit()
    return note_out(note)


def note_and_engagement(session: Session, user: User, note_id: int
                        ) -> tuple[ReviewNote, Engagement]:
    engagement_id = get_or_404(session, ReviewNote, note_id, user).engagement_id
    auth.hold(session, f"review:{engagement_id}")
    session.expire_all()  # it may have changed before we held the guard
    note = get_or_404(session, ReviewNote, note_id, user)
    return note, note.engagement


@router.post("/review-notes/{note_id}/replies", status_code=201)
def reply_to_note(note_id: int, body: ReplyIn, user: User = Depends(require("prepare")),
                  session: Session = Depends(get_session)) -> dict:
    note, e = note_and_engagement(session, user, note_id)
    if note.status == "cleared":
        raise HTTPException(400, "This note is cleared. A reviewer can reopen it first.")
    note.replies.append(ReviewReply(text=body.text, author_id=user.id, author=user.name))
    # An answer comes from the section's preparer (or another preparer), not a fellow reviewer.
    if note.author_id != user.id and (user.id == preparer_id(e, note.section)
                                      or not auth.can(user, "review")):
        note.status = "responded"
    log(session, user, "note.reply",
        f"Replied to a review note on {SECTION[note.section].label}: {body.text[:200]}", e,
        details={"note_id": note.id, "text": body.text})
    session.commit()
    return note_out(note)


@router.patch("/review-notes/{note_id}")
def update_note(note_id: int, body: NotePatch, user: User = Depends(require("review")),
                session: Session = Depends(get_session)) -> dict:
    """Clear a note once its answer is satisfactory, reopen it, or send an answer back."""
    note, e = note_and_engagement(session, user, note_id)
    label = SECTION[note.section].label
    if body.status == "cleared" and note.status != "cleared":
        if user.id == preparer_id(e, note.section):
            raise HTTPException(400, f"You prepared {label}, so another reviewer needs to clear "
                                     "notes on it.")
        note.status, note.cleared_by, note.cleared_at = "cleared", user.name, utcnow()
        log(session, user, "note.clear", f"Cleared a review note on {label}", e,
            details={"note_id": note.id})
    elif body.status == "open" and note.status == "cleared":
        note.status, note.cleared_by, note.cleared_at = "open", "", None
        withdrawn = withdraw_review(session, user, e, note.section, "a review note was reopened")
        log(session, user, "note.reopen", f"Reopened a review note on {label}", e,
            details={"note_id": note.id, "withdrew_review": withdrawn})
    elif body.status == "open" and note.status == "responded":
        note.status = "open"
        log(session, user, "note.reopen", f"Sent back the answer to a review note on {label}", e,
            details={"note_id": note.id, "withdrew_review": False})
    session.commit()
    return note_out(note)


@router.delete("/review-notes/{note_id}", status_code=204)
def delete_note(note_id: int, user: User = Depends(require("review")),
                session: Session = Depends(get_session)) -> Response:
    """Delete a note raised by mistake: only its author, and only before anyone replies."""
    note, e = note_and_engagement(session, user, note_id)
    if note.author_id != user.id or note.replies:
        raise HTTPException(400, "Only the reviewer who raised a note can delete it, and only "
                                 "before anyone replies. Clear it instead.")
    log(session, user, "note.delete",
        f"Deleted a review note on {SECTION[note.section].label}: {note.text[:200]}", e,
        details={"note_id": note.id, "text": note.text})
    session.delete(note)
    session.commit()
    return Response(status_code=204)

