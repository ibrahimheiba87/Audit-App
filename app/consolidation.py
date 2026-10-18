"""Group audits and consolidation, as in Caseware Group and CCH: the group engagement adds the
statements of its components (other engagements in the app, such as subsidiaries) to its own,
with eliminations for balances and transactions inside the group.

- Each component is added in full, line by line, from its own mapped trial balance and
  adjusting entries. A component with a currency translation set up (translation.py) is
  added in the presentation currency, its translation reserve included.
- Eliminations are entered by statement line, as debits and credits that balance.
- When the group owns less than all of a component, the share of its profit and net assets
  belonging to non-controlling interests is shown below the statements.
- The group team sets each component's materiality and scope. A component above the group's
  materiality is flagged, and so is one that makes up 15% or more of the group's revenue or
  total assets (a significant component) but isn't in full scope.

Adding a component needs access to its engagement; after that, everyone on the group's team
sees its figures (only the statement lines, not its files). Everything is logged.
"""

from __future__ import annotations

import io
from typing import Annotated, Literal

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.orm import Session

from . import auth, statements, translation
from .auth import current_user, log, require
from .db import ConsolidationEntry, Engagement, GroupComponent, User, get_session
from .engagements import get_or_404, held_engagement, safe_stem
from .exports import attachment, finish_workbook

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

SCOPES = {"full": "Full audit", "specific": "Specific balances", "analytical":
          "Analytical procedures only"}
SIGNIFICANT_PCT = 15.0
MAX_COMPONENTS = 50
MAX_ENTRIES = 500
CENT = 0.005
RESERVE = translation.RESERVE

Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]
Money = Annotated[float, Field(ge=0, le=1e13, allow_inf_nan=False)]


class ComponentIn(BaseModel):
    component_engagement_id: int
    ownership_pct: float = Field(default=100.0, gt=0, le=100, allow_inf_nan=False)
    materiality: Money | None = None
    scope: Literal["full", "specific", "analytical"] = "full"
    note: Text = ""


class ComponentPatch(BaseModel):
    ownership_pct: float = Field(gt=0, le=100, allow_inf_nan=False)
    materiality: Money | None = None
    scope: Literal["full", "specific", "analytical"]
    note: Text = ""


class EntryLineIn(BaseModel):
    line: str
    debit: Money = 0.0
    credit: Money = 0.0


class EntryIn(BaseModel):
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1,
                                                  max_length=2000)]
    lines: list[EntryLineIn] = Field(min_length=2, max_length=50)


def _r(x: float) -> float:
    return round(float(x) + 0.0, 2)


def debit_positive(data: dict, t=None) -> dict[str, float]:
    """Each statement line's adjusted balance, debit-positive, translated when `t` is set."""
    out = {}
    for s in data.get("lead_sheets", []):
        line, amount = s["line"], s["totals"]["adjusted"]
        dp = amount if statements.debit_normal(line) else -amount
        if t is not None:
            if line == "retained_earnings" and t.opening_reserves is not None:
                dp = -translation.retained_presented(data, t)
            else:
                dp *= translation.rate_for(line, t)[0]
        out[line] = dp
    return out


def entry_effects(x: ConsolidationEntry) -> dict[str, float]:
    out: dict[str, float] = {}
    for ln in x.lines:
        out[ln["line"]] = out.get(ln["line"], 0.0) + ln["debit"] - ln["credit"]
    return out


def component_out(c: GroupComponent, figures: dict | None = None) -> dict:
    ce = c.component
    return {"id": c.id, "component_engagement_id": ce.id, "client": ce.client.name,
            "engagement": ce.name, "period_end": ce.period_end.isoformat(),
            "ownership_pct": c.ownership_pct, "materiality": c.materiality, "scope": c.scope,
            "scope_label": SCOPES[c.scope], "note": c.note, "created_by": c.created_by,
            "currency": (ce.translation.presentation if ce.translation else None),
            **(figures or {})}


