"""Related parties (ISA 550): the register of the client's related parties, and a scan of the
general ledger for transactions that name them.

The team lists each party (shareholders, directors, their families, group companies and so
on) with any other names it may appear under. The scan looks for those names in the ledger's
descriptions and account names (and any other text column of the file, such as an account
name beside the account code), ignoring case, punctuation and company endings such as "Ltd",
and lists what it finds, flagging entries near the period end and round amounts. Someone then
records a conclusion on each party's transactions and whether the statements disclose it.
The scan's counts are kept so the dashboard can prompt without reading the ledger again.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import timedelta
from typing import Annotated, Literal

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.orm import Session

from . import auth
from .auth import current_user, log, require
from .db import Engagement, RelatedParty, Upload, User, get_session, utcnow
from .engagements import forget_scan, get_or_404, held_engagement, load_upload, safe_stem
from .exports import attachment, finish_workbook

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

RELATIONSHIPS = {
    "parent": "Parent or controlling party", "shareholder": "Shareholder",
    "director": "Director or board member", "key_management": "Key management",
    "family": "Close family member", "group": "Group company or subsidiary",
    "associate": "Associate or joint venture", "controlled": "Entity they control",
    "other": "Other",
}
DISCLOSED = {"not_checked": "Not checked yet", "disclosed": "Disclosed",
             "not_needed": "No disclosure needed", "missing": "Not disclosed"}
# Company endings left out when matching, so "Acme Ltd" finds "ACME LIMITED".
ENDINGS = {"ltd", "limited", "llc", "inc", "co", "company", "corp", "corporation", "plc",
           "sa", "sarl", "gmbh", "bv", "llp", "lp", "pjsc", "psc", "fze", "fzco", "fzc",
           "est", "establishment", "group", "holding", "holdings", "the",
           "شركه", "مؤسسه", "ذمم", "شمع", "شمم", "شذمم", "ش", "م", "ع", "ذ"}
# Arabic spelling variants made the same: vowel marks and tatweel dropped, alef forms,
# alef maqsura and taa marbuta written one way.
ARABIC = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ة": "ه",
                        **{chr(c): None for c in (*range(0x064B, 0x0660), 0x0670, 0x0640)}})
MIN_NAME = 3  # shorter names would match too much
NEAR_DAYS = 7  # entries this close to the period end are flagged
ROUND = 1000
MAX_PARTIES = 300
MAX_ALIASES = 10
MAX_SHOWN = 200  # transactions returned per party; the Excel file has them all
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=5000)]


class PartyIn(BaseModel):
    name: Name
    relationship: Literal[tuple(RELATIONSHIPS)] = "other"  # type: ignore[valid-type]
    aliases: list[Name] = Field(default_factory=list, max_length=MAX_ALIASES)
    note: Text = ""
    source: Annotated[str, StringConstraints(max_length=50)] = ""


class PartyPatch(BaseModel):
    """Only the fields sent change."""
    name: Name | None = None
    relationship: Literal[tuple(RELATIONSHIPS)] | None = None  # type: ignore[valid-type]
    aliases: list[Name] | None = Field(default=None, max_length=MAX_ALIASES)
    note: Text | None = None
    conclusion: Text | None = None
    reviewed: bool | None = None
    disclosed: Literal[tuple(DISCLOSED)] | None = None  # type: ignore[valid-type]


class ScanIn(BaseModel):
    upload_id: int | None = None


def normalize(text: object) -> str:
    """Lower case words, punctuation dropped, company endings left out."""
    text = str(text or "").lower().translate(ARABIC)
    text = re.sub(r"(?<=\w)\.(?=\w)", "", text)  # "l.l.c." -> "llc."
    words = re.sub(r"[\W_]+", " ", text).split()
    return " ".join(w for w in words if w not in ENDINGS)


def patterns(p: RelatedParty) -> list[str]:
    out = []
    for n in [p.name, *(p.aliases or [])]:
        k = normalize(n)
        if len(k) >= MIN_NAME and k not in out:
            out.append(k)
    return out


def matches_name(text: str, keys: list[str]) -> bool:
    """`text` already normalized; whole words only, so "Ali" doesn't find "Alison"."""
    padded = f" {text} "
    return any(f" {k} " in padded for k in keys)


