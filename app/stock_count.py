"""Attending the client's inventory count (ISA 501).

The auditor uploads the client's inventory listing and says which columns hold the item code,
description, location, quantity, unit cost and value (guessed from the headers until then).
The app looks over the listing for odd lines, then picks the items to count from the books
to the floor: the largest by value, and a seeded random selection from the rest, so the same
seed picks the same items again. On the floor the auditor records what they counted, and
adds items they picked there to trace back to the books, which the app looks up in the
listing.

Differences are valued at the unit cost. Those on the largest items are taken as they are;
the rate found on the random items is applied to the rest of the listing, and the total is
compared with performance materiality. A reviewer signs the count off once every item is
counted, every difference explained and a conclusion written; any later change takes the
sign-off away.
"""

from __future__ import annotations

import io
import random
from datetime import date
from typing import Annotated

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.orm import Session

from . import auth, listings
from .auth import current_user, log, require
from .db import Engagement, StockCount, StockCountItem, Upload, User, get_session, utcnow
from .engagements import get_or_404, held_engagement, safe_stem
from .exports import attachment, finish_workbook

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
KINDS = ("inventory",)
FIELDS = {"item": "Item code", "description": "Description", "location": "Location",
          "quantity": "Quantity", "unit_cost": "Unit cost", "value": "Value"}
SYNONYMS = {
    "item": ["itemcode", "itemno", "itemnumber", "sku", "partno", "partnumber", "productcode",
             "stockcode", "code", "item", "itemid", "productid", "barcode"],
    "description": ["description", "itemdescription", "productname", "itemname", "name",
                    "product", "details"],
    "location": ["location", "warehouse", "store", "bin", "binlocation", "site", "branch",
                 "storelocation"],
    "quantity": ["quantity", "qty", "qtyonhand", "quantityonhand", "onhand", "stockqty",
                 "closingqty", "balanceqty", "units", "bookqty", "systemqty"],
    "unit_cost": ["unitcost", "cost", "costprice", "averagecost", "avgcost", "unitprice",
                  "price", "rate", "standardcost"],
    "value": ["value", "totalvalue", "stockvalue", "extendedcost", "totalcost", "amount",
              "closingvalue", "inventoryvalue", "bookvalue"],
}
DIRECTIONS = {"to_floor": "Books to floor", "to_book": "Floor to books"}
REASONS = {"largest": "Largest by value", "random": "Random", "floor": "Picked on the floor"}
STATUSES = {"to_count": "To count", "agreed": "Agreed", "difference": "Difference to explain",
            "explained": "Difference explained", "not_listed": "Not in the listing"}
OPEN = ("to_count", "difference", "not_listed")
MAX_PICK = 200
MAX_ITEMS = 1000
MAX_SHOWN = 100
MAX_QTY = 1e12
Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]
Long = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]
Column = Annotated[str, StringConstraints(max_length=255)]
Qty = Annotated[float, Field(ge=-MAX_QTY, le=MAX_QTY, allow_inf_nan=False)]
Cost = Annotated[float, Field(ge=0, le=MAX_QTY, allow_inf_nan=False)]


class ListingIn(BaseModel):
    upload_id: int
    columns: dict[str, Column] = Field(default_factory=dict, max_length=len(FIELDS))


class PickIn(ListingIn):
    largest: Annotated[int, Field(ge=0, le=MAX_PICK)] = 10
    random: Annotated[int, Field(ge=0, le=MAX_PICK)] = 15
    seed: Annotated[int, Field(ge=0, le=2**31 - 1)] | None = None


class CountPatch(BaseModel):
    count_date: date | None = None
    location: Short | None = None
    observations: Long | None = None
    conclusion: Long | None = None
    reviewed: bool | None = None


class FloorItemIn(BaseModel):
    item: Short = ""
    description: Short = ""
    location: Short = ""
    counted_qty: Qty | None = None
    note: Long = ""


