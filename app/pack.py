"""The whole audit file as one download: every workpaper the app can produce for an
engagement, in folders, with an index of what is in it, the sign-offs, review notes and
activity log, and (if asked for) the files the work was done on.

It's what gets kept when the engagement is finished, or handed to a reviewer or inspector.
"""

from __future__ import annotations

import io
import logging
import re
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import PurePosixPath

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from . import (
    acceptance,
    ai_writer,
    comparisons,
    confirmations,
    consolidation,
    controls,
    crossmatch,
    disclosures,
    ecl,
    estimates,
    expectations,
    findings,
    leadsheets,
    leases,
    loans,
    portal,
    prior_points,
    quality,
    reconciliations,
    related_parties,
    report_draft,
    responses,
    review,
    risks,
    samples,
    stock_count,
    summaries,
    vat,
    workplan,
)
from . import completion as completion_module
from . import planning as planning_module
from .auth import current_user, log
from .db import Activity, Engagement, Evidence, RequestFile, Upload, User, Vouching, get_session
from .engagements import get_or_404, run_workbook, safe_stem
from .exports import attachment, finish_workbook

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")
SPOOL = 32 * 1024 * 1024  # kept in memory up to this size, then on disk
CHUNK = 1024 * 1024

FOLDERS = {
    "planning": "1 Planning",
    "testing": "2 Testing",
    "completion": "3 Completion",
    "files": "4 Source files",
}


class Pack:
    """The zip being built, and the list of what went in or couldn't."""

    def __init__(self, session: Session, e: Engagement, out) -> None:
        self.session, self.engagement = session, e
        self.zip = zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED)
        self.contents: list[dict] = []
        self.skipped: list[dict] = []
        self.names: set[str] = set()

    def path(self, folder: str, name: str) -> str:
        """A unique path in the zip; names that clash get a number."""
        name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name).strip(" .") or "file"
        stem, suffix = PurePosixPath(name).stem, PurePosixPath(name).suffix
        path, n = f"{FOLDERS[folder]}/{name}", 1
        while path.casefold() in self.names:
            n += 1
            path = f"{FOLDERS[folder]}/{stem} ({n}){suffix}"
        self.names.add(path.casefold())
        return path

    def add(self, folder: str, name: str, content: bytes, what: str) -> None:
        path = self.path(folder, name)
        self.zip.writestr(path, content)
        self.contents.append({"Folder": FOLDERS[folder], "File": path.split("/", 1)[1],
                              "What it is": what, "Size (bytes)": len(content)})

    def workpaper(self, folder: str, what: str, build: Callable[[], tuple[str, bytes]],
                  *used: object) -> None:
        """Add one workpaper; if it can't be built, say why in the index instead."""
        try:
            name, content = build()
        except HTTPException as exc:
            self.skipped.append({"Workpaper": what, "Why it isn't included": str(exc.detail)})
            return
        except Exception:
            logger.exception("Couldn't build %s for the audit file pack", what)
            self.skipped.append({"Workpaper": what,
                                 "Why it isn't included": "Something went wrong building it."})
            return
        finally:
            self.release(*used, *self.engagement.uploads)
        self.add(folder, name, content, what)

    def release(self, *objects: object) -> None:
        """Let go of the large columns (results, file contents) a workpaper loaded, so a big
        engagement isn't held in memory all at once."""
        for o in objects:
            heavy = [a.key for a in inspect(o).mapper.column_attrs if a.deferred]
            if heavy:
                self.session.expire(o, heavy)


