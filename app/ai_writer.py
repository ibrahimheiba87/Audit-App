"""AI drafts on top of the assistant (assistant.py, ai_tools.py): a risk assessment for planning,
and a management letter written from the findings register.

Like the assistant, they are off without ANTHROPIC_API_KEY. The AI gets figures and findings
the app has already worked out (lead sheet totals, ratios, materiality, findings), never whole
files, and its answer is checked before it is kept: risk areas must be statement lines the app
knows, and letter points may only cite findings that were sent. Each draft is saved with what
the AI was given, logged, and exported through exports.finish_workbook.
"""

from __future__ import annotations

import io
import json
from typing import Literal

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import assistant, register
from .ai_tools import DATA_ONLY, _list, _text, ask, require_ai
from .auth import current_user, log, require
from .dashboard import bank_state
from .db import AiDraft, Engagement, Finding, User, get_session
from .engagements import get_or_404, held_engagement, materiality_out, safe_stem
from .exports import attachment, finish_workbook
from .leadsheets import compute
from .statements import LINE

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TIMEOUT = 240
MAX_TOKENS = 16000
MAX_LETTER_FINDINGS = 80
MAX_RISK_FINDINGS = 30  # findings described one by one in the risk assessment
MAX_RISKS = 25
MAX_POINTS = 30
SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}

LEVELS = {"significant": "Significant risk", "higher": "Higher risk", "lower": "Lower risk"}
ASSERTIONS = {
    "existence": "Existence", "occurrence": "Occurrence", "completeness": "Completeness",
    "accuracy": "Accuracy", "valuation": "Valuation", "cut_off": "Cut-off",
    "rights": "Rights and obligations", "classification": "Classification",
    "presentation": "Presentation and disclosure",
}
AREAS = {"entity": "Whole financial statements", "fraud": "Fraud",
         "going_concern": "Going concern"}
PRIORITIES = {"high": "High", "medium": "Medium", "low": "Low"}
KINDS = {"risk": "Risk assessment", "letter": "Management letter"}

LANGUAGE = {"en": "", "ar": " Write every text value in Modern Standard Arabic, keeping figures, "
                         "account numbers and finding numbers as they are."}

RISK_SYSTEM = (
    "You help a financial statement auditor plan an audit by assessing the risks of material "
    "misstatement (as in ISA 315). " + DATA_ONLY +
    "Base every risk on the figures, ratios and findings given; say which ones in 'why'. "
    "Include revenue recognition and management override of controls (as fraud risks) unless "
    "the data clearly rules them out. Answer with JSON only, no other text, in exactly this "
    'shape: {"summary": "<two or three sentences>", "risks": [{"area": "<line id, or entity, '
    'fraud or going_concern>", "title": "<the risk in a few words>", '
    '"level": "significant|higher|lower", "assertions": ["<assertion ids>"], '
    '"why": "<one or two sentences>", "procedures": ["<a planned audit procedure>"]}]}. '
    f"Assertion ids: {', '.join(ASSERTIONS)}. Order the risks from the highest. At most "
    f"{MAX_RISKS} risks and 5 procedures each.")

LETTER_SYSTEM = (
    "You help a financial statement auditor write the management letter (the letter on "
    "internal control deficiencies and other matters found during the audit) to the client's "
    "management. " + DATA_ONLY +
    "Group related findings into points, write for the client's management in plain, polite, "
    "professional language, and don't name the auditor's staff. Use only what the findings "
    "say; don't invent amounts, dates or causes. Answer with JSON only, no other text, in "
    'exactly this shape: {"intro": "<opening paragraph>", "points": [{"title": "...", '
    '"finding_ids": [<numbers of the findings it covers>], "observation": "<what was found>", '
    '"implication": "<the risk to the business>", "recommendation": "<what management should '
    'do>", "priority": "high|medium|low"}], "closing": "<closing paragraph>"}. '
    f"At most {MAX_POINTS} points, most important first.")


class RiskIn(BaseModel):
    upload_id: int | None = None
    prior_upload_id: int | None = None
    language: Literal["en", "ar"] = "en"


class LetterIn(BaseModel):
    statuses: list[Literal["open", "reviewed", "cleared"]] = Field(
        default=["open", "reviewed"], min_length=1)
    language: Literal["en", "ar"] = "en"