class ItemPatch(BaseModel):
    """Only the fields sent change; the counted quantity can be cleared with null. The book
    quantity and unit cost can only be entered for an item picked on the floor."""
    counted_qty: Qty | None = None
    note: Long | None = None
    explanation: Long | None = None
    book_qty: Qty | None = None
    unit_cost: Cost | None = None


# --- The listing ----------------------------------------------------------------------

def money(v) -> float:
    return round(float(v), 2) + 0.0


class ColumnMissing(ValueError):
    pass


def frame(raw: pd.DataFrame, cols: dict[str, str]) -> pd.DataFrame:
    """One row per line of the listing, with its value worked out where it isn't given."""
    if "quantity" not in cols:
        raise ColumnMissing("Say which column holds each item's quantity.")
    if "unit_cost" not in cols and "value" not in cols:
        raise ColumnMissing("Say which column holds the unit cost or the value.")
    qty = listings.numbers(raw, cols.get("quantity"))
    cost = listings.numbers(raw, cols.get("unit_cost"))
    value = listings.numbers(raw, cols.get("value"))
    df = pd.DataFrame({
        "row": raw["__row"], "item": listings.text(raw, cols.get("item")),
        "description": listings.text(raw, cols.get("description")),
        "location": listings.text(raw, cols.get("location")),
        "quantity": qty, "unit_cost": cost, "given_value": value})
    df["value"] = value.where(value.notna(), qty * cost).fillna(0.0)
    # Without a unit cost, the value per unit; a nil quantity has none.
    df["cost_used"] = cost.where(cost.notna(), value / qty.where(qty != 0))
    return df


def checks(df: pd.DataFrame, cols: dict[str, str]) -> dict:
    """Lines worth a look before the count: they distort the selection or the valuation."""
    found: dict[str, list[int]] = {}

    def flag(key: str, mask: pd.Series) -> None:
        rows = [int(r) for r in df.loc[mask.fillna(False), "row"]]
        if rows:
            found[key] = rows

    flag("negative_quantity", df["quantity"] < 0)
    flag("no_quantity", df["quantity"].isna())
    if "unit_cost" in cols:
        flag("no_cost", df["unit_cost"].isna() | (df["unit_cost"] <= 0))
    if "unit_cost" in cols and "value" in cols:
        worked = df["quantity"] * df["unit_cost"]
        flag("value_differs", (df["given_value"] - worked).abs() > 1)
    if "item" in cols:
        key = df["item"].str.lower() + "\x00" + df["location"].str.lower()
        flag("duplicate", (df["item"] != "") & key.duplicated(keep=False))
    labels = {"negative_quantity": "Negative quantity", "no_quantity": "No quantity",
              "no_cost": "Unit cost nil or missing",
              "value_differs": "Value isn't quantity times unit cost",
              "duplicate": "The same item code more than once at a location"}
    return {"lines": int(sum(len(v) for v in found.values())),
            "items": [{"check": k, "label": labels[k], "count": len(v), "rows": v[:50]}
                      for k, v in found.items()]}


def read_listing(upload: Upload, columns: dict[str, str]) -> tuple[pd.DataFrame, dict,
                                                                   list[str]]:
    df, cols, file_columns, problem = look(upload, columns)
    if problem:
        raise HTTPException(400, problem)
    return df, cols, file_columns


def look(upload: Upload, columns: dict[str, str]) -> tuple[pd.DataFrame | None, dict,
                                                           list[str], str | None]:
    """The listing read with the columns picked, or the reason it can't be yet."""
    raw = listings.read(upload, KINDS, "an inventory listing")
    cols = listings.columns_for(raw, columns, SYNONYMS)
    file_columns = [c for c in raw.columns if c != "__row"]
    try:
        return frame(raw, cols), cols, file_columns, None
    except ColumnMissing as exc:
        return None, cols, file_columns, str(exc)


