"""The work plan: tasks with an owner and a due date, the hours budgeted for each area of the
audit, and the hours the team actually worked.

Tasks belong to an area of the audit file (the review sections) and to someone on the team.
Everyone sees their own open tasks across engagements under "My tasks". Partners and
reviewers set the budget; each person logs their own hours, which the app sets against the
budget by area and by person. Every change is logged, and it all exports to Excel.
"""

from __future__ import annotations

import io
from datetime import date
from typing import Annotated, Literal

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import auth, review
from .auth import current_user, log, require
from .db import (
    Client,
    Engagement,
    EngagementLock,
    Task,
    TimeBudget,
    TimeEntry,
    User,
    get_session,
    utcnow,
)
from .engagements import get_or_404, held_engagement, safe_stem
from .exports import attachment, finish_workbook

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# The areas of the audit file, as the review sections name them, and anything else.
AREAS = {s.id: s.label for s in review.WORK} | {"completion": "Completion",
                                                 "other": "Other"}
AreaId = Literal[tuple(AREAS)]  # type: ignore[valid-type]
STATUSES = {"open": "To do", "in_progress": "In progress", "done": "Done"}
MAX_DAY_HOURS = 24.0
MAX_BUDGET_HOURS = 100000.0
MAX_ENTRIES_SHOWN = 300
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]


class TaskIn(BaseModel):
    title: Title
    area: AreaId = "other"
    assignee_id: int | None = None
    due_on: date | None = None
    note: Text = ""


class TaskPatch(BaseModel):
    """Only the fields sent change; the owner and due date can be cleared with null."""
    title: Title | None = None
    area: AreaId | None = None
    assignee_id: int | None = None
    due_on: date | None = None
    status: Literal["open", "in_progress", "done"] | None = None
    note: Text | None = None


class BudgetIn(BaseModel):
    hours: dict[AreaId, float] = Field(max_length=len(AREAS))


class TimeIn(BaseModel):
    worked_on: date
    area: AreaId = "other"
    hours: float = Field(gt=0, le=MAX_DAY_HOURS, allow_inf_nan=False)
    note: Text = ""
    task_id: int | None = None


def overdue(t: Task, today: date | None = None) -> bool:
    return t.status != "done" and t.due_on is not None and t.due_on < (today or date.today())


def task_out(t: Task) -> dict:
    return {"id": t.id, "engagement_id": t.engagement_id, "title": t.title, "area": t.area,
            "area_label": AREAS.get(t.area, t.area), "assignee_id": t.assignee_id,
            "assignee_name": t.assignee_name,
            "due_on": t.due_on.isoformat() if t.due_on else None, "status": t.status,
            "status_label": STATUSES[t.status], "overdue": overdue(t), "note": t.note,
            "created_by": t.created_by, "created_by_id": t.created_by_id,
            "created_at": t.created_at.isoformat(),
            "updated_by": t.updated_by, "updated_at": t.updated_at.isoformat(),
            "done_by": t.done_by, "done_at": t.done_at.isoformat() if t.done_at else None}


def entry_out(x: TimeEntry) -> dict:
    return {"id": x.id, "user_id": x.user_id, "user_name": x.user_name,
            "worked_on": x.worked_on.isoformat(), "area": x.area,
            "area_label": AREAS.get(x.area, x.area), "hours": x.hours, "note": x.note,
            "task_id": x.task_id, "created_at": x.created_at.isoformat()}


def assignable(session: Session, e: Engagement) -> list[User]:
    """Who a task can be given to: active staff on the engagement's team."""
    ids = [m.user_id for m in e.team]
    if not ids:
        return []
    users = session.scalars(select(User).where(User.id.in_(ids), User.active.is_(True),
                                               User.role != "client")).all()
    return sorted(users, key=lambda u: u.name.lower())


def check_assignee(session: Session, e: Engagement, assignee_id: int | None) -> User | None:
    if assignee_id is None:
        return None
    u = session.get(User, assignee_id)
    if u is None or not u.active or u.role == "client" \
            or not any(m.user_id == u.id for m in e.team):
        raise HTTPException(400, "Give the task to someone on the engagement's team.")
    return u


def budget_rows(e: Engagement) -> list[dict]:
    """Budget against hours worked for each area that has either."""
    budget = {b.area: b.hours for b in e.time_budgets}
    actual: dict[str, float] = {}
    for x in e.time_entries:
        actual[x.area] = actual.get(x.area, 0.0) + x.hours
    rows = []
    for area, label in AREAS.items():
        b, a = budget.get(area), round(actual.get(area, 0.0), 2)
        if b is None and not a:
            continue
        rows.append({"area": area, "label": label, "budget": b, "actual": a,
                     "remaining": round(b - a, 2) if b is not None else None,
                     "used_pct": round(a / b * 100, 1) if b else None,
                     "over": b is not None and a > b + 1e-9})
    return rows