def entry_out(x: ConsolidationEntry) -> dict:
    return {"id": x.id, "description": x.description,
            "lines": [{**ln, "label": statements.LINE[ln["line"]]["label"]} for ln in x.lines],
            "total": _r(sum(ln["debit"] for ln in x.lines)), "created_by": x.created_by,
            "created_at": x.created_at.isoformat()}


def consolidate(session: Session, e: Engagement) -> dict:
    """The consolidation worksheet: each column's statements and the group total."""
    from .leadsheets import compute
    columns: list[dict] = []
    parent_data = compute(session, e, None, None)[0]
    columns.append({"key": "parent", "label": f"{e.client.name} (this engagement)",
                    "by_line": debit_positive(parent_data), "ownership": 100.0})
    for c in e.components:
        data = compute(session, c.component, None, None)[0]
        columns.append({"key": f"c{c.id}", "label": f"{c.component.client.name}",
                        "by_line": debit_positive(data, c.component.translation),
                        "ownership": c.ownership_pct, "component": c,
                        "has_tb": bool(data),
                        # What doesn't balance before translating, at the closing rate.
                        "own_difference": (data["statements"]["difference"]["current"]
                                           * c.component.translation.closing_rate
                                           if data and c.component.translation else 0.0)})
    elim: dict[str, float] = {}
    for x in e.consolidation_entries:
        for line, amount in entry_effects(x).items():
            elim[line] = elim.get(line, 0.0) + amount
    columns.append({"key": "eliminations", "label": "Eliminations", "by_line": elim,
                    "ownership": 100.0})
    used = [line for line, _, _ in statements.LINES
            if any(abs(col["by_line"].get(line, 0.0)) >= CENT for col in columns)]

    def statements_of(by_line: dict[str, float]) -> dict:
        return statements._statements({line: [{"adjusted": by_line.get(line, 0.0),
                                               "prior": 0.0}] for line in used})

    total = {line: sum(col["by_line"].get(line, 0.0) for col in columns) for line in used}
    for col in columns:
        col["statements"] = statements_of(col["by_line"])
    group = {"key": "group", "label": "Group", "by_line": total,
             "statements": statements_of(total)}
    # Translating at different rates leaves a difference in each translated component; it
    # is that component's translation reserve.
    for col in columns:
        c = col.get("component")
        col["reserve"] = (col["statements"]["difference"]["current"] - col["own_difference"]
                          if c is not None and c.component.translation else 0.0)
    reserve = _r(sum(col["reserve"] for col in columns))
    all_cols = [*columns, group]
    group["reserve"] = reserve

    def merged(name: str) -> list[dict]:
        rows = []
        for i, r in enumerate(group["statements"][name]):
            rows.append({"label": r["label"], "kind": r["kind"],
                         "values": [col["statements"][name][i]["current"] for col in all_cols]})
        return rows

    income, balance = merged("income_statement"), merged("balance_sheet")
    if any(abs(col["reserve"]) >= CENT for col in all_cols):
        at = next(i for i, r in enumerate(balance) if r["label"] == "Total equity")
        balance.insert(at, {"label": RESERVE, "kind": "line",
                            "values": [_r(col["reserve"]) for col in all_cols]})
        for r in balance[at + 1:]:
            if r["label"] in ("Total equity", "Total liabilities and equity"):
                r["values"] = [_r(v + col["reserve"]) for v, col in zip(r["values"], all_cols,
                                                                         strict=True)]

    # Non-controlling interests: their share of each part-owned component's profit and net
    # assets (after translation).
    nci_profit = nci_assets = 0.0
    for col in columns:
        share = 1 - col["ownership"] / 100
        if share > 0:
            st = col["statements"]
            nci_profit += share * st["profit"]["current"]
            net_assets = next(r["current"] for r in st["balance_sheet"]
                              if r["label"] == "Total assets") - next(
                r["current"] for r in st["balance_sheet"] if r["label"] == "Total liabilities")
            nci_assets += share * net_assets
    profit = group["statements"]["profit"]["current"]
    equity = next(r for r in balance if r["label"] == "Total equity")["values"][-1]

    unbalanced = [{"label": col["label"], "difference": _r(col["statements"]["difference"]
                                                            ["current"] - col["reserve"])}
                  for col in all_cols if abs(col["statements"]["difference"]["current"]
                                             - col["reserve"]) >= CENT]
    return {"columns": [{"key": c["key"], "label": c["label"]} for c in all_cols],
            "income_statement": income, "balance_sheet": balance,
            "profit": _r(profit), "reserve": reserve,
            "attributable": {"profit_owners": _r(profit - nci_profit),
                             "profit_nci": _r(nci_profit),
                             "equity_owners": _r(equity - nci_assets),
                             "equity_nci": _r(nci_assets)},
            "unbalanced": unbalanced, "_columns": columns}