def listing_out(upload: Upload, df: pd.DataFrame | None, cols: dict,
                file_columns: list[str]) -> dict:
    head = {"upload_id": upload.id, "filename": upload.filename, "file_columns": file_columns,
            "fields": [{"id": k, "label": v} for k, v in FIELDS.items()], "columns": cols,
            "problem": None}
    if df is None:
        return head
    return {**head, "lines": len(df), "value": money(df["value"].sum()),
            "checks": checks(df, cols)}


def pick(df: pd.DataFrame, largest: int, how_many: int, seed: int) -> list[tuple[str, int]]:
    """(reason, index) for each line picked: the largest by value, then a random selection
    of the rest. Lines with no quantity can't be counted against anything, so aren't picked."""
    usable = df[df["quantity"].notna()]
    order = usable["value"].abs().sort_values(ascending=False, kind="stable")
    top = list(order.index[:largest])
    rest = sorted(set(usable.index) - set(top))
    chosen = sorted(random.Random(seed).sample(rest, min(how_many, len(rest))))
    return [("largest", int(i)) for i in top] + [("random", int(i)) for i in chosen]


# --- Working it out --------------------------------------------------------------------

def item_state(x: StockCountItem) -> dict:
    diff_qty = None if x.counted_qty is None or x.book_qty is None \
        else round(x.counted_qty - x.book_qty, 6)
    diff = None if diff_qty is None or x.unit_cost is None else money(diff_qty * x.unit_cost)
    if x.counted_qty is None:
        status = "to_count"
    elif x.direction == "to_book" and x.book_qty is None:
        status = "explained" if x.explanation.strip() else "not_listed"
    elif diff_qty == 0:
        status = "agreed"
    else:
        status = "explained" if x.explanation.strip() else "difference"
    book_value = None if x.book_qty is None or x.unit_cost is None \
        else money(x.book_qty * x.unit_cost)
    return {"difference_qty": diff_qty, "difference": diff, "book_value": book_value,
            "status": status}


def item_out(x: StockCountItem) -> dict:
    s = item_state(x)
    return {"id": x.id, "direction": x.direction, "direction_label": DIRECTIONS[x.direction],
            "reason": x.reason, "reason_label": REASONS.get(x.reason, ""),
            "source_row": x.source_row, "item": x.item, "description": x.description,
            "location": x.location, "unit_cost": x.unit_cost, "book_qty": x.book_qty,
            "counted_qty": x.counted_qty, "note": x.note, "explanation": x.explanation,
            "counted_by": x.counted_by,
            "counted_at": x.counted_at.isoformat() if x.counted_at else None,
            **s, "status_label": STATUSES[s["status"]], "open": s["status"] in OPEN}


def evaluate(c: StockCount, items: list[dict], e: Engagement) -> dict:
    """The differences found and what they suggest for the whole listing."""
    floor = [x for x in items if x["direction"] == "to_floor"]
    top = [x for x in floor if x["reason"] == "largest"]
    rand = [x for x in floor if x["reason"] == "random"]

    def total(xs, key):
        return money(sum(x[key] or 0 for x in xs))

    top_error, rand_error = total(top, "difference"), total(rand, "difference")
    top_value, rand_value = total(top, "book_value"), total(rand, "book_value")
    counted = all(x["counted_qty"] is not None for x in floor)
    rest = money(c.population_value - top_value)
    rate = rand_error / rand_value if rand_value else 0.0
    projected = money(top_error + rate * rest)
    pm = e.performance_materiality
    if not floor:
        outcome = "nothing_picked"
    elif not counted:
        outcome = "counting"
    elif pm is None:
        outcome = "no_materiality"
    else:
        outcome = "over" if abs(projected) > pm else "within"
    statuses = {k: sum(1 for x in items if x["status"] == k) for k in STATUSES}
    return {
        "items": len(items), "to_floor": len(floor),
        "to_book": len(items) - len(floor), "statuses": statuses,
        "open": sum(statuses[k] for k in OPEN),
        "tested_value": money(top_value + rand_value),
        "coverage_pct": round(100 * (top_value + rand_value) / c.population_value, 1)
        if c.population_value else None,
        "largest_error": top_error, "random_error": rand_error,
        "random_rate_pct": round(100 * rate, 2), "rest_value": rest,
        "projected": projected, "found": money(sum(x["difference"] or 0 for x in items)),
        "performance_materiality": pm, "outcome": outcome,
        "outcome_label": {"nothing_picked": "No items picked yet",
                          "counting": "Counting not finished",
                          "no_materiality": "Set performance materiality to compare with",
                          "within": "Within performance materiality",
                          "over": "Over performance materiality"}[outcome]}