def cell(v: object) -> str:
    return "" if v is None or pd.isna(v) else str(v)


def party_out(p: RelatedParty) -> dict:
    return {
        "id": p.id, "name": p.name, "relationship": p.relationship_kind,
        "relationship_label": RELATIONSHIPS.get(p.relationship_kind, p.relationship_kind),
        "aliases": list(p.aliases or []), "note": p.note,
        "searchable": bool(patterns(p)),
        "scanned_at": p.scanned_at.isoformat() if p.scanned_at else None,
        "matches": p.matches, "matched_amount": round(p.matched_amount, 2),
        "conclusion": p.conclusion, "reviewed_by": p.reviewed_by,
        "reviewed_at": p.reviewed_at.isoformat() if p.reviewed_at else None,
        "disclosed": p.disclosed, "disclosed_label": DISCLOSED.get(p.disclosed, p.disclosed),
        "source": p.source, "created_by": p.created_by, "updated_by": p.updated_by,
        "updated_at": p.updated_at.isoformat(),
    }


def summary(e: Engagement) -> dict:
    parties = list(e.related_parties)
    return {
        "parties": len(parties),
        # Every current name checked: a party added or renamed since the last scan wasn't.
        "scanned": bool(parties) and all(p.scanned_at for p in parties),
        "with_matches": sum(1 for p in parties if p.matches),
        "to_review": sum(1 for p in parties if p.matches and not p.reviewed_at),
        "missing_disclosure": sum(1 for p in parties if p.disclosed == "missing"),
        "has_ledger": any(u.kind == "gl" for u in e.uploads),
    }


def list_out(e: Engagement) -> dict:
    return {"parties": [party_out(p) for p in e.related_parties], "summary": summary(e),
            "relationships": [{"id": k, "label": v} for k, v in RELATIONSHIPS.items()],
            "disclosed": [{"id": k, "label": v} for k, v in DISCLOSED.items()],
            "ledgers": [{"id": u.id, "filename": u.filename} for u in e.uploads if u.kind == "gl"]}


def latest_ledger(e: Engagement) -> Upload | None:
    gls = [u for u in e.uploads if u.kind == "gl"]
    return max(gls, key=lambda u: (u.uploaded_at, u.id)) if gls else None


def scan(e: Engagement, gl: Upload, parties: list[RelatedParty] | None = None) -> list[dict]:
    """Each party with the ledger lines that name it."""
    loaded = load_upload(gl)
    df = loaded.df
    text = pd.Series([""] * len(df), index=df.index)
    for col in ("description", "account"):
        if col in df:
            text = text + " " + df[col].fillna("").astype(str)
    if loaded.unmapped is not None:  # an Account Name beside the Account Code, say
        others = loaded.unmapped.reindex(df["row"].to_numpy())
        for col in others.columns:
            text = text + " " + others[col].fillna("").astype(str).to_numpy()
    norm = text.map(normalize)
    near_from, near_to = e.period_end - timedelta(days=NEAR_DAYS), e.period_end + \
        timedelta(days=NEAR_DAYS)
    out = []
    for p in (e.related_parties if parties is None else parties):
        keys = patterns(p)
        hits = norm[norm.map(lambda t, keys=keys: matches_name(t, keys))] if keys else norm[:0]
        rows = []
        for i in hits.index:
            r = df.loc[i]
            amount = float(r["amount"])
            day = r["date"].date() if "date" in df and r["date"] == r["date"] else None
            flags = []
            if day and near_from <= day <= near_to:
                flags.append("Near the period end")
            if abs(amount) >= ROUND and abs(amount) % ROUND == 0:
                flags.append("Round amount")
            rows.append({"row": int(r["row"]), "date": day.isoformat() if day else None,
                         "entry_id": cell(r.get("entry_id")), "account": cell(r.get("account")),
                         "description": cell(r.get("description")),
                         "amount": round(amount, 2), "flags": flags})
        rows.sort(key=lambda x: -abs(x["amount"]))
        out.append({"party": p, "rows": rows,
                    "debits": round(sum(x["amount"] for x in rows if x["amount"] > 0), 2),
                    "credits": round(-sum(x["amount"] for x in rows if x["amount"] < 0), 2)})
    return out