def build(session: Session, e: Engagement, user: User, with_files: bool, out) -> dict:
    """Write the pack for `e` into the file `out`. Returns what went into it."""
    pack = Pack(session, e, out)
    planning, testing, completion = "planning", "testing", "completion"
    if e.acceptance is not None or e.declarations:
        pack.workpaper(planning, "Acceptance, continuance and independence",
                       lambda: acceptance.workbook(session, e, user))
    if e.risks:
        pack.workpaper(planning, "Risk register and audit program", lambda: risks.workbook(e))
    if e.controls:
        pack.workpaper(planning, "Internal controls and tests of controls",
                       lambda: controls.workbook(e))
    if e.tasks or e.time_budgets or e.time_entries or e.bookings:
        pack.workpaper(planning, "Work plan, budget and time",
                       lambda: workplan.workbook(session, e, user))
    if e.prior_points:
        pack.workpaper(planning, "Follow-up of last year's points",
                       lambda: prior_points.workbook(e))
    for d in e.ai_drafts:
        pack.workpaper(planning, f"AI {ai_writer.KINDS[d.kind].lower()} #{d.id}",
                       lambda d=d: ai_writer.workbook(d))

    latest = {}
    for r in e.runs:  # in order, so the last one for each file wins
        latest[r.upload_id] = r
    for r in latest.values():
        pack.workpaper(testing, f"Journal entry checks on {r.upload.filename} (run #{r.id})",
                       lambda r=r: run_workbook(r), r)
    for c in e.comparisons:
        pack.workpaper(testing, f"Prior-year comparison #{c.id}", lambda c=c: comparisons.workbook(c))
    for x in e.samples:
        pack.workpaper(testing, f"Sample #{x.id} from {x.upload.filename}",
                       lambda x=x: samples.workbook(x))
    for r in e.bank_reconciliations:
        pack.workpaper(testing, f"Bank reconciliation #{r.id}",
                       lambda r=r: reconciliations.workbook(r))
    if e.expectations:
        pack.workpaper(testing, "Analytical procedures and proofs in total",
                       lambda: expectations.workbook(session, e))
    if e.leases:
        pack.workpaper(testing, "Leases (IFRS 16)", lambda: leases.workbook(e))
    if e.ecl_setup is not None:
        pack.workpaper(testing, "Expected credit losses (IFRS 9)",
                       lambda: ecl.workbook(session, e))
    if e.loans or e.covenants:
        pack.workpaper(testing, "Loans and covenants", lambda: loans.workbook(session, e))
    if e.estimates:
        pack.workpaper(testing, "Accounting estimates", lambda: estimates.workbook(e))
    if e.stock_count is not None:
        pack.workpaper(testing, "Inventory count", lambda: stock_count.workbook(e))
    if e.confirmations:
        pack.workpaper(testing, "External confirmations", lambda: confirmations.workbook(e))
    for m in e.cross_matches:
        pack.workpaper(testing, f"File match #{m.id}", lambda m=m: crossmatch.workbook(m), m)
    if e.related_parties:
        pack.workpaper(testing, "Related parties", lambda: related_parties.workbook(e))
    if e.document_summaries:
        pack.workpaper(testing, "AI summaries of contracts and minutes",
                       lambda: summaries.workbook(e))
    if e.findings:
        pack.workpaper(testing, "Findings register", lambda: findings.workbook(session, e))
        if any(f.response for f in e.findings):
            pack.workpaper(testing, "Management responses",
                           lambda: responses.workbook(session, e))
    if e.document_requests:
        pack.workpaper(testing, "Client requests", lambda: portal.workbook(e))

    data, tb, prior = None, None, None
    try:
        data, tb, prior = leadsheets.compute(session, e, None, None)
    except HTTPException as exc:
        pack.skipped.append({"Workpaper": "Lead sheets and draft statements",
                             "Why it isn't included": str(exc.detail)})
    except Exception:
        logger.exception("Couldn't work out the lead sheets for the audit file pack")
        pack.skipped.append({"Workpaper": "Lead sheets and draft statements",
                             "Why it isn't included": "Something went wrong building it."})
    finally:
        pack.release(*e.uploads)
    if tb is not None:
        pack.workpaper(completion, "Lead sheets and draft statements",
                       lambda: leadsheets.workbook(e, data, tb, prior))
    if e.components or e.consolidation_entries:
        pack.workpaper(completion, "Group consolidation",
                       lambda: consolidation.workbook(session, e))
    if e.vat_setup is not None or e.vat_returns:
        pack.workpaper(testing, "VAT reconciliation", lambda: vat.workbook(session, e))
    if e.disclosures is not None:
        pack.workpaper(completion, "Disclosure checklist", lambda: disclosures.workbook(e))
    if e.completion is not None or e.subsequent_events:
        pack.workpaper(completion, "Completion procedures",
                       lambda: completion_module.workbook(session, e))
    if e.quality_review is not None and e.quality_review.required:
        pack.workpaper(completion, "Engagement quality review", lambda: quality.workbook(e))
    if e.plan is not None:
        pack.workpaper(planning, "Engagement letter",
                       lambda: planning_module.word_file(e, "letter"))
        pack.workpaper(planning, "Planning memo", lambda: planning_module.word_file(e, "memo"))
    if e.audit_report is not None:
        pack.workpaper(completion, "Draft auditor's report",
                       lambda: report_draft.word_file(session, e))

    inventory = source_files(e)
    if with_files:
        for f in inventory:  # read one at a time, without keeping it in the session
            content = session.scalar(select(f["model"].content).where(f["model"].id == f["id"]))
            pack.add("files", f["name"], content or b"", f["what"])

    rows = index_rows(session, e, user, with_files, pack, inventory)
    name = safe_stem(f"{e.client.name}-{e.name}-index") + ".xlsx"
    pack.zip.writestr(name, index_workbook(rows))  # at the top, where it's found first
    pack.zip.close()
    return {"workpapers": sum(1 for c in pack.contents if c["Folder"] != FOLDERS["files"]),
            "files": len(inventory) if with_files else 0, "skipped": len(pack.skipped)}