def count_out(c: StockCount | None, e: Engagement) -> dict:
    if c is None:
        return {"count": None, "items": [], "summary": None}
    items = [item_out(x) for x in c.items]
    return {"count": {
        "id": c.id, "upload_id": c.upload_id, "filename": c.filename, "sha256": c.sha256,
        "columns": c.columns, "selection": c.selection,
        "population_value": c.population_value, "population_lines": c.population_lines,
        "listing_checks": c.listing_checks,
        "count_date": c.count_date.isoformat() if c.count_date else None,
        "location": c.location, "observations": c.observations, "conclusion": c.conclusion,
        "reviewed_by": c.reviewed_by,
        "reviewed_at": c.reviewed_at.isoformat() if c.reviewed_at else None,
        "created_by": c.created_by, "updated_by": c.updated_by,
        "updated_at": c.updated_at.isoformat()},
        "items": items, "summary": evaluate(c, items, e)}


def summary(e: Engagement) -> dict | None:
    """What the dashboard needs, or None without a count."""
    return count_out(e.stock_count, e)["summary"]


def unreview(session: Session, user: User, c: StockCount) -> None:
    """A change after the sign-off: it no longer stands."""
    if c.reviewed_at is not None:
        log(session, user, "stock.unreview", "The stock count changed after it was signed "
            "off, so the sign-off was taken away", c.engagement,
            details={"count_id": c.id, "was_reviewed_by": c.reviewed_by})
        c.reviewed_by, c.reviewed_at = "", None
    c.updated_by, c.updated_at = user.name, utcnow()


# --- Routes --------------------------------------------------------------------------

def held_count(session: Session, user: User, engagement_id: int) -> tuple[Engagement,
                                                                          StockCount]:
    e = held_engagement(session, user, engagement_id, f"stock:{engagement_id}")
    if e.stock_count is None:
        raise HTTPException(404, "Pick the items to count first.")
    return e, e.stock_count


def held_item(session: Session, user: User, item_id: int) -> StockCountItem:
    engagement_id = get_or_404(session, StockCountItem, item_id, user).engagement_id
    auth.hold(session, f"stock:{engagement_id}")
    session.expire_all()
    return get_or_404(session, StockCountItem, item_id, user)


@router.post("/uploads/{upload_id}/inventory")
def look_at_listing(upload_id: int, body: ListingIn, user: User = Depends(current_user),
                    session: Session = Depends(get_session)) -> dict:
    """The listing's columns, total and odd lines, before picking. Only reads."""
    upload = get_or_404(session, Upload, upload_id, user)
    df, cols, file_columns, problem = look(upload, body.columns)
    if problem:
        return {**listing_out(upload, None, cols, file_columns), "problem": problem}
    return listing_out(upload, df, cols, file_columns)


@router.get("/engagements/{engagement_id}/stock-count")
def get_count(engagement_id: int, user: User = Depends(current_user),
              session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id, user)
    return count_out(e.stock_count, e)