def fingerprint(gl: Upload, rows: list[dict]) -> str:
    """The upload (which file, sheet and column mapping) and the lines found in it: a
    different set needs a fresh review."""
    lines = sorted((r["row"], r["date"], r["entry_id"], r["amount"]) for r in rows)
    source = json.dumps([gl.id, gl.sha256, gl.sheet, gl.mapping], sort_keys=True, default=str)
    return hashlib.sha256(f"{source}|{lines}".encode()).hexdigest()


def still_matches(p: RelatedParty, gl: Upload, rows: list[dict]) -> bool:
    """Whether a fresh scan of the ledger finds what the party's saved scan found. Scans saved
    before fingerprints were kept can only be checked by their count."""
    if not p.scan_fingerprint:
        return len(rows) == p.matches
    return fingerprint(gl, rows) == p.scan_fingerprint


def scan_out(results: list[dict], gl: Upload) -> dict:
    return {"upload_id": gl.id, "filename": gl.filename, "parties": [
        {"id": x["party"].id, "name": x["party"].name, "matches": len(x["rows"]),
         "debits": x["debits"], "credits": x["credits"],
         "flagged": sum(1 for r in x["rows"] if r["flags"]),
         "rows": x["rows"][:MAX_SHOWN]} for x in results]}


def pick_ledger(session: Session, user: User, e: Engagement, upload_id: int | None) -> Upload:
    if upload_id is not None:
        gl = get_or_404(session, Upload, upload_id, user)
        if gl.engagement_id != e.id or gl.kind != "gl":
            raise HTTPException(400, "Pick a general ledger uploaded to this engagement.")
        return gl
    gl = latest_ledger(e)
    if gl is None:
        raise HTTPException(400, "Upload the general ledger first.")
    return gl


@router.get("/related-parties/options")
def options() -> dict:
    return {"relationships": [{"id": k, "label": v} for k, v in RELATIONSHIPS.items()],
            "disclosed": [{"id": k, "label": v} for k, v in DISCLOSED.items()]}


@router.get("/engagements/{engagement_id}/related-parties")
def list_parties(engagement_id: int, user: User = Depends(current_user),
                 session: Session = Depends(get_session)) -> dict:
    return list_out(get_or_404(session, Engagement, engagement_id, user))


def held(session: Session, user: User, party_id: int) -> RelatedParty:
    engagement_id = get_or_404(session, RelatedParty, party_id, user).engagement_id
    auth.hold(session, f"related:{engagement_id}")
    session.expire_all()
    return get_or_404(session, RelatedParty, party_id, user)


def check_unique(e: Engagement, name: str, skip: int | None = None) -> None:
    if any(normalize(p.name) == normalize(name) and p.id != skip for p in e.related_parties):
        raise HTTPException(400, f"{name} is already in the register.")


@router.post("/engagements/{engagement_id}/related-parties", status_code=201)
def create_party(engagement_id: int, body: PartyIn, user: User = Depends(require("prepare")),
                 session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"related:{engagement_id}")
    if len(e.related_parties) >= MAX_PARTIES:
        raise HTTPException(400, f"An engagement can have at most {MAX_PARTIES} related "
                                 "parties.")
    check_unique(e, body.name)
    p = RelatedParty(engagement=e, name=body.name, relationship_kind=body.relationship,
                     aliases=body.aliases, note=body.note, source=body.source,
                     created_by=user.name, updated_by=user.name)
    session.add(p)
    session.flush()
    log(session, user, "related.create", f"Added {p.name} to the related parties", e,
        details={"party_id": p.id, "relationship": p.relationship_kind, "source": p.source})
    session.commit()
    return list_out(e)