def draft_out(d: AiDraft) -> dict:
    return {"id": d.id, "engagement_id": d.engagement_id, "kind": d.kind,
            "kind_label": KINDS[d.kind], "language": d.language, "content": d.content,
            "sent": d.sent, "model": d.model, "created_by": d.created_by,
            "created_at": d.created_at.isoformat()}


def save(session: Session, e: Engagement, user: User, kind: str, language: str, content: dict,
         sent: dict) -> AiDraft:
    d = AiDraft(engagement=e, kind=kind, language=language, content=content, sent=sent,
                model=assistant.model() or "", created_by=user.name)
    session.add(d)
    session.flush()
    return d


# --- Risk assessment ----------------------------------------------------------------

def risk_context(session: Session, e: Engagement, body: RiskIn) -> tuple[dict, dict]:
    """What the AI is told about the engagement, and the counts kept with the draft."""
    data, tb, _ = compute(session, e, body.upload_id, body.prior_upload_id)
    if tb is None:
        raise HTTPException(400, "Upload this year's trial balance first.")
    lines = []
    for sheet in data["lead_sheets"]:
        t = sheet["totals"]
        prior = t["prior"] if data["has_prior"] else None
        change = (None if not prior else round((t["adjusted"] - prior) / abs(prior) * 100, 1))
        lines.append({"line": sheet["line"], "label": sheet["label"],
                      "section": sheet["section_label"], "this_year": t["adjusted"],
                      "last_year": prior, "change_pct": change})
    ratios = [{"ratio": r["label"], "this_year": r["current"], "last_year": r["prior"],
               "flag": r["flag"]} for r in data["ratios"]["ratios"] if r["current"] is not None]
    findings = list(session.scalars(select(Finding).where(
        Finding.engagement_id == e.id, Finding.status != "cleared")))
    by_check: dict[tuple[str, str], int] = {}
    for f in findings:
        by_check[(f.check_name, f.severity)] = by_check.get((f.check_name, f.severity), 0) + 1
    # The counts say little about findings added by hand (all one "check"), so the most
    # severe findings are also described one by one, those added by hand first.
    findings.sort(key=lambda f: (f.source != "manual", SEVERITY_RANK.get(f.severity, 3),
                                 f.id))
    detailed = [{"check": f.check_name, "severity": f.severity, "account": f.account,
                 "description": _text(f.description, 300),
                 "detail": _text(f.detail, 300), "amount": f.amount}
                for f in findings[:MAX_RISK_FINDINGS]]
    unc = data["uncorrected"]
    context = {
        "engagement": {"framework": e.framework, "period_end": e.period_end.isoformat(),
                       "period_start": e.period_start.isoformat() if e.period_start else None},
        "materiality": materiality_out(e),
        "statement_lines": lines,
        "unmapped_accounts": len(data["unmapped"]),
        "ratios": ratios,
        "warning_signs": [w["text"] for w in data["ratios"]["warnings"]],
        "findings_not_cleared": [{"check": c, "severity": s, "count": n}
                                 for (c, s), n in sorted(by_check.items(),
                                                         key=lambda x: -x[1])][:40],
        "most_severe_findings": detailed,
        "findings_described": f"{len(detailed)} of {len(findings)}",
        # Accounts not mapped yet aren't counted in the effect, so no percentage until they are.
        "uncorrected_misstatements": {
            "entries": len(unc["entries"]), "profit": unc["profit"],
            "net_assets": unc["net_assets"],
            "pct_of_materiality": None if unc["unclassified"] else unc["pct_of_overall"],
            "unclassified_accounts": unc["unclassified"][:20],
            "conclusion": unc["conclusion"]},
        "bank_reconciliation": bank_state(e),
    }
    sent = {"lines": len(lines), "ratios": len(ratios), "findings": len(findings),
            "trial_balance": tb.filename, "has_prior": data["has_prior"]}
    return context, sent


