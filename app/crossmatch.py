"""Matching two files on chosen columns, to find rows that shouldn't have anything in common:
vendors sharing an employee's bank account, address or phone number, customers that are also
vendors, or the same supplier entered twice in one master file.

Each pair of columns is matched one of three ways: the same text (ignoring case, spaces and
punctuation), the same digits (phone numbers, IDs, bank accounts written with or without
separators), or similar names in any word order. Two rows match if any pair of columns does;
the rows matched on the most columns come first.
"""

from __future__ import annotations

import io
import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Annotated, Literal

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.orm import Session

from . import auth, loader
from .auth import current_user, log, require
from .db import CrossMatch, Engagement, Upload, User, get_session
from .engagements import cross_match_brief, get_or_404, safe_stem
from .exports import attachment, finish_workbook

router = APIRouter(prefix="/api")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

METHODS = {
    "exact": "Same text",
    "digits": "Same digits",
    "name": "Similar names",
}
MAX_KEYS = 5
MAX_MATCHES = 5000   # pairs kept; more than this and the columns don't tell rows apart
MAX_SHOWN = 500      # pairs sent to the page; the Excel download has them all
MAX_COLUMNS = 30     # columns of each matched row kept, to show beside the match
MAX_CELL = 200
COMMON_VALUE = 50    # a value in more rows than this ("N/A", "Riyadh") is a placeholder or a
                     # category, not something two rows could share by accident; it's skipped
MAX_PAIRS = 100_000  # pairs found on one column before giving up on it
COMMON_TOKEN = 200   # a word in more rows than this ("trading") doesn't suggest a match
MAX_CANDIDATES = 2000
MAX_COMPARISONS = 3_000_000  # name comparisons per column, which keeps a match under a minute
# Words that say what kind of business it is, not which one.
NAME_NOISE = {
    "the", "and", "of", "co", "company", "ltd", "limited", "llc", "inc", "incorporated",
    "corp", "corporation", "plc", "llp", "lp", "sa", "sarl", "gmbh", "bv", "ag", "est",
    "establishment", "group", "holding", "holdings", "trading", "mr", "mrs", "ms", "dr",
    "wll", "fze", "fzco", "fzc", "fzllc", "spc", "pjsc", "psc", "jsc",
    "شركة", "مؤسسة", "للتجارة", "التجارية", "المحدودة",
}


class MatchError(ValueError):
    pass


Column = Annotated[str, StringConstraints(min_length=1, max_length=255)]


class KeyIn(BaseModel):
    left: Column
    right: Column
    method: Literal["exact", "digits", "name"] = "exact"


class CrossMatchIn(BaseModel):
    left_upload_id: int
    right_upload_id: int
    keys: list[KeyIn] = Field(min_length=1, max_length=MAX_KEYS)
    threshold: int = Field(default=90, ge=70, le=100)  # name similarity, as a percentage
    title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = ""


# --- Matching -------------------------------------------------------------------------


def _text(v) -> str:
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)  # an account number read as 12345.0
    return str(v).strip()


def normalize(value, method: str) -> str:
    """The form two values are compared in; blank if there's too little to match on."""
    s = _text(value).casefold()
    if method == "digits":
        out = re.sub(r"\D+", "", s)
        return out if len(out) >= 4 else ""
    if method == "name":
        # "L.L.C." is one word, and single letters ("ذ م م", initials) say little.
        words = [w for w in re.findall(r"\w+", s.replace(".", ""))
                 if len(w) > 1 and w not in NAME_NOISE]
        return " ".join(sorted(words))
    out = re.sub(r"[\W_]+", "", s)
    return out if len(out) >= 3 else ""


def _similar(m: SequenceMatcher, a: str, threshold: int) -> int:
    """How like `a` the matcher's second string is, if at least `threshold` percent."""
    m.set_seq1(a)
    if m.real_quick_ratio() * 100 < threshold or m.quick_ratio() * 100 < threshold:
        return 0
    score = round(m.ratio() * 100)
    return score if score >= threshold else 0