def people_rows(e: Engagement) -> list[dict]:
    """Hours per person, by account, so two people with one name stay apart and a renamed
    person stays together (under their latest name)."""
    by: dict[int | str, list] = {}
    for x in sorted(e.time_entries, key=lambda x: x.id):
        row = by.setdefault(x.user_id or f"name:{x.user_name}", [x.user_name, 0.0])
        row[0], row[1] = x.user_name, row[1] + x.hours
    return [{"name": n, "hours": round(h, 2)} for n, h in sorted(by.values(), key=lambda i: -i[1])]


def owners(e: Engagement) -> set[int]:
    """Who can still own a task: active staff on the team (as assignable(), without a query)."""
    return {m.user_id for m in e.team if m.user.active and m.user.role != "client"}


def summary(e: Engagement) -> dict:
    """For the dashboard: tasks open and overdue, and hours against the budget."""
    today = date.today()
    budget = sum(b.hours for b in e.time_budgets)
    actual = round(sum(x.hours for x in e.time_entries), 2)
    return {"tasks": len(e.tasks), "open": sum(1 for t in e.tasks if t.status != "done"),
            "overdue": sum(1 for t in e.tasks if overdue(t, today)),
            # Tasks with no owner, or one who has since left the team or been deactivated.
            "unassigned": sum(1 for t in e.tasks if t.status != "done"
                              and t.assignee_id not in owners(e)),
            "budget": round(budget, 2) if e.time_budgets else None, "actual": actual,
            "over_areas": [r["label"] for r in budget_rows(e) if r["over"]]}


def state(session: Session, e: Engagement, user: User) -> dict:
    rows = budget_rows(e)
    entries = sorted(e.time_entries, key=lambda x: (x.worked_on, x.id), reverse=True)
    return {
        "tasks": [task_out(t) for t in e.tasks],
        "areas": [{"id": k, "label": v} for k, v in AREAS.items()],
        "statuses": [{"id": k, "label": v} for k, v in STATUSES.items()],
        "people": [{"id": u.id, "name": u.name} for u in assignable(session, e)],
        "budget": rows,
        "budget_total": round(sum(r["budget"] or 0 for r in rows), 2)
        if e.time_budgets else None,
        "actual_total": round(sum(x.hours for x in e.time_entries), 2),
        "by_person": people_rows(e),
        "entries": [entry_out(x) for x in entries[:MAX_ENTRIES_SHOWN]],
        "entries_total": len(entries),
        "my_hours": round(sum(x.hours for x in e.time_entries if x.user_id == user.id), 2),
        "summary": summary(e),
    }


# --- Tasks ----------------------------------------------------------------------------

@router.get("/engagements/{engagement_id}/workplan")
def get_workplan(engagement_id: int, user: User = Depends(current_user),
                 session: Session = Depends(get_session)) -> dict:
    return state(session, get_or_404(session, Engagement, engagement_id, user), user)


@router.post("/engagements/{engagement_id}/tasks")
def add_task(engagement_id: int, body: TaskIn, user: User = Depends(require("prepare")),
             session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"workplan:{engagement_id}")
    who = check_assignee(session, e, body.assignee_id)
    t = Task(engagement=e, title=body.title, area=body.area, due_on=body.due_on, note=body.note,
             assignee_id=who.id if who else None, assignee_name=who.name if who else "",
             created_by=user.name, created_by_id=user.id, updated_by=user.name)
    session.add(t)
    session.flush()
    log(session, user, "task.create", f"Added the task \"{t.title[:80]}\"", e,
        details={"task_id": t.id, "area": t.area, "assignee": t.assignee_name,
                 "due_on": body.due_on.isoformat() if body.due_on else None})
    session.commit()
    return state(session, e, user)


def held_task(session: Session, user: User, task_id: int) -> Task:
    t = get_or_404(session, Task, task_id, user)
    held_engagement(session, user, t.engagement_id, f"workplan:{t.engagement_id}")
    return get_or_404(session, Task, task_id, user)