def source_files(e: Engagement) -> list[dict]:
    """The files the work was done on: uploads, evidence on findings, and files from the
    client portal. Content is read only if they go in the pack."""
    out = []
    for u in e.uploads:
        out.append({"name": u.filename, "what": f"Uploaded {u.kind}", "size": u.size,
                    "sha256": u.sha256, "by": "", "at": u.uploaded_at,
                    "model": Upload, "id": u.id})
    for f in e.findings:
        for x in f.evidence:
            out.append({"name": x.filename,
                        "what": f"Evidence for finding #{f.id} ({f.check_name})",
                        "size": x.size, "sha256": x.sha256, "by": x.author, "at": x.uploaded_at,
                        "model": Evidence, "id": x.id})
    for s in e.samples:
        for v in s.vouchings:
            out.append({"name": v.filename,
                        "what": f"Vouched to row {v.row} of sample #{s.id}",
                        "size": v.size, "sha256": v.sha256, "by": v.created_by,
                        "at": v.created_at, "model": Vouching, "id": v.id})
    for r in e.document_requests:
        for x in r.files:
            out.append({"name": x.filename, "what": f"Sent for request {r.number}",
                        "size": x.size, "sha256": x.sha256,
                        "by": x.uploaded_by + (" (client)" if x.from_client else ""),
                        "at": x.uploaded_at, "model": RequestFile, "id": x.id})
    return out


def utc(at: datetime | None) -> str:
    return f"{at:%Y-%m-%d %H:%M}" if at else ""