def key_matches(left: list[str], right: list[str], method: str, threshold: int,
                same_file: bool, symmetric: bool = True
                ) -> tuple[dict[tuple[int, int], int], dict]:
    """{(left row, right row): score out of 100} for one pair of normalized columns, and what
    was left out: values too common to mean anything, and rows with too many similar names.

    In one file, a row never matches itself. If both sides are the same column, each pair is
    listed once (lower row first); if they're different columns (one row's phone is another's
    alternate phone), the direction says which row's column matched which."""
    counts_left: dict[str, int] = defaultdict(int)
    for v in left:
        if v:
            counts_left[v] += 1
    index: dict[str, list[int]] = defaultdict(list)
    for j, v in enumerate(right):
        if v:
            index[v].append(j)
    common_values = {v: max(counts_left.get(v, 0), len(rows)) for v, rows in index.items()
                     if max(counts_left.get(v, 0), len(rows)) > COMMON_VALUE}
    common_values.update({v: n for v, n in counts_left.items() if n > COMMON_VALUE})
    for v in common_values:
        index.pop(v, None)
    out: dict[tuple[int, int], int] = {}

    def add(i: int, j: int, score: int) -> None:
        if same_file:
            if i == j:
                return
            if symmetric:
                i, j = min(i, j), max(i, j)
        if score > out.get((i, j), 0):
            out[(i, j)] = score
            if len(out) > MAX_PAIRS:
                raise MatchError(f"More than {MAX_PAIRS:,} pairs of rows match on one column. "
                                 "Pick columns that tell the rows apart.")

    for i, v in enumerate(left):
        if v and v not in common_values:
            for j in index.get(v, ()):
                add(i, j, 100)
    skipped = {"common_values": [{"value": v[:MAX_CELL], "rows": n} for v, n in
                                 sorted(common_values.items(), key=lambda x: -x[1])[:5]],
               "common_value_count": len(common_values), "rows_not_compared": 0}
    if method != "name" or threshold >= 100:
        return out, skipped
    # Similar names: only names sharing an uncommon word are compared, which keeps this
    # quick on large files and still finds "Gulf Supplies" against "Gulf Suplies LLC".
    by_word: dict[str, set[str]] = defaultdict(set)
    for v in index:
        for w in v.split():
            if len(w) >= 3:
                by_word[w].add(v)
    common = {w for w, names in by_word.items()
              if sum(len(index[n]) for n in names) > COMMON_TOKEN}
    budget = MAX_COMPARISONS
    matcher = SequenceMatcher(None, autojunk=False)
    for i, v in enumerate(left):
        if not v or v in common_values:
            continue
        candidates: set[str] = set()
        for w in v.split():
            if len(w) >= 3 and w not in common:
                candidates |= by_word.get(w, set())
        candidates.discard(v)
        if len(candidates) > MAX_CANDIDATES or len(candidates) > budget:
            skipped["rows_not_compared"] += 1
            continue
        budget -= len(candidates)
        matcher.set_seq2(v)  # compared with each candidate in turn
        for c in candidates:
            score = _similar(matcher, c, threshold)
            if score:
                for j in index[c]:
                    add(i, j, score)
    return out, skipped


def shown_columns(raw: pd.DataFrame, keys: list[str]) -> list[str]:
    """The first columns of a file, and the ones matched on wherever they are."""
    cols = list(raw.columns)[:MAX_COLUMNS]
    return cols + [k for k in dict.fromkeys(keys) if k not in cols]


def _rows(raw: pd.DataFrame, cols: list[str]) -> list[dict]:
    return [{c: _text(v)[:MAX_CELL] for c, v in zip(cols, row, strict=True)}
            for row in raw[cols].itertuples(index=False, name=None)]


def match(left: pd.DataFrame, right: pd.DataFrame, keys: list[dict], threshold: int,
          same_file: bool = False) -> dict:
    """Match the rows of two raw tables (as loader.read_file gives them) on `keys`, each
    {"left": column, "right": column, "method": ...}."""
    for k in keys:
        if k["method"] not in METHODS:
            raise MatchError(f"Unknown way of matching: {k['method']}.")
        if k["left"] not in left.columns:
            raise MatchError(f"There's no column {k['left']!r} in the first file.")
        if k["right"] not in right.columns:
            raise MatchError(f"There's no column {k['right']!r} in the second file.")
    pairs: dict[tuple[int, int], dict[int, int]] = defaultdict(dict)
    per_key: list[dict] = [{}] * len(keys)
    # In one file, columns matched against a different column have a direction, so they go
    # first; a column matched against itself holds either way round, and joins those pairs.
    symmetric = [k["left"] == k["right"] for k in keys]
    for n in sorted(range(len(keys)), key=lambda n: symmetric[n]):
        k = keys[n]
        found, skipped = key_matches([normalize(v, k["method"]) for v in left[k["left"]]],
                                     [normalize(v, k["method"]) for v in right[k["right"]]],
                                     k["method"], threshold, same_file, symmetric[n])
        per_key[n] = {"pairs": len(found), **skipped}
        for (i, j), score in found.items():
            if same_file and symmetric[n] and (i, j) not in pairs and (j, i) in pairs:
                i, j = j, i
            pairs[(i, j)][n] = score
    order = sorted(pairs.items(), key=lambda p: (-len(p[1]), -min(p[1].values()), p[0]))
    left_first = int(left.attrs.get("header_row") or 0) + 2  # the row number in the file
    right_first = int(right.attrs.get("header_row") or 0) + 2
    left_cols = shown_columns(left, [k["left"] for k in keys])
    right_cols = shown_columns(right, [k["right"] for k in keys])
    lrows, rrows = _rows(left, left_cols), _rows(right, right_cols)
    left_pos, right_pos = list(left.index), list(right.index)
    matches = [{
        "left_row": int(left_pos[i]) + left_first, "right_row": int(right_pos[j]) + right_first,
        "keys": [{"key": n, "score": s} for n, s in sorted(found.items())],
        "left": lrows[i], "right": rrows[j],
    } for (i, j), found in order[:MAX_MATCHES]]
    return {
        "keys": [{**k, "method_label": METHODS[k["method"]], **c}
                 for k, c in zip(keys, per_key, strict=True)],
        "threshold": threshold, "same_file": same_file,
        "left_columns": left_cols, "right_columns": right_cols,
        "totals": {
            "left_rows": len(left), "right_rows": len(right), "pairs": len(pairs),
            "all_keys": sum(1 for _, f in pairs.items() if len(f) == len(keys)),
            # In one file, a row is matched whichever side of a pair it's on.
            "left_matched": len({r for p in pairs for r in p} if same_file
                                else {i for i, _ in pairs}),
            "right_matched": len({r for p in pairs for r in p} if same_file
                                 else {j for _, j in pairs}),
        },
        "truncated": max(0, len(pairs) - MAX_MATCHES),
        "matches": matches,
    }


