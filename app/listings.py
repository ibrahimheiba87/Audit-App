"""Reading registers and listings whose columns the auditor picks: a fixed asset register or
an inventory listing. They are uploaded as lists (kept as they are), and each analysis says
which of its fields each column holds, guessed from the headers until someone picks."""

from __future__ import annotations

import pandas as pd
from fastapi import HTTPException

from . import loader
from .db import Upload


def guess(columns: list[str], synonyms: dict[str, list[str]]) -> dict[str, str]:
    """field -> column, for each field whose header is a known spelling. A column is used
    once, for the first field (in `synonyms` order) that claims it."""
    keyed = {loader._key(c): c for c in columns}
    out: dict[str, str] = {}
    for field, names in synonyms.items():
        for name in names:
            col = keyed.get(name)
            if col is not None and col not in out.values():
                out[field] = col
                break
    return out


def columns_for(raw: pd.DataFrame, picked: dict[str, str] | None,
                synonyms: dict[str, list[str]]) -> dict[str, str]:
    """The columns to use: those picked (blank ones left out), else the guesses."""
    cols = [str(c) for c in raw.columns]
    if not picked:
        return guess(cols, synonyms)
    out = {k: v for k, v in picked.items() if v}
    unknown = [v for v in out.values() if v not in cols]
    if unknown:
        raise HTTPException(400, f"The file has no column called {unknown[0]}.")
    if set(out) - set(synonyms):
        raise HTTPException(400, "Unknown field.")
    return out


def read(upload: Upload, kinds: tuple[str, ...], what: str) -> pd.DataFrame:
    if upload.kind not in kinds:
        raise HTTPException(400, f"Pick {what}.")
    try:
        raw = loader.read_file(upload.filename, upload.content, upload.sheet)
    except loader.LoadError as exc:
        raise HTTPException(400, str(exc)) from exc
    raw = raw.dropna(how="all")
    if raw.empty:
        raise HTTPException(400, f"{upload.filename} has no rows.")
    # Row numbers as in the file (the header is row 1), to find a line again.
    raw.insert(0, "__row", range(2, len(raw) + 2))
    return raw


def text(raw: pd.DataFrame, col: str | None) -> pd.Series:
    if not col:
        return pd.Series("", index=raw.index, dtype=object)
    return raw[col].astype("string").str.strip().fillna("").astype(object)


def numbers(raw: pd.DataFrame, col: str | None) -> pd.Series:
    if not col:
        return pd.Series(float("nan"), index=raw.index)
    return loader._to_number(raw[col])


def dates(raw: pd.DataFrame, cols: list[str | None]) -> list[pd.Series]:
    """Each column read as dates, with one day/month order for them all."""
    present = [raw[c] for c in cols if c]
    dayfirst = loader._day_first(present)
    return [loader._to_dates(raw[c], dayfirst) if c
            else pd.Series(pd.NaT, index=raw.index, dtype="datetime64[ns]") for c in cols]
