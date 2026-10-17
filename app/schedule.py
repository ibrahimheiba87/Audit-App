"""Staff scheduling across engagements, as in Caseware's and TeamMate+'s resource planning.

Reviewers, partners and admins book people from an engagement's team for a stretch of days
and a number of hours, spread evenly over the days. The firm's schedule shows everyone's
booked hours week by week, so the people booked for more than a full week stand out. Bookings
on engagements the viewer isn't on count toward the weekly hours but aren't
listed. Each engagement's bookings are set against its time budget. Every change is logged.
"""

from __future__ import annotations

import io
from datetime import date, timedelta
from typing import Annotated

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import auth
from .auth import current_user, log, require
from .db import Engagement, EngagementMember, StaffBooking, User, get_session
from .engagements import assigned_ids, get_or_404, held_engagement
from .exports import attachment, finish_workbook

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
WEEK_HOURS = 40.0  # a full week; more than this is over-booked
MAX_DAYS = 366
MAX_WEEKS = 26
Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]


class BookingIn(BaseModel):
    user_id: int
    starts_on: date
    ends_on: date
    hours: float = Field(gt=0, le=5000, allow_inf_nan=False)
    note: Note = ""


def days(b: StaffBooking) -> int:
    return (b.ends_on - b.starts_on).days + 1


def hours_between(b: StaffBooking, start: date, end: date) -> float:
    """The booking's hours that fall from `start` to `end`, both included."""
    lo, hi = max(b.starts_on, start), min(b.ends_on, end)
    if hi < lo:
        return 0.0
    return b.hours * ((hi - lo).days + 1) / days(b)


def booked_hours(e: Engagement) -> float:
    return round(sum(b.hours for b in e.bookings), 2)


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def booking_out(b: StaffBooking) -> dict:
    e = b.engagement
    return {"id": b.id, "user_id": b.user_id, "name": b.user.name,
            "starts_on": b.starts_on.isoformat(), "ends_on": b.ends_on.isoformat(),
            "hours": b.hours, "engagement_id": e.id, "label": f"{e.client.name} · {e.name}",
            "note": b.note, "created_by": b.created_by}


def week_load(session: Session, user_id: int, start: date, weeks: int) -> list[float]:
    end = start + timedelta(days=7 * weeks - 1)
    rows = session.scalars(select(StaffBooking).where(
        StaffBooking.user_id == user_id, StaffBooking.starts_on <= end,
        StaffBooking.ends_on >= start)).all()
    return [round(sum(hours_between(b, start + timedelta(days=7 * i),
                                    start + timedelta(days=7 * i + 6)) for b in rows), 1)
            for i in range(weeks)]


def over_weeks(session: Session, b: StaffBooking) -> list[dict]:
    """The weeks the booking's person is booked for more than a full week."""
    start = monday(b.starts_on)
    weeks = (monday(b.ends_on) - start).days // 7 + 1
    load = week_load(session, b.user_id, start, weeks)
    return [{"week": (start + timedelta(days=7 * i)).isoformat(), "hours": h}
            for i, h in enumerate(load) if h > WEEK_HOURS + 1e-9]


def engagement_state(session: Session, e: Engagement, user: User,
                     warnings: list[str] | None = None) -> dict:
    team = sorted((m.user for m in e.team if m.user.active and m.user.role != "client"),
                  key=lambda u: u.name.lower())
    return {"bookings": [booking_out(b) for b in e.bookings],
            "booked_hours": booked_hours(e),
            "budget_hours": round(sum(b.hours for b in e.time_budgets), 2),
            "team": [{"id": u.id, "name": u.name} for u in team],
            "can_book": auth.can(user, "review"), "warnings": warnings or []}


@router.get("/engagements/{engagement_id}/bookings")
def get_bookings(engagement_id: int, user: User = Depends(current_user),
                 session: Session = Depends(get_session)) -> dict:
    return engagement_state(session, get_or_404(session, Engagement, engagement_id, user), user)


@router.post("/engagements/{engagement_id}/bookings", status_code=201)
def add_booking(engagement_id: int, body: BookingIn, user: User = Depends(require("review")),
                session: Session = Depends(get_session)) -> dict:
    if body.ends_on < body.starts_on:
        raise HTTPException(400, "The booking ends before it starts.")
    if (body.ends_on - body.starts_on).days + 1 > MAX_DAYS:
        raise HTTPException(400, f"Book at most {MAX_DAYS} days at a time.")
    e = held_engagement(session, user, engagement_id, f"bookings:{engagement_id}")
    who = session.get(User, body.user_id)
    if who is None or who.role == "client" or not who.active \
            or session.get(EngagementMember, (e.id, who.id)) is None:
        raise HTTPException(400, "Book someone on the engagement's team. Add them to the "
                                 "team first.")
    if body.hours / ((body.ends_on - body.starts_on).days + 1) > 24:
        raise HTTPException(400, "That is more than 24 hours a day.")
    b = StaffBooking(engagement=e, user_id=who.id, starts_on=body.starts_on,
                     ends_on=body.ends_on, hours=round(body.hours, 2), note=body.note,
                     created_by=user.name)
    session.add(b)
    session.flush()
    log(session, user, "booking.create",
        f"Booked {who.name} for {b.hours:g} hours from {b.starts_on.isoformat()} to "
        f"{b.ends_on.isoformat()}", e,
        details={"booking_id": b.id, "user_id": who.id, "hours": b.hours,
                 "starts_on": b.starts_on.isoformat(), "ends_on": b.ends_on.isoformat()})
    session.commit()
    warnings = [f"{who.name} is now booked for {w['hours']:g} hours in the week of "
                f"{w['week']}, more than a full week of {WEEK_HOURS:g}."
                for w in over_weeks(session, b)]
    session.refresh(e)
    return engagement_state(session, e, user, warnings)