def read_raw(upload: Upload) -> pd.DataFrame:
    """Any upload as its columns and rows, as they are in the file."""
    try:
        raw = loader.read_file(upload.filename, upload.content, upload.sheet, as_text=True)
    except loader.LoadError as exc:
        raise HTTPException(400, str(exc)) from exc
    header = raw.attrs.get("header_row")
    raw = raw.dropna(how="all")
    raw.attrs["header_row"] = header
    return raw


def to_excel(m: CrossMatch) -> bytes:
    r = m.result
    keys = r["keys"]

    def key_label(k: dict) -> str:
        return f"{k['left']} = {k['right']} ({k['method_label']})"
    about = [("Client", m.engagement.client.name), ("Engagement", m.engagement.name),
             ("Title", m.title), ("First file", m.left_upload.filename),
             ("Second file", m.right_upload.filename),
             ("Matched by", m.created_by), ("Matched at", m.created_at.isoformat(" ", "minutes")),
             ("Name similarity needed (%)", r["threshold"]),
             ("Rows in the first file", r["totals"]["left_rows"]),
             ("Rows in the second file", r["totals"]["right_rows"]),
             ("Matching pairs", r["totals"]["pairs"]),
             ("Rows matched in the first file", r["totals"]["left_matched"]),
             ("Rows matched in the second file", r["totals"]["right_matched"]),
             ("Pairs matching on every column", r["totals"]["all_keys"])]
    if r["truncated"]:
        about.append(("Pairs not listed (too many)", r["truncated"]))
    rows = []
    for x in r["matches"]:
        on = {k["key"]: k["score"] for k in x["keys"]}
        row = {"First file row": x["left_row"], "Second file row": x["right_row"],
               "Columns matched": len(on),
               "Matched on": "; ".join(f"{key_label(keys[n])}"
                                       + (f" {s}%" if s < 100 else "") for n, s in on.items())}
        row.update({f"1: {c}": v for c, v in x["left"].items()})
        row.update({f"2: {c}": v for c, v in x["right"].items()})
        rows.append(row)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pd.DataFrame(about, columns=["Item", "Value"]).to_excel(xl, sheet_name="About",
                                                                index=False)
        pd.DataFrame([{"First file column": k["left"], "Second file column": k["right"],
                       "Matched by": k["method_label"], "Pairs": k["pairs"],
                       "Values skipped as too common": "; ".join(
                           f"{x['value']} ({x['rows']} rows)" for x in k["common_values"]),
                       "Rows with too many similar names to compare": k["rows_not_compared"]}
                      for k in keys]).to_excel(xl, sheet_name="Columns", index=False)
        pd.DataFrame(rows, columns=None if rows else ["First file row", "Second file row",
                                                      "Columns matched", "Matched on"]
                     ).to_excel(xl, sheet_name="Matches", index=False)
        finish_workbook(xl.book)
    return buf.getvalue()


def trimmed(m: CrossMatch) -> dict:
    r = m.result
    return {**cross_match_brief(m), "engagement_id": m.engagement_id,
            "result": {**{k: v for k, v in r.items() if k != "matches"},
                       "matches": r["matches"][:MAX_SHOWN],
                       "hidden": max(0, len(r["matches"]) - MAX_SHOWN)}}