def component_checks(e: Engagement, columns: list[dict]) -> dict[int, dict]:
    """Each component's share of the group and what to look at."""
    def gross(col: dict, lines: tuple[str, ...], sign: int) -> float:
        return sum(sign * col["by_line"].get(line, 0.0) for line in lines)

    revenue_lines = tuple(line for line, _, sec in statements.LINES if sec == "revenue")
    asset_lines = tuple(line for line, _, sec in statements.LINES
                        if sec in ("current_assets", "noncurrent_assets"))
    parts = [col for col in columns if col["key"] != "eliminations"]
    revenue = sum(gross(col, revenue_lines, -1) for col in parts)
    assets = sum(gross(col, asset_lines, 1) for col in parts)
    out = {}
    for col in parts:
        c = col.get("component")
        if c is None:
            continue
        rev_pct = round(gross(col, revenue_lines, -1) / revenue * 100, 1) if revenue else None
        asset_pct = round(gross(col, asset_lines, 1) / assets * 100, 1) if assets else None
        significant = any(p is not None and p >= SIGNIFICANT_PCT for p in (rev_pct, asset_pct))
        flags = []
        if not col.get("has_tb"):
            flags.append("Its engagement has no trial balance yet.")
        if e.overall_materiality and c.materiality and c.materiality > e.overall_materiality:
            flags.append("Its materiality is above the group's materiality.")
        if e.overall_materiality and c.materiality is None:
            flags.append("Set its materiality.")
        if significant and c.scope != "full":
            flags.append("It is a significant component but not in full scope.")
        t = c.component.translation
        if t is not None and e.translation is not None and t.presentation != e.translation.functional:
            flags.append(f"It is translated into {t.presentation}, but the group's figures "
                         f"are in {e.translation.functional}.")
        elif t is not None and e.translation is None:
            flags.append(f"It is translated into {t.presentation}: check that the group "
                         "reports in that currency.")
        out[c.id] = {"revenue_pct": rev_pct, "assets_pct": asset_pct,
                     "significant": significant, "flags": flags}
    return out


def group_out(session: Session, e: Engagement) -> dict:
    result = consolidate(session, e)
    checks = component_checks(e, result.pop("_columns"))
    return {"components": [component_out(c, checks.get(c.id)) for c in e.components],
            "entries": [entry_out(x) for x in e.consolidation_entries],
            "materiality": e.overall_materiality,
            "result": result if e.components else None,
            "lines": [{"id": i, "label": label} for i, label, _ in statements.LINES]}


@router.get("/engagements/{engagement_id}/group")
def get_group(engagement_id: int, user: User = Depends(current_user),
              session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id, user)
    out = group_out(session, e)
    # The engagements the user could add, for the picker.
    taken = {c.component_engagement_id for c in e.components}
    out["choices"] = [{"id": x.id, "label": f"{x.client.name}: {x.name}"}
                      for x in session.query(Engagement).order_by(Engagement.id)
                      if x.id != e.id and x.id not in taken
                      and auth.on_engagement(session, user, x.id)][:500]
    return out