@router.post("/engagements/{engagement_id}/stock-count", status_code=201)
def pick_items(engagement_id: int, body: PickIn, user: User = Depends(require("prepare")),
               session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"stock:{engagement_id}")
    upload = get_or_404(session, Upload, body.upload_id, user)
    if upload.engagement_id != e.id:
        raise HTTPException(400, "Pick an inventory listing of this engagement.")
    c = e.stock_count
    if c is not None and any(x.counted_qty is not None for x in c.items):
        raise HTTPException(409, "Counts are recorded already. Start the count over to pick "
                                 "the items again.")
    df, cols, _ = read_listing(upload, body.columns)
    if body.largest + body.random == 0:
        raise HTTPException(400, "Pick at least one item.")
    seed = body.seed if body.seed is not None else random.SystemRandom().randrange(2**31)
    picked = pick(df, body.largest, body.random, seed)
    if not picked:
        raise HTTPException(400, "No line of the listing has a quantity to count.")
    if c is None:
        c = StockCount(engagement=e, created_by=user.name)
        session.add(c)
    else:
        c.items.clear()  # none counted yet, so nothing is lost
    c.upload_id, c.filename, c.sha256 = upload.id, upload.filename, upload.sha256
    c.columns = cols
    c.selection = {"largest": body.largest, "random": body.random, "seed": seed,
                   "picked": len(picked)}
    c.population_value, c.population_lines = money(df["value"].sum()), len(df)
    c.listing_checks = checks(df, cols)
    for reason, i in picked:
        r = df.loc[i]
        cost = r["cost_used"]
        c.items.append(StockCountItem(
            engagement_id=e.id, direction="to_floor", reason=reason, source_row=int(r["row"]),
            item=r["item"][:200], description=r["description"][:500],
            location=r["location"][:200],
            unit_cost=None if pd.isna(cost) else float(cost), book_qty=float(r["quantity"])))
    unreview(session, user, c)
    session.flush()
    log(session, user, "stock.pick", f"Picked {len(picked)} items to count from "
        f"{upload.filename}", e,
        details={"count_id": c.id, "upload_id": upload.id, "sha256": upload.sha256,
                 "columns": cols, **c.selection, "population_value": c.population_value,
                 "population_lines": c.population_lines})
    session.commit()
    session.refresh(c)
    return count_out(c, e)


@router.patch("/engagements/{engagement_id}/stock-count")
def update_count(engagement_id: int, body: CountPatch, user: User = Depends(require("prepare")),
                 session: Session = Depends(get_session)) -> dict:
    e, c = held_count(session, user, engagement_id)
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items()
               if v is not None or k == "count_date"}
    if "reviewed" in changes and not auth.can(user, "review"):
        raise HTTPException(403, "Only a reviewer can sign off the stock count.")
    edits = {k: v for k, v in changes.items() if k != "reviewed"}
    for field, v in edits.items():
        setattr(c, field, v)
    if edits:
        unreview(session, user, c)
    if changes.get("reviewed") is True:
        s = count_out(c, e)["summary"]
        if s["outcome"] in ("nothing_picked", "counting") or s["open"]:
            raise HTTPException(400, f"Finish the count first: {s['open']} items are still "
                                     "to count or explain.")
        if not c.count_date:
            raise HTTPException(400, "Enter the date of the count before signing it off.")
        if not c.conclusion.strip():
            raise HTTPException(400, "Write a conclusion before signing it off.")
        c.reviewed_by, c.reviewed_at = user.name, utcnow()
    elif changes.get("reviewed") is False:
        c.reviewed_by, c.reviewed_at = "", None
    log(session, user, "stock.update", "Changed the stock count", e,
        details={"count_id": c.id, "changed": sorted(changes),
                 "reviewed": bool(c.reviewed_at)})
    session.commit()
    return count_out(c, e)


@router.delete("/engagements/{engagement_id}/stock-count")
def delete_count(engagement_id: int, user: User = Depends(require("review")),
                 session: Session = Depends(get_session)) -> dict:
    e, c = held_count(session, user, engagement_id)
    log(session, user, "stock.delete", "Started the stock count over", e,
        details={"count_id": c.id, "items": len(c.items),
                 "counted": sum(1 for x in c.items if x.counted_qty is not None)})
    session.delete(c)
    session.commit()
    session.refresh(e)
    return count_out(None, e)