@router.patch("/related-parties/{party_id}")
def update_party(party_id: int, body: PartyPatch, user: User = Depends(require("prepare")),
                 session: Session = Depends(get_session)) -> dict:
    p = held(session, user, party_id)
    e = p.engagement
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if "name" in changes:
        check_unique(e, changes["name"], skip=p.id)
    renamed = {"name", "aliases"} & set(changes)
    if renamed and (p.matches or p.reviewed_at) and not auth.can(user, "review"):
        raise HTTPException(403, "Only a reviewer can change the names of a party with "
                                 "transactions found or reviewed.")
    before = patterns(p)
    for field in ("name", "aliases", "note", "conclusion", "disclosed"):
        if field in changes:
            setattr(p, field, changes[field])
    if "relationship" in changes:
        p.relationship_kind = changes["relationship"]
    if patterns(p) != before:  # different names: last scan's counts no longer apply
        forget_scan(p)
    if changes.get("reviewed") is True:
        if p.matches and not p.conclusion:
            raise HTTPException(400, "Write a conclusion on the transactions before marking "
                                     "them reviewed.")
        p.reviewed_by, p.reviewed_at = user.name, utcnow()
    elif changes.get("reviewed") is False:
        p.reviewed_by, p.reviewed_at = "", None
    elif p.reviewed_at and p.matches and not p.conclusion.strip():
        p.reviewed_by, p.reviewed_at = "", None  # the conclusion the review rested on is gone
    p.updated_by, p.updated_at = user.name, utcnow()
    log(session, user, "related.update", f"Changed the related party {p.name}", e,
        details={"party_id": p.id, "changed": sorted(changes), "disclosed": p.disclosed,
                 "reviewed": bool(p.reviewed_at)})
    session.commit()
    return list_out(e)


@router.delete("/related-parties/{party_id}")
def delete_party(party_id: int, user: User = Depends(require("prepare")),
                 session: Session = Depends(get_session)) -> dict:
    p = held(session, user, party_id)
    e = p.engagement
    if (p.reviewed_at or p.matches) and not auth.can(user, "review"):
        raise HTTPException(403, "Only a reviewer can remove a party with transactions found "
                                 "or reviewed.")
    log(session, user, "related.delete", f"Removed {p.name} from the related parties", e,
        details={"party_id": p.id, "matches": p.matches})
    session.delete(p)
    session.commit()
    session.refresh(e)
    return list_out(e)


@router.post("/engagements/{engagement_id}/related-parties/scan")
def scan_ledger(engagement_id: int, body: ScanIn, user: User = Depends(require("prepare")),
                session: Session = Depends(get_session)) -> dict:
    e = held_engagement(session, user, engagement_id, f"related:{engagement_id}")
    if not e.related_parties:
        raise HTTPException(400, "Add the related parties first.")
    gl = pick_ledger(session, user, e, body.upload_id)
    results = scan(e, gl)
    now = utcnow()
    for x in results:
        p = x["party"]
        mark = fingerprint(gl, x["rows"])
        if mark != p.scan_fingerprint:
            p.reviewed_by, p.reviewed_at = "", None  # other ledger or lines: a fresh look
        p.scanned_at, p.matches = now, len(x["rows"])
        p.matched_amount = x["debits"] + x["credits"]
        p.scan_upload_id, p.scan_fingerprint = gl.id, mark
    log(session, user, "related.scan", f"Scanned {gl.filename} for related party names", e,
        details={"upload_id": gl.id, "parties": len(results),
                 "matches": sum(len(x["rows"]) for x in results)})
    session.commit()
    return {**list_out(e), "scan": scan_out(results, gl)}


# --- Excel -------------------------------------------------------------------------