@router.patch("/tasks/{task_id}")
def update_task(task_id: int, body: TaskPatch, user: User = Depends(require("prepare")),
                session: Session = Depends(get_session)) -> dict:
    t = held_task(session, user, task_id)
    e = t.engagement
    changes = body.model_dump(exclude_unset=True)
    changes = {k: v for k, v in changes.items() if v is not None or k in ("assignee_id",
                                                                           "due_on")}
    if "assignee_id" in changes and changes["assignee_id"] != t.assignee_id:
        who = check_assignee(session, e, changes["assignee_id"])
        t.assignee_name = who.name if who else ""
    was = t.status
    for field, value in changes.items():
        setattr(t, field, value)
    if t.status == "done" and was != "done":
        t.done_by, t.done_at = user.name, utcnow()
    elif t.status != "done":
        t.done_by, t.done_at = "", None
    t.updated_by, t.updated_at = user.name, utcnow()
    what = (f"Marked the task \"{t.title[:80]}\" as {STATUSES[t.status].lower()}"
            if t.status != was else f"Changed the task \"{t.title[:80]}\"")
    log(session, user, "task.update", what, e,
        details={"task_id": t.id, "changed": sorted(changes), "status": t.status,
                 "assignee": t.assignee_name,
                 "due_on": t.due_on.isoformat() if t.due_on else None})
    session.commit()
    return state(session, e, user)


@router.delete("/tasks/{task_id}")
def delete_task(task_id: int, user: User = Depends(require("prepare")),
                session: Session = Depends(get_session)) -> dict:
    t = held_task(session, user, task_id)
    if t.created_by_id != user.id and not auth.can(user, "review"):
        raise HTTPException(403, "Only whoever added the task, or a reviewer, can remove it.")
    e = t.engagement
    log(session, user, "task.delete", f"Removed the task \"{t.title[:80]}\"", e,
        details={"task_id": t.id, "status": t.status})
    session.delete(t)
    session.commit()
    session.refresh(e)
    return state(session, e, user)


@router.get("/my-tasks")
def my_tasks(user: User = Depends(current_user), session: Session = Depends(get_session)) -> dict:
    """The signed-in user's unfinished tasks on every engagement they can see, soonest due
    first."""
    rows = session.execute(
        select(Task, Engagement.name, Client.name)
        .join(Engagement, Task.engagement_id == Engagement.id)
        .join(Client, Engagement.client_id == Client.id)
        .outerjoin(EngagementLock, EngagementLock.engagement_id == Engagement.id)
        # A locked file's tasks can't be changed any more, so they aren't anyone's to do.
        .where(Task.assignee_id == user.id, Task.status != "done",
               EngagementLock.engagement_id.is_(None))).all()
    today = date.today()
    tasks = [{**task_out(t), "engagement_name": en, "client_name": cn} for t, en, cn in rows
             if auth.on_engagement(session, user, t.engagement_id)]
    tasks.sort(key=lambda t: (t["due_on"] is None, t["due_on"] or "", t["id"]))
    return {"tasks": tasks, "overdue": sum(1 for t in tasks if t["overdue"]),
            "due_today": sum(1 for t in tasks if t["due_on"] == today.isoformat())}


# --- Budget and time ----------------------------------------------------------------------

@router.put("/engagements/{engagement_id}/budget")
def set_budget(engagement_id: int, body: BudgetIn, user: User = Depends(require("review")),
               session: Session = Depends(get_session)) -> dict:
    """Set the budgeted hours for the areas sent; zero removes an area's budget."""
    for area, hours in body.hours.items():
        if not 0 <= hours <= MAX_BUDGET_HOURS or hours != hours:
            raise HTTPException(400, f"The budget for {AREAS[area]} must be between 0 and "
                                     f"{MAX_BUDGET_HOURS:,.0f} hours.")
    e = held_engagement(session, user, engagement_id, f"workplan:{engagement_id}")
    current = {b.area: b for b in e.time_budgets}
    changed = {}
    for area, hours in body.hours.items():
        hours = round(hours, 2)
        b = current.get(area)
        if hours == 0:
            if b is not None:
                session.delete(b)
                changed[area] = 0
        elif b is None:
            session.add(TimeBudget(engagement=e, area=area, hours=hours))
            changed[area] = hours
        elif b.hours != hours:
            b.hours = hours
            changed[area] = hours
    if changed:
        session.flush()
        session.expire(e, ["time_budgets"])
        log(session, user, "time.budget", "Changed the time budget", e,
            details={"hours": changed,
                     "total": round(sum(b.hours for b in e.time_budgets), 2)})
        session.commit()
    return state(session, e, user)