@router.delete("/staff-bookings/{staff_booking_id}")
def delete_booking(staff_booking_id: int, user: User = Depends(require("review")),
                   session: Session = Depends(get_session)) -> dict:
    b = get_or_404(session, StaffBooking, staff_booking_id, user)
    e = held_engagement(session, user, b.engagement_id, f"bookings:{b.engagement_id}")
    b = get_or_404(session, StaffBooking, staff_booking_id, user)
    log(session, user, "booking.delete",
        f"Removed {b.user.name}'s booking from {b.starts_on.isoformat()} to "
        f"{b.ends_on.isoformat()}", e,
        details={"booking_id": b.id, "user_id": b.user_id, "hours": b.hours})
    session.delete(b)
    session.commit()
    session.refresh(e)
    return engagement_state(session, e, user)


def firm_schedule(session: Session, user: User, start: date, weeks: int) -> dict:
    start = monday(start)
    end = start + timedelta(days=7 * weeks - 1)
    bookings = session.scalars(
        select(StaffBooking).where(StaffBooking.starts_on <= end, StaffBooking.ends_on >= start)
        .options(selectinload(StaffBooking.user), selectinload(StaffBooking.engagement)
                 .selectinload(Engagement.client))
        .order_by(StaffBooking.starts_on, StaffBooking.id)).all()
    assigned = assigned_ids(session, user)
    people = {u.id: u for u in session.scalars(
        select(User).where(User.role != "client", User.active.is_(True)))}
    people |= {b.user_id: b.user for b in bookings}
    week_starts = [start + timedelta(days=7 * i) for i in range(weeks)]
    rows = []
    for u in sorted(people.values(), key=lambda u: u.name.lower()):
        mine = [b for b in bookings if b.user_id == u.id]
        load = [round(sum(hours_between(b, w, w + timedelta(days=6)) for b in mine), 1)
                for w in week_starts]
        rows.append({"id": u.id, "name": u.name, "role": u.role,
                     "role_label": auth.ROLES[u.role], "active": u.active, "weeks": load,
                     "over": [h > WEEK_HOURS + 1e-9 for h in load],
                     "total": round(sum(load), 1),
                     # Bookings on engagements the viewer isn't on count in the weekly
                     # hours, but aren't listed.
                     "bookings": [booking_out(b) for b in mine
                                  if assigned is None or b.engagement_id in assigned],
                     "other_hours": round(sum(hours_between(b, start, end) for b in mine
                                              if assigned is not None
                                              and b.engagement_id not in assigned), 1)})
    return {"start": start.isoformat(), "end": end.isoformat(),
            "weeks": [w.isoformat() for w in week_starts], "week_hours": WEEK_HOURS,
            "people": rows, "over": sum(1 for r in rows if any(r["over"])),
            "can_book": auth.can(user, "review")}


@router.get("/schedule")
def get_schedule(start: date | None = None, weeks: int = Query(8, ge=1, le=MAX_WEEKS),
                 user: User = Depends(require("prepare")),
                 session: Session = Depends(get_session)) -> dict:
    return firm_schedule(session, user, start or date.today(), weeks)


@router.get("/schedule/export")
def export_schedule(start: date | None = None, weeks: int = Query(8, ge=1, le=MAX_WEEKS),
                    user: User = Depends(require("prepare")),
                    session: Session = Depends(get_session)) -> Response:
    s = firm_schedule(session, user, start or date.today(), weeks)
    log(session, user, "schedule.download", "Downloaded the staff schedule",
        details={"start": s["start"], "weeks": weeks})
    session.commit()
    grid = pd.DataFrame([{"Name": r["name"], "Role": r["role_label"],
                          **dict(zip([f"Week of {w}" for w in s["weeks"]], r["weeks"],
                                     strict=True)),
                          "Total": r["total"]} for r in s["people"]])
    listing = pd.DataFrame([{"Name": b["name"], "Engagement": b["label"],
                             "From": b["starts_on"], "To": b["ends_on"], "Hours": b["hours"],
                             "Note": b["note"]}
                            for r in s["people"] for b in r["bookings"]],
                           columns=["Name", "Engagement", "From", "To", "Hours", "Note"])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        grid.to_excel(xl, sheet_name="Hours by week", index=False)
        listing.to_excel(xl, sheet_name="Bookings", index=False)
        finish_workbook(xl.book)
    return Response(buf.getvalue(), media_type=XLSX,
                    headers=attachment(f"staff-schedule-{s['start']}.xlsx"))