def index_rows(session: Session, e: Engagement, user: User, with_files: bool, pack: Pack,
               inventory: list[dict]) -> dict[str, pd.DataFrame]:
    lock = e.lock
    about = [
        ("Client", e.client.name), ("Engagement", e.name),
        ("Period", f"{e.period_start.isoformat()} to {e.period_end.isoformat()}"
         if e.period_start else f"Ending {e.period_end.isoformat()}"),
        ("Framework", e.framework),
        ("Overall materiality", e.overall_materiality if e.overall_materiality is not None
         else "Not set"),
        ("Performance materiality", e.performance_materiality
         if e.performance_materiality is not None else "Not set"),
        ("Clearly trivial threshold", e.trivial_threshold
         if e.trivial_threshold is not None else "Not set"),
        ("Locked", f"By {lock.locked_by}, {utc(lock.locked_at)} UTC" if lock else "No"),
        ("Archived", f"By {lock.archived_by}, {utc(lock.archived_at)} UTC"
         if lock and lock.archived else "No"),
        ("Pack made by", user.name),
        ("Pack made at (UTC)", utc(datetime.now(UTC))),
        ("Source files included", "Yes" if with_files else "No (listed under Files)"),
    ]
    state = review.review_out(session, e)
    signoffs = [{
        "Section": s["label"], "Status": s["status"] if s["needed"] else "Not needed",
        "Prepared by": (s["prepared"] or {}).get("user_name", ""),
        "Prepared at (UTC)": ((s["prepared"] or {}).get("at") or "")[:16].replace("T", " "),
        "Reviewed by": (s["reviewed"] or {}).get("user_name", ""),
        "Reviewed at (UTC)": ((s["reviewed"] or {}).get("at") or "")[:16].replace("T", " "),
        "Open review notes": s["open_notes"],
    } for s in state["sections"]]
    notes = [{
        "Section": n["section_label"], "Note": n["text"], "Status": n["status"].capitalize(),
        "By": n["author"], "At (UTC)": (n["created_at"] or "")[:16].replace("T", " "),
        "Cleared by": n["cleared_by"] or "",
        "Cleared at (UTC)": (n["cleared_at"] or "")[:16].replace("T", " "),
        "Replies": "\n".join(f"{r['author']}: {r['text']}" for r in n["replies"]),
    } for n in state["notes"]]
    files = [{"File": f["name"], "What it is": f["what"], "Size (bytes)": f["size"],
              "SHA-256": f["sha256"], "Added by": f["by"], "Added (UTC)": utc(f["at"])}
             for f in inventory]
    activity = [{"When (UTC)": utc(a.at), "Who": a.user_name, "Role": a.user_role,
                 "Action": a.action, "What happened": a.summary}
                for a in session.scalars(select(Activity).where(
                    # older databases can reuse a deleted engagement's id
                    Activity.engagement_id == e.id, Activity.at >= e.created_at)
                    .order_by(Activity.id))]
    contents = [{"Folder": "", "File": "(this file)", "What it is": "Index", "Size (bytes)": ""},
                *pack.contents]
    frames = {
        "About": pd.DataFrame(about, columns=["Item", "Value"]),
        "Contents": pd.DataFrame(contents, columns=["Folder", "File", "What it is",
                                                    "Size (bytes)"]),
        "Sign-offs": pd.DataFrame(signoffs),
        "Review notes": pd.DataFrame(notes, columns=["Section", "Note", "Status", "By",
                                                     "At (UTC)", "Cleared by",
                                                     "Cleared at (UTC)", "Replies"]),
        "Files": pd.DataFrame(files, columns=["File", "What it is", "Size (bytes)", "SHA-256",
                                              "Added by", "Added (UTC)"]),
        "Activity": pd.DataFrame(activity, columns=["When (UTC)", "Who", "Role", "Action",
                                                    "What happened"]),
    }
    if pack.skipped:
        frames["Not included"] = pd.DataFrame(pack.skipped)
    return frames


def index_workbook(frames: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        for sheet, df in frames.items():
            df.to_excel(xl, sheet_name=sheet, index=False)
        finish_workbook(xl.book)
    return buf.getvalue()


def chunks(f) -> Iterator[bytes]:
    try:
        f.seek(0)
        while block := f.read(CHUNK):
            yield block
    finally:
        f.close()


@router.get("/engagements/{engagement_id}/pack")
def download_pack(engagement_id: int, files: bool = False, user: User = Depends(current_user),
                  session: Session = Depends(get_session)) -> StreamingResponse:
    """The whole audit file as a zip: workpapers by stage, an index, and (with files=true)
    the source files too."""
    e = get_or_404(session, Engagement, engagement_id, user)
    out = tempfile.SpooledTemporaryFile(max_size=SPOOL)
    try:
        made = build(session, e, user, files, out)
    except BaseException:
        out.close()
        raise
    what = "with source files" if files else "without source files"
    log(session, user, "pack.download", f"Downloaded the audit file pack ({what})", e,
        details={"source_files": files, **made})
    session.commit()
    name = safe_stem(f"{e.client.name}-{e.name}-audit-file") + ".zip"
    return StreamingResponse(chunks(out), media_type="application/zip",
                             headers=attachment(name))