@router.post("/engagements/{engagement_id}/group/components", status_code=201)
def add_component(engagement_id: int, body: ComponentIn, user: User = Depends(require("prepare")),
                  session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"group:{engagement_id}")
    ce = session.get(Engagement, body.component_engagement_id)
    if ce is None or not auth.on_engagement(session, user, ce.id):
        raise HTTPException(400, "Pick an engagement you are on as the component.")
    if ce.id == e.id:
        raise HTTPException(400, "An engagement can't be a component of itself.")
    if any(c.component_engagement_id == ce.id for c in e.components):
        raise HTTPException(409, "That engagement is already a component of this group.")
    if any(c.component_engagement_id == e.id for c in ce.components):
        raise HTTPException(400, "That engagement is itself the group for this one.")
    if len(e.components) >= MAX_COMPONENTS:
        raise HTTPException(400, f"A group can have up to {MAX_COMPONENTS} components.")
    c = GroupComponent(engagement=e, component=ce, ownership_pct=body.ownership_pct,
                       materiality=body.materiality, scope=body.scope, note=body.note,
                       created_by=user.name)
    session.add(c)
    session.flush()
    log(session, user, "group.component_add",
        f"Added {ce.client.name}: {ce.name} as a component ({body.ownership_pct:g}% owned)", e,
        details={"component_id": c.id, "component_engagement_id": ce.id,
                 "ownership_pct": body.ownership_pct, "materiality": body.materiality,
                 "scope": body.scope})
    session.commit()
    return group_out(session, e)


def held_component(session: Session, user: User, component_id: int) -> GroupComponent:
    c = get_or_404(session, GroupComponent, component_id, user)
    held_engagement(session, user, c.engagement_id, f"group:{c.engagement_id}")
    return get_or_404(session, GroupComponent, component_id, user)


@router.patch("/group-components/{component_id}")
def update_component(component_id: int, body: ComponentPatch,
                     user: User = Depends(require("prepare")),
                     session: Session = Depends(get_session)) -> dict:
    c = held_component(session, user, component_id)
    before = {"ownership_pct": c.ownership_pct, "materiality": c.materiality,
              "scope": c.scope, "note": c.note}
    changes = body.model_dump(exclude_unset=True)  # a field left out keeps its value
    for k, v in changes.items():
        setattr(c, k, v)
    log(session, user, "group.component_update",
        f"Changed component {c.component.client.name}: {c.component.name}", c.engagement,
        details={"component_id": c.id, "before": before, "after": changes})
    session.commit()
    return group_out(session, c.engagement)


@router.delete("/group-components/{component_id}")
def delete_component(component_id: int, user: User = Depends(require("prepare")),
                     session: Session = Depends(get_session)) -> dict:
    c = held_component(session, user, component_id)
    e = c.engagement
    log(session, user, "group.component_remove",
        f"Took {c.component.client.name}: {c.component.name} out of the group", e,
        details={"component_id": c.id, "component_engagement_id": c.component_engagement_id})
    session.delete(c)
    session.commit()
    session.refresh(e)
    return group_out(session, e)