# --- Routes ---------------------------------------------------------------------------


@router.get("/uploads/{upload_id}/columns")
def upload_columns(upload_id: int, user: User = Depends(current_user),
                   session: Session = Depends(get_session)) -> dict:
    """The columns of a file, as they are in it, and a few rows, to pick what to match on."""
    upload = get_or_404(session, Upload, upload_id, user)
    report = upload.import_report or {}
    if "columns" in report:  # lists keep their columns, so the file needn't be read again
        return {"upload_id": upload.id, "filename": upload.filename,
                "rows": report["rows_used"], "columns": report["columns"]}
    raw = read_raw(upload)
    return {"upload_id": upload.id, "filename": upload.filename, "rows": len(raw),
            "columns": list(raw.columns)}


@router.post("/engagements/{engagement_id}/cross-matches", status_code=201)
def create_cross_match(engagement_id: int, body: CrossMatchIn,
                       user: User = Depends(require("prepare")),
                       session: Session = Depends(get_session)) -> dict:
    e = get_or_404(session, Engagement, engagement_id, user)
    ids = (body.left_upload_id, body.right_upload_id)
    files = []
    for upload_id in ids:
        u = get_or_404(session, Upload, upload_id, user)
        if u.engagement_id != e.id:
            raise HTTPException(400, "Both files must belong to this engagement.")
        files.append(u)
    left, right = files
    same_file = left.id == right.id
    sha = (left.sha256, right.sha256)
    keys = [k.model_dump() for k in body.keys]
    # Matching a large file takes a while, so it's done before anything is held: holding
    # writes a row, and on SQLite that would keep everyone else waiting until it's done.
    left_raw = read_raw(left)
    right_raw = left_raw if same_file else read_raw(right)
    try:
        result = match(left_raw, right_raw, keys, body.threshold, same_file=same_file)
    except MatchError as exc:
        raise HTTPException(400, str(exc)) from exc
    session.commit()  # nothing was written; ends the read so the hold below sees the latest
    # Held like a file deletion holds it, so the match can't be saved onto a deleted file.
    for upload_id in sorted(set(ids)):
        auth.hold(session, f"upload:{upload_id}")
    session.expire_all()
    e = get_or_404(session, Engagement, engagement_id, user)
    left = get_or_404(session, Upload, body.left_upload_id, user)
    right = get_or_404(session, Upload, body.right_upload_id, user)
    if (left.sha256, right.sha256) != sha:
        raise HTTPException(409, "A file changed while it was being matched. Try again.")
    settings = {"keys": keys, "threshold": body.threshold,
                "left_sha256": left.sha256, "right_sha256": right.sha256}
    m = CrossMatch(engagement=e, left_upload=left, right_upload=right, title=body.title,
                   settings=settings, totals=result["totals"], result=result,
                   created_by=user.name)
    session.add(m)
    session.flush()
    what = (f"Looked for duplicates in {left.filename}" if left.id == right.id
            else f"Matched {left.filename} with {right.filename}")
    log(session, user, "crossmatch.create", f"{what}: {result['totals']['pairs']} matches", e,
        details={"match_id": m.id, **settings, "totals": result["totals"]})
    session.commit()
    return trimmed(m)


@router.get("/cross-matches/{match_id}")
def get_cross_match(match_id: int, user: User = Depends(current_user),
                    session: Session = Depends(get_session)) -> dict:
    return trimmed(get_or_404(session, CrossMatch, match_id, user))


@router.get("/cross-matches/{match_id}/export")
def export_cross_match(match_id: int, user: User = Depends(current_user),
                       session: Session = Depends(get_session)) -> Response:
    m = get_or_404(session, CrossMatch, match_id, user)
    log(session, user, "crossmatch.download", f"Downloaded file match #{m.id}", m.engagement,
        details={"match_id": m.id})
    session.commit()
    stem = safe_stem(f"{m.engagement.name}-match-{m.id}")
    return Response(to_excel(m), media_type=XLSX, headers=attachment(f"{stem}.xlsx"))


@router.delete("/cross-matches/{match_id}", status_code=204)
def delete_cross_match(match_id: int, user: User = Depends(require("review")),
                       session: Session = Depends(get_session)) -> Response:
    m = get_or_404(session, CrossMatch, match_id, user)
    e = m.engagement
    log(session, user, "crossmatch.delete", f"Deleted file match #{m.id}", e,
        details={"match_id": m.id, "totals": m.totals})
    session.delete(m)
    session.commit()
    return Response(status_code=204)
