"""Shared helpers for files the app hands out: spreadsheet safety and download headers.

Exports contain text from uploaded files and from users (descriptions, notes, filenames).
A cell like `=HYPERLINK(...)` or `+cmd|...` would run as a formula when the file is opened,
so every export goes through these helpers. None of our exports contain real formulas.
"""

from __future__ import annotations

from urllib.parse import quote

import pandas as pd

# Characters that make Excel, LibreOffice or Google Sheets treat a value as a formula.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def finish_workbook(book) -> None:
    """Store any formula-looking value as plain text, and size the columns to fit."""
    for ws in book.worksheets:
        for column in ws.columns:
            for cell in column:
                if cell.data_type == "f":  # openpyxl turns strings starting with "=" into formulas
                    cell.data_type = "s"
            width = max(len(str(c.value or "")) for c in column)
            ws.column_dimensions[column[0].column_letter].width = min(max(width + 2, 10), 60)


def csv_safe(value):
    """Prefix a formula-looking string with an apostrophe so spreadsheets show it as text."""
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def csv_safe_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == object or pd.api.types.is_string_dtype(out[col]):
            out[col] = out[col].map(csv_safe)
    return out


def attachment(filename: str) -> dict[str, str]:
    """A Content-Disposition header that works for any filename (RFC 6266 / RFC 5987).

    Headers must be Latin-1, so non-ASCII names go in `filename*` as UTF-8, with an ASCII
    fallback in `filename` for old clients.
    """
    clean = "".join(ch for ch in filename if ch.isprintable() and ch not in '"\\/')
    clean = clean.strip() or "download"
    fallback = "".join(ch if ch.isascii() else "_" for ch in clean)
    return {"Content-Disposition":
            f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(clean, safe='')}"}