@router.post("/engagements/{engagement_id}/group/entries", status_code=201)
def add_entry(engagement_id: int, body: EntryIn, user: User = Depends(require("review")),
              session: Session = Depends(get_session)) -> dict:
    lines = []
    for i, ln in enumerate(body.lines, start=1):
        if ln.line not in statements.LINE:
            raise HTTPException(400, f"Line {i}: pick a statement line.")
        if (ln.debit > 0) == (ln.credit > 0):
            raise HTTPException(400, f"Line {i}: enter either a debit or a credit.")
        lines.append({"line": ln.line, "debit": round(ln.debit, 2),
                      "credit": round(ln.credit, 2)})
    dr = round(sum(x["debit"] for x in lines) * 100)
    cr = round(sum(x["credit"] for x in lines) * 100)
    if dr != cr:
        raise HTTPException(400, f"Debits ({dr / 100:,.2f}) and credits ({cr / 100:,.2f}) "
                                 "must be equal.")
    e = held_engagement(session, user, engagement_id, f"group:{engagement_id}")
    if len(e.consolidation_entries) >= MAX_ENTRIES:
        raise HTTPException(400, f"A group can have up to {MAX_ENTRIES} eliminations.")
    x = ConsolidationEntry(engagement=e, description=body.description, lines=lines,
                           created_by=user.name)
    session.add(x)
    session.flush()
    log(session, user, "group.entry_add",
        f"Added the elimination {body.description} ({dr / 100:,.2f})", e,
        details={"entry_id": x.id, "lines": lines})
    session.commit()
    return group_out(session, e)


@router.delete("/consolidation-entries/{consolidation_entry_id}")
def delete_entry(consolidation_entry_id: int, user: User = Depends(require("review")),
                 session: Session = Depends(get_session)) -> dict:
    x = get_or_404(session, ConsolidationEntry, consolidation_entry_id, user)
    e = held_engagement(session, user, x.engagement_id, f"group:{x.engagement_id}")
    x = get_or_404(session, ConsolidationEntry, consolidation_entry_id, user)
    log(session, user, "group.entry_delete", f"Deleted the elimination {x.description}", e,
        details={"entry_id": x.id, "lines": x.lines})
    session.delete(x)
    session.commit()
    session.refresh(e)
    return group_out(session, e)


def workbook(session: Session, e: Engagement) -> tuple[str, bytes]:
    out = group_out(session, e)
    r = out["result"]
    labels = [c["label"] for c in r["columns"]] if r else []
    sheets = {}
    if r:
        for name, title in (("income_statement", "Income statement"),
                            ("balance_sheet", "Balance sheet")):
            sheets[title] = pd.DataFrame(
                [[x["label"], *(None if x["kind"] == "heading" else v for v in x["values"])]
                 for x in r[name]], columns=["Line", *labels])
        a = r["attributable"]
        sheets["Attributable"] = pd.DataFrame([
            ("Profit attributable to owners of the parent", a["profit_owners"]),
            ("Profit attributable to non-controlling interests", a["profit_nci"]),
            ("Equity attributable to owners of the parent", a["equity_owners"]),
            ("Non-controlling interests", a["equity_nci"])], columns=["Item", "Amount"])
    sheets["Components"] = pd.DataFrame([{
        "Component": f"{c['client']}: {c['engagement']}", "Period end": c["period_end"],
        "Owned %": c["ownership_pct"], "Scope": c["scope_label"],
        "Materiality": c["materiality"], "Share of revenue %": c.get("revenue_pct"),
        "Share of total assets %": c.get("assets_pct"),
        "Significant": "Yes" if c.get("significant") else "No",
        "To look at": " ".join(c.get("flags", [])), "Note": c["note"]}
        for c in out["components"]])
    sheets["Eliminations"] = pd.DataFrame([{
        "Elimination": x["description"], "Line": ln["label"], "Debit": ln["debit"] or None,
        "Credit": ln["credit"] or None, "By": x["created_by"]}
        for x in out["entries"] for ln in x["lines"]],
        columns=["Elimination", "Line", "Debit", "Credit", "By"])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        for title, df in sheets.items():
            df.to_excel(xl, sheet_name=title, index=False)
        finish_workbook(xl.book)
    return f"{safe_stem(f'{e.client.name}-{e.name}-consolidation')}.xlsx", buf.getvalue()


@router.get("/engagements/{engagement_id}/group/export")
def export_group(engagement_id: int, user: User = Depends(current_user),
                 session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    log(session, user, "group.download", "Downloaded the consolidation", e)
    session.commit()
    name, content = workbook(session, e)
    return Response(content, media_type=XLSX, headers=attachment(name))