def clean_risks(answer: dict) -> dict:
    risks = []
    for r in _list(answer.get("risks")):
        if not isinstance(r, dict):
            continue
        area, level = r.get("area"), r.get("level")
        if not isinstance(area, str) or (area not in LINE and area not in AREAS):
            area = "entity"
        if not isinstance(level, str) or level not in LEVELS:
            level = "higher"
        title = _text(r.get("title"), 200)
        if not title:
            continue
        risks.append({
            "area": area, "area_label": LINE[area]["label"] if area in LINE else AREAS[area],
            "title": title, "level": level,
            "assertions": [a for a in dict.fromkeys(x for x in _list(r.get("assertions"))
                                                    if isinstance(x, str)) if a in ASSERTIONS],
            "why": _text(r.get("why"), 1000),
            "procedures": [_text(p, 500) for p in _list(r.get("procedures"))
                           if isinstance(p, str | int | float) and _text(p)][:5],
        })
    order = list(LEVELS)
    risks.sort(key=lambda r: order.index(r["level"]))  # stable: the AI's order within a level
    return {"summary": _text(answer.get("summary"), 2000), "risks": risks[:MAX_RISKS]}


def checked_risks(answer: dict) -> dict:
    content = clean_risks(answer)
    if not content["risks"]:
        raise ValueError("The AI's answer had no risks in it. Try again.")
    return content


@router.post("/engagements/{engagement_id}/ai/risk-assessment", status_code=201)
def risk_assessment(engagement_id: int, body: RiskIn, user: User = Depends(require("prepare")),
                    session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id, user)
    require_ai()
    register.sync_engagement(session, e)
    context, sent = risk_context(session, e, body)
    prompt = (f"<engagement>\n{json.dumps(context, default=str)}\n</engagement>\n\n"
              "Assess the risks of material misstatement." + LANGUAGE[body.language])
    content = ask(session, user, e.id, "assistant.risk_assessment",
                  "Asked the AI assistant for a risk assessment", sent, RISK_SYSTEM, prompt,
                  check=checked_risks, max_tokens=MAX_TOKENS, timeout=TIMEOUT)
    # Held like a deletion holds it, so the engagement can't go between the check and the save.
    e = held_engagement(session, user, engagement_id, f"team:{engagement_id}")
    d = save(session, e, user, "risk", body.language, content, sent)
    session.commit()
    return draft_out(d)


# --- Management letter --------------------------------------------------------------

def finding_brief(f: Finding) -> dict:
    notes = [n.text for n in f.notes if n.text.strip()][-3:]
    return {"number": f.id, "check": f.check_name, "severity": f.severity, "status": f.status,
            "date": f.date, "account": f.account, "description": f.description,
            "amount": f.amount, "entry": f.entry_id, "detail": f.detail[:1500],
            "auditor_notes": [n[:500] for n in notes]}


def clean_letter(answer: dict, sent_ids: set[int]) -> dict:
    points = []
    for p in _list(answer.get("points")):
        if not isinstance(p, dict):
            continue
        ids = list(dict.fromkeys(i for i in _list(p.get("finding_ids"))
                                 if isinstance(i, int) and not isinstance(i, bool)
                                 and i in sent_ids))
        title = _text(p.get("title"), 200)
        if not title:
            continue
        priority = p.get("priority")
        points.append({
            "title": title, "finding_ids": ids,
            "observation": _text(p.get("observation"), 3000),
            "implication": _text(p.get("implication"), 2000),
            "recommendation": _text(p.get("recommendation"), 2000),
            "priority": priority if isinstance(priority, str) and priority in PRIORITIES
            else "medium",
        })
    return {"intro": _text(answer.get("intro"), 3000), "points": points[:MAX_POINTS],
            "closing": _text(answer.get("closing"), 3000)}


@router.post("/engagements/{engagement_id}/ai/management-letter", status_code=201)
def management_letter(engagement_id: int, body: LetterIn,
                      user: User = Depends(require("prepare")),
                      session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id, user)
    require_ai()
    register.sync_engagement(session, e)
    statuses = list(dict.fromkeys(body.statuses))
    findings = list(session.scalars(register.with_details(select(Finding).where(
        Finding.engagement_id == e.id, Finding.status.in_(statuses)))))
    if not findings:
        raise HTTPException(400, "There are no findings with those statuses to write about.")
    findings.sort(key=lambda f: (SEVERITY_RANK.get(f.severity, 3), f.id))
    total = len(findings)
    findings = findings[:MAX_LETTER_FINDINGS]
    briefs = [finding_brief(f) for f in findings]
    context = {"framework": e.framework, "period_end": e.period_end.isoformat(),
               "materiality": materiality_out(e)}
    prompt = (f"<engagement>\n{json.dumps(context, default=str)}\n</engagement>\n"
              "<findings>\n" + "\n".join(assistant.as_json(b) for b in briefs)
              + "\n</findings>\n\nWrite the management letter points."
              + LANGUAGE[body.language])
    sent = {"findings": len(briefs), "findings_total": total, "statuses": statuses}
    sent_ids = {b["number"] for b in briefs}

    def checked(answer: dict) -> dict:
        content = clean_letter(answer, sent_ids)
        if not content["points"]:
            raise ValueError("The AI's answer had no letter points in it. Try again.")
        return content

    content = ask(session, user, e.id, "assistant.management_letter",
                  f"Asked the AI assistant to draft a management letter from {len(briefs)} "
                  "findings", sent, LETTER_SYSTEM, prompt, check=checked, max_tokens=MAX_TOKENS,
                  timeout=TIMEOUT)
    e = held_engagement(session, user, engagement_id, f"team:{engagement_id}")
    d = save(session, e, user, "letter", body.language, content, sent)
    session.commit()
    return draft_out(d)