def to_excel(e: Engagement, results: list[dict] | None, gl: Upload | None,
             stale: list[str] | None = None) -> bytes:
    s = summary(e)
    about = [("Client", e.client.name), ("Engagement", e.name),
             ("Period end", e.period_end.isoformat()), ("Related parties", s["parties"]),
             ("With transactions found", s["with_matches"]),
             ("Ledger scanned", gl.filename if gl else "Not scanned")]
    if stale:
        about.append(("Not listed", "The ledger no longer gives the transactions the last "
                                    "scan found for: " + ", ".join(stale)))
    parties = [{"Name": p.name, "Relationship": RELATIONSHIPS.get(p.relationship_kind, ""),
                "Other names": ", ".join(p.aliases or []), "Note": p.note,
                "Transactions found": p.matches,
                "Debits plus credits": round(p.matched_amount, 2),
                "Conclusion": p.conclusion, "Reviewed by": p.reviewed_by,
                "Disclosure": DISCLOSED.get(p.disclosed, "")} for p in e.related_parties]
    rows = [{"Party": x["party"].name, "Row": r["row"], "Date": r["date"] or "",
             "Entry": r["entry_id"], "Account": r["account"], "Description": r["description"],
             "Amount": r["amount"], "Flags": ", ".join(r["flags"])}
            for x in results or [] for r in x["rows"]]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame(parties, columns=["Name", "Relationship", "Other names", "Note",
                                       "Transactions found", "Debits plus credits",
                                       "Conclusion", "Reviewed by", "Disclosure"]).to_excel(
            xl, sheet_name="Related parties", index=False)
        pd.DataFrame(rows, columns=["Party", "Row", "Date", "Entry", "Account", "Description",
                                    "Amount", "Flags"]).to_excel(
            xl, sheet_name="Transactions", index=False)
        finish_workbook(xl.book)
    return buf.getvalue()


def scanned_ledger(e: Engagement) -> Upload | None:
    """The ledger of the latest scan, if it is still uploaded."""
    done = [p for p in e.related_parties if p.scanned_at and p.scan_upload_id]
    if not done:
        return None
    last = max(done, key=lambda p: p.scanned_at).scan_upload_id
    return next((u for u in e.uploads if u.id == last), None)


def workbook(e: Engagement) -> tuple[str, bytes]:
    """The register, with the transactions found by the last scan: the same ledger and the
    same parties its counts, conclusions and reviews rest on."""
    gl = scanned_ledger(e)
    results, stale = None, []
    if gl is not None:
        try:
            found = scan(e, gl, [p for p in e.related_parties if p.scan_upload_id == gl.id])
        except HTTPException:
            gl, found = None, []  # a ledger that no longer loads: the register on its own
        results = [x for x in found if still_matches(x["party"], gl, x["rows"])]
        stale = [x["party"].name for x in found if x not in results]
    stem = safe_stem(f"{e.client.name}-{e.name}-related-parties")
    return f"{stem}.xlsx", to_excel(e, results, gl, stale)


@router.get("/engagements/{engagement_id}/related-parties/export")
def export_parties(engagement_id: int, user: User = Depends(current_user),
                   session: Session = Depends(get_session)) -> Response:
    e = get_or_404(session, Engagement, engagement_id, user)
    name, content = workbook(e)
    log(session, user, "related.download", "Downloaded the related parties workpaper", e,
        details={"parties": len(e.related_parties)})
    session.commit()
    return Response(content, media_type=XLSX, headers=attachment(name))


def known_names(e: Engagement) -> dict[str, str]:
    """Each searchable name (normalized) to the party it belongs to, for summaries.py."""
    return {k: p.name for p in e.related_parties for k in patterns(p)}


def find_party(known: dict[str, str], name: str) -> str | None:
    k = normalize(name)
    if not k:
        return None
    # Only a register name found within the document's name: "Ali" alone isn't "Ali Hassan".
    return next((v for key, v in known.items() if matches_name(k, [key])), None)