def find_in_listing(session: Session, c: StockCount, item: str,
                    location: str) -> dict | None:
    """The listing's line for an item picked on the floor: same code, and the same location
    when the listing has one for it. None when it isn't there or can't be read now."""
    upload = session.get(Upload, c.upload_id) if c.upload_id else None
    if upload is None or not item or "item" not in c.columns:
        return None
    try:
        df, _, _ = read_listing(upload, c.columns)
    except HTTPException:
        return None
    same = df[df["item"].str.lower() == item.lower()]
    if location and "location" in c.columns:
        here = same[same["location"].str.lower() == location.lower()]
        same = here if len(here) else same.iloc[0:0]
    if same.empty:
        return None
    qty = same["quantity"].sum()
    cost = same["cost_used"].dropna()
    r = same.iloc[0]
    return {"row": int(r["row"]), "description": r["description"],
            "book_qty": None if same["quantity"].isna().all() else float(qty),
            "unit_cost": float(cost.iloc[0]) if len(cost) else None}


@router.post("/engagements/{engagement_id}/stock-count/items", status_code=201)
def add_floor_item(engagement_id: int, body: FloorItemIn,
                   user: User = Depends(require("prepare")),
                   session: Session = Depends(get_session)) -> dict:
    e, c = held_count(session, user, engagement_id)
    if not (body.item or body.description):
        raise HTTPException(400, "Enter the item code or a description.")
    if len(c.items) >= MAX_ITEMS:
        raise HTTPException(400, f"A count can have at most {MAX_ITEMS} items.")
    found = find_in_listing(session, c, body.item, body.location)
    x = StockCountItem(engagement_id=e.id, direction="to_book", reason="floor",
                       item=body.item, description=body.description or
                       (found or {}).get("description", "")[:500], location=body.location,
                       note=body.note)
    if found:
        x.source_row, x.book_qty, x.unit_cost = found["row"], found["book_qty"], \
            found["unit_cost"]
    if body.counted_qty is not None:
        x.counted_qty, x.counted_by, x.counted_at = body.counted_qty, user.name, utcnow()
    c.items.append(x)
    unreview(session, user, c)
    session.flush()
    log(session, user, "stock.add", f"Added {x.item or x.description} picked on the floor", e,
        details={"count_id": c.id, "item_id": x.id, "item": x.item, "in_listing": bool(found),
                 "counted_qty": x.counted_qty})
    session.commit()
    return count_out(c, e)


@router.patch("/stock-count-items/{stock_item_id}")
def update_item(stock_item_id: int, body: ItemPatch, user: User = Depends(require("prepare")),
                session: Session = Depends(get_session)) -> dict:
    x = held_item(session, user, stock_item_id)
    c = x.count
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items()
               if v is not None or k in ("counted_qty", "book_qty", "unit_cost")}
    if {"book_qty", "unit_cost"} & set(changes) and x.direction != "to_book":
        raise HTTPException(400, "The book quantity and cost of an item picked from the "
                                 "listing come from the listing.")
    for field in ("note", "explanation", "book_qty", "unit_cost"):
        if field in changes:
            setattr(x, field, changes[field])
    if "counted_qty" in changes:
        x.counted_qty = changes["counted_qty"]
        x.counted_by, x.counted_at = (user.name, utcnow()) if x.counted_qty is not None \
            else ("", None)
    unreview(session, user, c)
    s = item_state(x)
    log(session, user, "stock.count", f"Recorded the count of {x.item or x.description}",
        c.engagement,
        details={"count_id": c.id, "item_id": x.id, "changed": sorted(changes),
                 "book_qty": x.book_qty, "counted_qty": x.counted_qty,
                 "difference": s["difference"], "status": s["status"]})
    session.commit()
    return count_out(c, c.engagement)


@router.delete("/stock-count-items/{stock_item_id}")
def delete_item(stock_item_id: int, user: User = Depends(require("prepare")),
                session: Session = Depends(get_session)) -> dict:
    x = held_item(session, user, stock_item_id)
    c = x.count
    if x.direction != "to_book":
        raise HTTPException(400, "Items picked from the listing stay in the count. Record "
                                 "what was found instead.")
    log(session, user, "stock.remove", f"Removed {x.item or x.description} from the count",
        c.engagement, details={"count_id": c.id, "item_id": x.id, "item": x.item})
    c.items.remove(x)
    unreview(session, user, c)
    session.commit()
    return count_out(c, c.engagement)