@router.post("/engagements/{engagement_id}/time")
def log_time(engagement_id: int, body: TimeIn, user: User = Depends(require("prepare")),
             session: Session = Depends(get_session)) -> dict:
    """Log the signed-in user's own hours."""
    if body.worked_on > date.today():
        raise HTTPException(400, "Hours can't be logged for a day that hasn't come yet.")
    e = held_engagement(session, user, engagement_id, f"workplan:{engagement_id}")
    auth.hold(session, f"time:{user.id}")  # the daily total spans engagements
    if body.task_id is not None and not any(t.id == body.task_id for t in e.tasks):
        raise HTTPException(400, "That task isn't on this engagement.")
    day = session.scalar(select(func.coalesce(func.sum(TimeEntry.hours), 0.0)).where(
        TimeEntry.user_id == user.id, TimeEntry.worked_on == body.worked_on)) or 0.0
    if day + body.hours > MAX_DAY_HOURS + 1e-9:
        raise HTTPException(400, f"That would be more than {MAX_DAY_HOURS:.0f} hours on "
                                 f"{body.worked_on.isoformat()} across your engagements.")
    x = TimeEntry(engagement=e, user_id=user.id, user_name=user.name,
                  worked_on=body.worked_on, area=body.area, hours=round(body.hours, 2),
                  note=body.note, task_id=body.task_id)
    session.add(x)
    session.flush()
    log(session, user, "time.log",
        f"Logged {x.hours:g} hours on {AREAS[x.area]} for {x.worked_on.isoformat()}", e,
        details={"entry_id": x.id, "hours": x.hours, "area": x.area, "task_id": x.task_id})
    session.commit()
    return state(session, e, user)


@router.delete("/time-entries/{time_entry_id}")
def delete_time(time_entry_id: int, user: User = Depends(require("prepare")),
                session: Session = Depends(get_session)) -> dict:
    x = get_or_404(session, TimeEntry, time_entry_id, user)
    e = held_engagement(session, user, x.engagement_id, f"workplan:{x.engagement_id}")
    x = get_or_404(session, TimeEntry, time_entry_id, user)
    if x.user_id != user.id and not auth.can(user, "review"):
        raise HTTPException(403, "Only whoever logged the hours, or a reviewer, can remove "
                                 "them.")
    log(session, user, "time.delete",
        f"Removed {x.hours:g} hours logged by {x.user_name} for {x.worked_on.isoformat()}", e,
        details={"entry_id": x.id, "hours": x.hours, "area": x.area, "user": x.user_name})
    session.delete(x)
    session.commit()
    session.refresh(e)
    return state(session, e, user)


# --- Excel ----------------------------------------------------------------------------

def workbook(session: Session, e: Engagement, user: User) -> tuple[str, bytes]:
    """The work plan as an Excel file, and its name."""
    s = state(session, e, user)
    about = [("Client", e.client.name), ("Engagement", e.name),
             ("Period end", e.period_end.isoformat()),
             ("Tasks", len(s["tasks"])), ("Tasks not done", s["summary"]["open"]),
             ("Tasks overdue", s["summary"]["overdue"]),
             ("Hours budgeted", s["budget_total"] if s["budget_total"] is not None
              else "No budget"),
             ("Hours worked", s["actual_total"])]
    tasks = [{"Task": t["title"], "Area": t["area_label"], "Owner": t["assignee_name"],
              "Due": t["due_on"] or "", "Status": t["status_label"],
              "Overdue": "Yes" if t["overdue"] else "", "Note": t["note"],
              "Done by": t["done_by"], "Added by": t["created_by"]} for t in s["tasks"]]
    budget = [{"Area": r["label"], "Budget (hours)": r["budget"], "Worked (hours)": r["actual"],
               "Remaining (hours)": r["remaining"], "Used %": r["used_pct"],
               "Over budget": "Yes" if r["over"] else ""} for r in s["budget"]]
    entries = [{"Date": x.worked_on.isoformat(), "Who": x.user_name,
                "Area": AREAS.get(x.area, x.area), "Hours": x.hours, "Note": x.note}
               for x in sorted(e.time_entries, key=lambda x: (x.worked_on, x.id))]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame(tasks, columns=["Task", "Area", "Owner", "Due", "Status", "Overdue",
                                     "Note", "Done by", "Added by"]).to_excel(
            xl, sheet_name="Tasks", index=False)
        pd.DataFrame(budget, columns=["Area", "Budget (hours)", "Worked (hours)",
                                      "Remaining (hours)", "Used %", "Over budget"]).to_excel(
            xl, sheet_name="Budget vs actual", index=False)
        pd.DataFrame(s["by_person"]).rename(columns={"name": "Who", "hours": "Hours"}).to_excel(
            xl, sheet_name="Hours by person", index=False)
        pd.DataFrame(entries, columns=["Date", "Who", "Area", "Hours", "Note"]).to_excel(
            xl, sheet_name="Time log", index=False)
        finish_workbook(xl.book)
    return safe_stem(f"{e.client.name}-{e.name}-work-plan") + ".xlsx", buf.getvalue()


@router.get("/engagements/{engagement_id}/workplan/export")
def export_workplan(engagement_id: int, user: User = Depends(current_user),
                    session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    name, content = workbook(session, e, user)
    log(session, user, "workplan.download", "Downloaded the work plan", e)
    session.commit()
    return Response(content, media_type=XLSX, headers=attachment(name))
