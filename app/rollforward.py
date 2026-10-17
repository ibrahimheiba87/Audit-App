"""Roll an engagement forward: start next year's file from this year's.

The new engagement gets the next period, and (each optional) the team, the account mapping,
the materiality method without its amounts, the client request list (open again, without due
dates), this year's adjusted trial balance as its prior-year trial balance, and the set-up
of later work (controls, related parties, the engagement letter terms, the VAT accounts, the
group's components that have next year's engagement already, and so on).
"""

from __future__ import annotations

import calendar
import csv
import hashlib
import io
import re
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from . import auth, leases, loader, prior_points
from .auth import log, require
from .db import (
    AccountingEstimate,
    AccountMapping,
    Control,
    Covenant,
    DocumentRequest,
    EclSetup,
    Engagement,
    EngagementChecklist,
    EngagementMember,
    EngagementPlan,
    Expectation,
    GroupComponent,
    Lease,
    Loan,
    QualityReview,
    RelatedParty,
    Task,
    TimeBudget,
    Upload,
    User,
    VatSetup,
    get_session,
)
from .engagements import engagement_out, get_or_404
from .leadsheets import compute

router = APIRouter(prefix="/api")


class RollForwardIn(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    team: bool = True
    mapping: bool = True
    materiality: bool = True
    requests: bool = True
    trial_balance: bool = True
    controls: bool = True
    workplan: bool = True
    related_parties: bool = True
    expectations: bool = True
    prior_points: bool = True
    quality: bool = True
    plan: bool = True
    vat: bool = True
    group: bool = True
    leases: bool = True
    ecl: bool = True
    loans: bool = True
    estimates: bool = True
    checklists: bool = True


# The names and periods of an expectation's inputs; the amounts and rates are this year's.
CARRIED_INPUTS = ("base_label", "units_label", "price_label", "months", "periods")


def next_year(d: date) -> date:
    """The same date a year on; a month end stays a month end (28 Feb 2027 -> 29 Feb 2028)."""
    last = calendar.monthrange(d.year, d.month)[1]
    new_last = calendar.monthrange(d.year + 1, d.month)[1]
    return date(d.year + 1, d.month, new_last if d.day == last else min(d.day, new_last))


def next_name(name: str) -> str:
    """"FY2026" -> "FY2027"; a name without a year gets "(rolled forward)"."""
    bumped, n = re.subn(r"(?<!\d)(19|20)(\d\d)(?!\d)", lambda m: str(int(m[0]) + 1), name)
    return bumped if n else f"{name[:183]} (rolled forward)"


def adjusted_tb_csv(data: dict) -> tuple[bytes, int]:
    """This year's adjusted trial balance as a CSV the loader reads back (debit positive)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Account", "Account Name", "Amount"])
    rows = 0
    for r in data["accounts"]:
        if r["in_tb"] or abs(r["adjusting"]) >= 0.005 or abs(r["reclassification"]) >= 0.005:
            w.writerow([r["account"], r["name"] or "", f"{r['adjusted']:.2f}"])
            rows += 1
    return buf.getvalue().encode("utf-8"), rows


@router.post("/engagements/{engagement_id}/roll-forward", status_code=201)
def roll_forward(engagement_id: int, body: RollForwardIn, user: User = Depends(require("review")),
                 session: Session = Depends(get_session)) -> dict:
    old = get_or_404(session, Engagement, engagement_id, user)
    name = (body.name or "").strip() or next_name(old.name)
    period_end = next_year(old.period_end)
    if any(x.name == name and x.period_end == period_end for x in old.client.engagements):
        raise HTTPException(409, f"This client already has an engagement called {name} "
                                 f"for the period ending {period_end}.")
    new = Engagement(client=old.client, name=name, period_end=period_end,
                     # The day after this period (which may have been a short first one).
                     period_start=old.period_end + timedelta(days=1) if old.period_start else None,
                     framework=old.framework)
    session.add(new)
    carried: dict[str, int | bool | str] = {}
    if body.team:
        # Admins see every engagement, so they aren't put on teams (see set_team).
        members = [m.user for m in old.team
                   if m.user.active and m.user.role not in ("client", "admin")]
        for u in members:
            new.team.append(EngagementMember(user_id=u.id))
        carried["team"] = len(members)
    if not auth.sees_all_engagements(user) and all(m.user_id != user.id for m in new.team):
        new.team.append(EngagementMember(user_id=user.id))  # or they'd lose it at once
    if body.mapping:
        for m in old.account_mappings:
            new.account_mappings.append(AccountMapping(account=m.account, line=m.line,
                                                       updated_by=user.name))
        carried["mapping"] = len(old.account_mappings)
    if body.materiality and old.benchmark:
        # The method, not the amounts: this year's benchmark amount is entered afresh.
        new.benchmark, new.benchmark_pct = old.benchmark, old.benchmark_pct
        new.performance_pct, new.trivial_pct = old.performance_pct, old.trivial_pct
        new.materiality_rationale = old.materiality_rationale
        carried["materiality"] = True
    if body.requests:
        for number, r in enumerate(old.document_requests, start=1):
            new.document_requests.append(DocumentRequest(
                number=number, title=r.title, details=r.details, status="open",
                created_by=user.name))
        carried["requests"] = len(old.document_requests)
    if body.controls and old.controls:
        # What each control is, not this year's assessment: design and tests are done afresh,
        # and the risks they addressed are last year's.
        for c in old.controls:
            new.controls.append(Control(
                area=c.area, process=c.process, title=c.title, description=c.description,
                kind=c.kind, nature=c.nature, frequency=c.frequency, key=c.key, owner=c.owner,
                source=f"rolled:{c.id}", created_by=user.name))
        carried["controls"] = len(old.controls)
    if body.workplan and (old.tasks or old.time_budgets):
        # The list of work and the hours planned for it; owners and dates are set afresh.
        for t in old.tasks:
            new.tasks.append(Task(title=t.title, area=t.area, note=t.note, created_by=user.name,
                                  created_by_id=user.id, updated_by=user.name))
        for b in old.time_budgets:
            new.time_budgets.append(TimeBudget(area=b.area, hours=b.hours))
        carried["tasks"] = len(old.tasks)
        carried["budget"] = round(sum(b.hours for b in old.time_budgets), 2)
    if body.related_parties and old.related_parties:
        # Who the related parties are, not this year's scan or conclusions.
        for p in old.related_parties:
            new.related_parties.append(RelatedParty(
                name=p.name, relationship_kind=p.relationship_kind, aliases=list(p.aliases or []),
                note=p.note, source=f"rolled:{p.id}", created_by=user.name,
                updated_by=user.name))
        carried["related_parties"] = len(old.related_parties)
    if body.expectations and old.expectations:
        # How each expectation is built and where its inputs come from; the figures, the
        # threshold and the conclusions are this year's to work out.
        for x in old.expectations:
            new.expectations.append(Expectation(
                kind=x.kind, title=x.title, line=x.line, method=x.method,
                inputs={k: v for k, v in (x.inputs or {}).items() if k in CARRIED_INPUTS},
                basis=x.basis, source=f"rolled:{x.id}", created_by=user.name,
                updated_by=user.name))
        carried["expectations"] = len(old.expectations)
    if body.prior_points:
        # Last year's management letter points and open findings, to follow up this year.
        added = prior_points.carry(new, old, user)
        if added:
            carried["prior_points"] = added
    q = old.quality_review
    if body.quality and q is not None and q.required:
        # Whether a quality review is needed, why and by whom; the checklist is this year's.
        new.quality_review = QualityReview(
            required=True, reason=q.reason, reason_note=q.reason_note, reviewer_id=q.reviewer_id,
            reviewer_name=q.reviewer_name, updated_by=user.name)
        carried["quality"] = True
    p = old.plan
    if body.plan and p is not None:
        # Who the letter is to and from and how the work is done; the dates, the fee and the
        # client's acceptance are next year's.
        new.plan = EngagementPlan(
            addressee=p.addressee, firm_name=p.firm_name, partner_name=p.partner_name,
            city=p.city, currency=p.currency, fee_basis=p.fee_basis,
            other_services=p.other_services, business=p.business, approach=p.approach,
            other_notes=p.other_notes, updated_by=user.name)
        carried["plan"] = True
    v = old.vat_setup
    if body.vat and v is not None:
        # The rate and the VAT accounts; next year's ledger is picked once it's uploaded.
        new.vat_setup = VatSetup(rate_pct=v.rate_pct, output_accounts=list(v.output_accounts),
                                 input_accounts=list(v.input_accounts), updated_by=user.name)
        carried["vat"] = True
    if body.group and old.components:
        # Each component whose own engagement for next year is already in the app, and on
        # whose team the user is (as when a component is added by hand).
        added = 0
        for c in old.components:
            after = next_year(c.component.period_end)
            successor = next((x for x in c.component.client.engagements
                              if x.period_end == after and x is not new and x.id is not None
                              and auth.on_engagement(session, user, x.id)), None)
            if successor is not None:
                new.components.append(GroupComponent(
                    component=successor, ownership_pct=c.ownership_pct, scope=c.scope,
                    note=c.note, created_by=user.name))
                added += 1
        carried["group_components"] = added
        carried["group_components_left"] = len(old.components) - added
    if body.leases and old.leases:
        # Leases still running next year; the client's figures are next year's to enter.
        running = [x for x in old.leases if leases.schedule(x)
                   and leases.schedule(x)[-1]["end"] > old.period_end + timedelta(days=1)]
        for x in running:
            new.leases.append(Lease(
                name=x.name, start_date=x.start_date, term_months=x.term_months,
                payment=x.payment, frequency=x.frequency, timing=x.timing,
                rate_pct=x.rate_pct, direct_costs=x.direct_costs, note=x.note,
                created_by=user.name, updated_by=user.name))
        if running:
            carried["leases"] = len(running)
    c = old.ecl_setup
    if body.ecl and c is not None:
        # The loss rates and terms; next year's listing and booked allowance are picked anew.
        new.ecl_setup = EclSetup(terms_days=c.terms_days, rates=dict(c.rates or {}),
                                 forward_pct=c.forward_pct, updated_by=user.name)
        carried["ecl"] = True
    if body.loans and (old.loans or old.covenants):
        # Each loan opens next year at this year's closing balance (the client's, when
        # entered); the movements and the client's figures are next year's.
        moved = {}
        for x in old.loans:
            closing = x.client_closing if x.client_closing is not None \
                else x.opening + x.drawn - x.repaid
            if x.maturity is not None and x.maturity <= old.period_end and abs(closing) < 0.005:
                continue  # repaid and done with
            y = Loan(name=x.name, lender=x.lender, opening=closing, rate_pct=x.rate_pct,
                     maturity=x.maturity, security=x.security, note=x.note,
                     created_by=user.name, updated_by=user.name)
            new.loans.append(y)
            moved[x.id] = y
        for c in old.covenants:
            if c.loan_id is not None and c.loan_id not in moved:
                continue  # its loan is repaid
            new.covenants.append(Covenant(
                title=c.title, loan=moved.get(c.loan_id), metric=c.metric,
                operator=c.operator, limit=c.limit, note=c.note, created_by=user.name))
        carried["loans"] = len(moved)
        carried["covenants"] = len(new.covenants)
    if body.estimates and old.estimates:
        # What each estimate is and how it's made; this year's amount becomes next year's
        # "last year's estimate", for the look back.
        for x in old.estimates:
            new.estimates.append(AccountingEstimate(
                title=x.title, line=x.line, management=0.0, method=x.method,
                assumptions=x.assumptions, uncertainty=x.uncertainty, approach=x.approach,
                prior_estimate=x.management, created_by=user.name, updated_by=user.name))
        carried["estimates"] = len(old.estimates)
    if body.checklists and old.checklists:
        # The questions as this year had them; the answers are next year's.
        for x in old.checklists:
            new.checklists.append(EngagementChecklist(
                source_id=x.source_id, name=x.name, items=list(x.items), answers={},
                created_by=user.name, updated_by=user.name))
        carried["checklists"] = len(old.checklists)
    session.flush()

    upload = None
    if body.trial_balance:
        try:
            data, tb, _ = compute(session, old, None, None)
        except HTTPException as exc:  # a trial balance the statements can't be built from
            data, tb = None, None
            carried["trial_balance_error"] = str(exc.detail)
        if tb is not None:
            content, rows = adjusted_tb_csv(data)
            filename = f"{old.name} adjusted trial balance.csv"
            loaded = loader.load(filename, content)
            upload = Upload(engagement=new, kind="prior_tb", filename=filename, content=content,
                            size=len(content), sha256=hashlib.sha256(content).hexdigest(),
                            mapping=loaded.report.mapping, import_report=loaded.report.to_dict())
            session.add(upload)
            session.flush()
            carried["trial_balance"] = rows
    log(session, user, "engagement.roll_forward",
        f"Rolled forward to {new.name} (period end {period_end})", old,
        details={"to_engagement_id": new.id, "carried": carried})
    log(session, user, "engagement.create",
        f"Created engagement {new.name} (period end {period_end}) by rolling forward "
        f"{old.name}", new, details={"from_engagement_id": old.id, "carried": carried})
    if carried.get("controls"):  # so the controls section needs a review this year too
        log(session, user, "control.carry",
            f"Carried {carried['controls']} controls from {old.name}", new,
            details={"from_engagement_id": old.id, "controls": carried['controls']})
    if carried.get("expectations"):  # and the analytics section
        log(session, user, "expectation.carry",
            f"Carried {carried['expectations']} expectations from {old.name}", new,
            details={"from_engagement_id": old.id, "expectations": carried["expectations"]})
    if carried.get("related_parties"):  # and the related parties section
        log(session, user, "related.carry",
            f"Carried {carried['related_parties']} related parties from {old.name}", new,
            details={"from_engagement_id": old.id, "parties": carried["related_parties"]})
    if carried.get("prior_points"):  # and the last year's points section
        log(session, user, "prior.carry",
            f"Carried {carried['prior_points']} points from {old.name} to follow up", new,
            details={"from_engagement_id": old.id, "points": carried["prior_points"]})
    if carried.get("quality"):  # and the quality review section
        log(session, user, "quality.setup",
            f"Carried the quality review set-up from {old.name}", new,
            details={"from_engagement_id": old.id, "reason": q.reason,
                     "reviewer": q.reviewer_name})
    if carried.get("leases"):  # and the leases and credit losses section
        log(session, user, "lease.carry",
            f"Carried {carried['leases']} leases from {old.name}", new,
            details={"from_engagement_id": old.id, "leases": carried["leases"]})
    if carried.get("loans") or carried.get("covenants"):  # and the estimates section
        log(session, user, "loan.carry",
            f"Carried {carried['loans']} loans and {carried['covenants']} covenants from "
            f"{old.name}", new,
            details={"from_engagement_id": old.id, "loans": carried["loans"],
                     "covenants": carried["covenants"]})
    if carried.get("checklists"):  # and the firm checklists section
        log(session, user, "checklist.carry",
            f"Carried {carried['checklists']} checklists from {old.name}", new,
            details={"from_engagement_id": old.id, "checklists": carried["checklists"]})
    if carried.get("estimates"):  # and the leases, loans and estimates section
        log(session, user, "estimate.carry",
            f"Carried {carried['estimates']} estimates from {old.name}", new,
            details={"from_engagement_id": old.id, "estimates": carried["estimates"]})
    if upload is not None:
        log(session, user, "upload.create",
            f"Uploaded {upload.filename} (Prior-year trial balance) from {old.name}", new,
            details={"upload_id": upload.id, "filename": upload.filename, "kind": "prior_tb",
                     "sha256": upload.sha256, "size": upload.size, "sheet": None,
                     "mapping": upload.mapping, "rows_used": carried["trial_balance"],
                     "from_engagement_id": old.id})
    session.commit()
    return {**engagement_out(new), "carried": carried}