# --- Excel -----------------------------------------------------------------------------

def to_excel(e: Engagement, out: dict) -> bytes:
    c, s = out["count"], out["summary"]
    sel = c["selection"]
    about = [("Client", e.client.name), ("Engagement", e.name),
             ("Period end", e.period_end.isoformat()), ("Date of the count", c["count_date"]),
             ("Where", c["location"]),
             ("Inventory listing", f"{c['filename']} (SHA-256 {c['sha256']})"),
             *((f"Column for {FIELDS[k].lower()}", v) for k, v in c["columns"].items()),
             ("Lines in the listing", c["population_lines"]),
             ("Value of the listing", c["population_value"]),
             ("Largest items picked", sel.get("largest")),
             ("Random items picked", sel.get("random")),
             ("Random seed (picks the same items again)", sel.get("seed")),
             ("Value of the items picked", s["tested_value"]),
             ("Coverage %", s["coverage_pct"]),
             ("Differences on the largest items", s["largest_error"]),
             ("Differences on the random items", s["random_error"]),
             ("Rate on the random items %", s["random_rate_pct"]),
             ("Value of the rest of the listing", s["rest_value"]),
             ("Projected misstatement", s["projected"]),
             ("Performance materiality", s["performance_materiality"]),
             ("Outcome", s["outcome_label"]),
             ("How the count was run", c["observations"]), ("Conclusion", c["conclusion"]),
             ("Reviewed by", c["reviewed_by"]), ("Reviewed at", c["reviewed_at"])]
    columns = ["Ref", "Direction", "Why picked", "Listing row", "Item", "Description",
               "Location", "Unit cost", "Book quantity", "Counted quantity",
               "Difference (units)", "Difference (value)", "Status", "Note", "Explanation",
               "Counted by"]
    rows = [{"Ref": f"SC{n}", "Direction": x["direction_label"], "Why picked": x["reason_label"],
             "Listing row": x["source_row"], "Item": x["item"], "Description": x["description"],
             "Location": x["location"], "Unit cost": x["unit_cost"],
             "Book quantity": x["book_qty"], "Counted quantity": x["counted_qty"],
             "Difference (units)": x["difference_qty"], "Difference (value)": x["difference"],
             "Status": x["status_label"], "Note": x["note"], "Explanation": x["explanation"],
             "Counted by": x["counted_by"]} for n, x in enumerate(out["items"], 1)]
    lc = c["listing_checks"] or {}
    checked = [{"Check": i["label"], "Lines": i["count"],
                "Rows (first 50)": ", ".join(map(str, i["rows"]))} for i in lc.get("items", [])]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame(rows, columns=columns).to_excel(xl, sheet_name="Count", index=False)
        pd.DataFrame(checked, columns=["Check", "Lines", "Rows (first 50)"]).to_excel(
            xl, sheet_name="Listing checks", index=False)
        finish_workbook(xl.book)
    return buf.getvalue()


def workbook(e: Engagement) -> tuple[str, bytes]:
    stem = safe_stem(f"{e.client.name}-{e.name}-inventory-count")
    return f"{stem}.xlsx", to_excel(e, count_out(e.stock_count, e))


@router.get("/engagements/{engagement_id}/stock-count/export")
def export_count(engagement_id: int, user: User = Depends(current_user),
                 session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    if e.stock_count is None:
        raise HTTPException(404, "Pick the items to count first.")
    name, content = workbook(e)
    log(session, user, "stock.download", "Downloaded the inventory count workpaper", e,
        details={"count_id": e.stock_count.id, "items": len(e.stock_count.items)})
    session.commit()
    return Response(content, media_type=XLSX, headers=attachment(name))