# --- Saved drafts ---------------------------------------------------------------------

@router.get("/engagements/{engagement_id}/ai-drafts")
def list_drafts(engagement_id: int, user: User = Depends(current_user),
                session: Session = Depends(get_session)) -> dict:
    """The latest draft of each kind, and how many earlier ones there are."""
    e = get_or_404(session, Engagement, engagement_id, user)
    out = {}
    for kind in KINDS:
        drafts = [d for d in e.ai_drafts if d.kind == kind]
        out[kind] = {"latest": draft_out(drafts[-1]) if drafts else None, "count": len(drafts)}
    return out


@router.get("/ai-drafts/{draft_id}")
def get_draft(draft_id: int, user: User = Depends(current_user),
              session: Session = Depends(get_session)) -> dict:
    return draft_out(get_or_404(session, AiDraft, draft_id, user))


def to_excel(d: AiDraft, context: dict) -> bytes:
    c = d.content
    head = list(context.items())
    if d.kind == "risk":
        head += [("Summary", c["summary"])]
        rows = pd.DataFrame([{
            "Level": LEVELS[r["level"]], "Area": r["area_label"], "Risk": r["title"],
            "Assertions": ", ".join(ASSERTIONS[a] for a in r["assertions"]),
            "Why": r["why"], "Planned procedures": "\n".join(r["procedures"]),
        } for r in c["risks"]])
        sheet = "Risks"
    else:
        s = d.sent
        if s.get("findings_total", 0) > s.get("findings", 0):
            head += [("Coverage", f"Written from {s['findings']} of {s['findings_total']} "
                                  "findings, the most severe first. The rest were not sent.")]
        head += [("Introduction", c["intro"]), ("Closing", c["closing"])]
        rows = pd.DataFrame([{
            "Priority": PRIORITIES[p["priority"]], "Point": p["title"],
            "Observation": p["observation"], "Implication": p["implication"],
            "Recommendation": p["recommendation"],
            "Findings": ", ".join(f"#{i}" for i in p["finding_ids"]),
        } for p in c["points"]])
        sheet = "Letter points"
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(head, columns=["Item", "Value"]).to_excel(xl, sheet_name="Summary",
                                                               index=False)
        rows.to_excel(xl, sheet_name=sheet, index=False)
        finish_workbook(xl.book)
    return buf.getvalue()


@router.get("/ai-drafts/{draft_id}/export")
def export_draft(draft_id: int, user: User = Depends(current_user),
                 session: Session = Depends(get_session)) -> Response:
    d = get_or_404(session, AiDraft, draft_id, user)
    e = d.engagement
    label = KINDS[d.kind]
    log(session, user, "ai_draft.download", f"Downloaded AI {label.lower()} #{d.id}", e,
        details={"draft_id": d.id, "kind": d.kind})
    session.commit()
    context = {"Client": e.client.name, "Engagement": e.name,
               "Period end": e.period_end.isoformat(),
               "Drafted": f"{d.created_at:%Y-%m-%d %H:%M} UTC by {d.created_by} with {d.model}",
               "Note": "Drafted by AI. Check it against the audit file before relying on it."}
    stem = safe_stem(f"{e.client.name}-{e.name}-{label}-{d.id}")
    return Response(to_excel(d, context), media_type=XLSX, headers=attachment(f"{stem}.xlsx"))
