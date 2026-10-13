"""Load a trial balance or general ledger file into a normalized DataFrame.

Normalized columns (only those found in the source are present):
    row        1-based row number in the source file (header is row 1)
    entry_id   journal entry / document number
    date       transaction date
    posted     posting date (used for cutoff testing)
    account    account number or name
    description
    user       who created / posted the entry
    debit, credit
    amount     signed amount (debit positive, credit negative)

Source columns are matched to these fields either by an explicit mapping chosen by
the user, or by guessing from common header names (see COLUMN_SYNONYMS).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import pandas as pd

# Fields the user can map, in the order the mapping screen shows them.
FIELDS: list[dict] = [
    {"id": "date", "label": "Transaction date"},
    {"id": "posted", "label": "Posting date"},
    {"id": "account", "label": "Account"},
    {"id": "entry_id", "label": "Entry / document number"},
    {"id": "description", "label": "Description"},
    {"id": "user", "label": "User"},
    {"id": "amount", "label": "Amount (signed)"},
    {"id": "debit", "label": "Debit"},
    {"id": "credit", "label": "Credit"},
]
FIELD_IDS = [f["id"] for f in FIELDS]

# Source header spellings for each normalized column, compared after lowercasing
# and stripping everything that is not a letter or digit.
COLUMN_SYNONYMS: dict[str, list[str]] = {
    "entry_id": ["entryid", "entry", "entryno", "entrynumber", "journalid", "journal",
                 "journalno", "journalnumber", "je", "jeno", "jenumber", "jv", "jvno", "jvnumber",
                 "journalentry", "journalentryid", "documentno", "documentnumber",
                 "docno", "voucher", "voucherno", "transactionid", "txnid", "reference", "ref"],
    "date": ["date", "transactiondate", "txndate", "entrydate", "effectivedate",
             "documentdate", "gldate", "accountingdate"],
    "posted": ["posteddate", "postingdate", "posted", "postdate", "createddate", "enteredon",
               "entereddate"],
    "account": ["account", "accountno", "accountnumber", "accountcode", "glaccount",
                "glaccountnumber", "acct", "acctno", "accountname", "ledgeraccount"],
    "description": ["description", "memo", "narration", "narrative", "details", "text",
                    "linedescription", "comment", "accountname", "accountdescription", "name"],
    "user": ["user", "createdby", "postedby", "enteredby", "preparer", "username", "userid"],
    "debit": ["debit", "debits", "dr", "debitamount"],
    "credit": ["credit", "credits", "cr", "creditamount"],
    "amount": ["amount", "value", "netamount", "balance", "amt", "signedamount"],
}


class LoadError(ValueError):
    """Raised when an uploaded file can't be turned into ledger data."""


@dataclass
class ImportReport:
    """What happened while importing, so the user can check nothing was lost."""

    rows_read: int = 0
    rows_used: int = 0
    skipped_rows: list[int] = field(default_factory=list)  # source row numbers
    total_rows: list[int] = field(default_factory=list)
    bad_dates: dict[str, list[int]] = field(default_factory=dict)
    both_sides_rows: list[int] = field(default_factory=list)
    total_debits: float = 0.0
    total_credits: float = 0.0
    mapping: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        warnings = []
        if self.skipped_rows:
            warnings.append(
                f"{len(self.skipped_rows)} rows skipped because the amount is blank or not a "
                f"number (rows {_row_list(self.skipped_rows)})."
            )
        if self.total_rows:
            warnings.append(
                f"{len(self.total_rows)} rows left out because they look like totals: they "
                f"have an amount but no account, entry or date (rows {_row_list(self.total_rows)})."
            )
        for col, rows in self.bad_dates.items():
            warnings.append(
                f"{len(rows)} rows have a {col} that couldn't be read as a date "
                f"(rows {_row_list(rows)})."
            )
        if self.both_sides_rows:
            warnings.append(
                f"{len(self.both_sides_rows)} rows have both a debit and a credit "
                f"(rows {_row_list(self.both_sides_rows)}); they were netted."
            )
        return {
            "rows_read": self.rows_read,
            "rows_used": self.rows_used,
            "rows_skipped": len(self.skipped_rows) + len(self.total_rows),
            "total_debits": round(self.total_debits, 2),
            "total_credits": round(self.total_credits, 2),
            "mapping": self.mapping,
            "warnings": warnings,
        }


@dataclass
class Loaded:
    df: pd.DataFrame
    report: ImportReport


def _row_list(rows: list[int], limit: int = 10) -> str:
    shown = ", ".join(str(r) for r in rows[:limit])
    return shown + (f" and {len(rows) - limit} more" if len(rows) > limit else "")


def _key(name: object) -> str:
    """A header reduced for matching: lowercase letters and digits, without anything in
    brackets (so "Debit (EGP)" matches "debit")."""
    return re.sub(r"[^a-z0-9]", "", re.sub(r"[(\[{].*?[)\]}]", "", str(name).lower()))


HEADER_SEARCH_ROWS = 30  # how far down a report's title rows can push the header


def _header_score(cells) -> int:
    keys = {_key(c) for c in cells if isinstance(c, str)}
    return sum(1 for synonyms in COLUMN_SYNONYMS.values() if keys & set(synonyms))


def _header_row(top: pd.DataFrame) -> int:
    """The row holding the column headers, 0-based. Exports from accounting systems often
    start with a few title rows (company, report name, period); the header is the first row
    that names the most known columns, if it names at least two. Otherwise the first row."""
    best, best_score = 0, 1
    for i, cells in enumerate(top.itertuples(index=False)):
        score = _header_score(cells)
        if score > best_score:
            best, best_score = i, score
    return best


def _csv_top(content: bytes) -> pd.DataFrame:
    """The first rows of a CSV, split on its delimiter (the most common of , ; tab |).
    csv.Sniffer gives up on files whose title rows have no delimiters, so it isn't used."""
    import csv

    text = content[:256 * 1024].decode("utf-8-sig", errors="replace")
    lines = text.splitlines()[:HEADER_SEARCH_ROWS]
    delimiter = max(",;\t|", key=lambda d: sum(line.count(d) for line in lines))
    return pd.DataFrame(list(csv.reader(lines, delimiter=delimiter)))


def sheet_names(filename: str, content: bytes) -> list[str]:
    if not filename.lower().endswith((".xlsx", ".xlsm", ".xls")):
        return []
    try:
        return [str(s) for s in pd.ExcelFile(io.BytesIO(content)).sheet_names]
    except Exception as exc:
        raise LoadError(f"Could not read {filename}: {exc}") from exc


def read_file(filename: str, content: bytes, sheet: str | None = None) -> pd.DataFrame:
    """Read CSV or Excel bytes into a raw DataFrame with string column names."""
    lower = filename.lower()
    try:
        if lower.endswith((".xlsx", ".xlsm", ".xls")):
            top = pd.read_excel(io.BytesIO(content), sheet_name=sheet or 0, header=None,
                                nrows=HEADER_SEARCH_ROWS)
            header = _header_row(top)
            raw = pd.read_excel(io.BytesIO(content), sheet_name=sheet or 0, header=header)
        elif lower.endswith((".csv", ".txt")):
            header = _header_row(_csv_top(content))
            raw = pd.read_csv(io.BytesIO(content), sep=None, engine="python", skiprows=header)
        else:
            raise LoadError("Unsupported file type. Upload a .csv or .xlsx file.")
    except LoadError:
        raise
    except ValueError as exc:
        if sheet and "not found" in str(exc).lower():
            raise LoadError(f"Sheet {sheet!r} not found in {filename}.") from exc
        raise LoadError(f"Could not read {filename}: {exc}") from exc
    except Exception as exc:  # pandas raises many different error types
        raise LoadError(f"Could not read {filename}: {exc}") from exc
    raw.columns = [str(c).strip() for c in raw.columns]
    raw.attrs["header_row"] = header
    return raw


def suggest_mapping(columns: list[str]) -> dict[str, str]:
    """Guess which source column holds each field, from common header names. Closer matches
    win: with "Account Code", "Account Name" and "Narration", the account is the code and the
    description the narration."""
    candidates = sorted((synonyms.index(_key(col)), pos, target, col)
                        for pos, col in enumerate(columns)
                        for target, synonyms in COLUMN_SYNONYMS.items() if _key(col) in synonyms)
    mapping: dict[str, str] = {}
    for _, _, target, col in candidates:
        if target not in mapping and col not in mapping.values():
            mapping[target] = col
    return mapping


def validate_mapping(mapping: dict[str, str], columns: list[str]) -> dict[str, str]:
    """Drop empty entries and check the rest name real columns and known fields."""
    clean = {f: c for f, c in mapping.items() if c}
    unknown_fields = sorted(set(clean) - set(FIELD_IDS))
    if unknown_fields:
        raise LoadError(f"Unknown field(s) in mapping: {', '.join(unknown_fields)}.")
    missing = sorted({c for c in clean.values() if c not in columns})
    if missing:
        raise LoadError(f"Column(s) not in the file: {', '.join(missing)}.")
    reused = sorted({c for c in clean.values() if list(clean.values()).count(c) > 1})
    if reused:
        raise LoadError(f"Each column can only be mapped once: {', '.join(reused)}.")
    if "amount" not in clean and "debit" not in clean and "credit" not in clean:
        raise LoadError(
            "No amount column found. Map an Amount column, or Debit and Credit columns."
        )
    return clean


DATE_PARTS = re.compile(r"^\s*(\d{1,2})[/.\-](\d{1,2})[/.\-]\d{2,4}\b")


def _day_first(columns: list[pd.Series]) -> bool:
    """Whether dates written like 03/04/2026 put the day first. Decided by the dates that
    can only be read one way (13/04/2026 or 04/13/2026); day first if nothing settles it,
    since that's how most of the world writes dates."""
    if not columns:
        return False
    parts = pd.concat([c.astype("string").str.extract(DATE_PARTS) for c in columns]).dropna()
    if parts.empty:
        return False  # no such dates: ISO dates and real spreadsheet dates are unambiguous
    first, second = parts[0].astype(int), parts[1].astype(int)
    return bool((first > 12).any() or not (second > 12).any())


def _whole_numbers(series: pd.Series) -> pd.Series:
    """A blank cell makes pandas read a column of codes as floats (1000.0); keep 1000."""
    if pd.api.types.is_float_dtype(series):
        codes = series.dropna()
        if (codes == codes.round()).all() and codes.abs().lt(2**53).all():
            return series.astype("Int64")
    return series


def _to_number(series: pd.Series) -> pd.Series:
    """Parse amounts like '1,234.50', '(500.00)' and '$ 75' into floats."""
    if pd.api.types.is_numeric_dtype(series):
        return series.astype(float)
    text = series.astype("string").str.strip()
    negative = (text.str.match(r"^\(.*\)$") | text.str.endswith("-")).fillna(False)
    cleaned = text.str.replace(r"[^0-9.\-]", "", regex=True).str.rstrip("-")
    numbers = pd.to_numeric(cleaned.replace("", None), errors="coerce").astype(float)
    return numbers.where(~negative, -numbers.abs())


def normalize(raw: pd.DataFrame, mapping: dict[str, str] | None = None) -> Loaded:
    """Map source columns onto the normalized schema."""
    header_row = raw.attrs.get("header_row", 0)
    raw = raw.dropna(how="all")
    columns = list(raw.columns)
    chosen = validate_mapping(mapping if mapping is not None else suggest_mapping(columns), columns)
    report = ImportReport(rows_read=len(raw), mapping=chosen)

    df = pd.DataFrame({f: raw[c] for f, c in chosen.items()}, index=raw.index)
    df.insert(0, "row", raw.index.to_numpy() + header_row + 2)  # as numbered in the source

    for col in ("debit", "credit", "amount"):
        if col in df:
            df[col] = _to_number(df[col])

    if "amount" not in df:
        debit = df["debit"] if "debit" in df else pd.Series(float("nan"), index=df.index)
        credit = df["credit"] if "credit" in df else pd.Series(float("nan"), index=df.index)
        report.both_sides_rows = df.loc[(debit.fillna(0) != 0) & (credit.fillna(0) != 0),
                                        "row"].tolist()
        amount = debit.fillna(0) - credit.fillna(0)
        df["amount"] = amount.where(debit.notna() | credit.notna())

    dayfirst = _day_first([df[c] for c in ("date", "posted") if c in df])
    for col, label in (("date", "transaction date"), ("posted", "posting date")):
        if col in df:
            parsed = pd.to_datetime(df[col], errors="coerce", format="mixed", dayfirst=dayfirst)
            bad = parsed.isna() & df[col].notna()
            if bad.any():
                report.bad_dates[label] = df.loc[bad, "row"].tolist()
            df[col] = parsed

    for col in ("entry_id", "account"):
        if col in df:
            df[col] = _whole_numbers(df[col])

    for col in ("entry_id", "account", "description", "user"):
        if col in df:
            df[col] = df[col].astype("string").str.strip()

    # A total or subtotal line has an amount but no account, entry or date, and says "Total"
    # (or nothing) where the description goes.
    keys = [c for c in ("account", "entry_id", "date") if c in df]
    if keys:
        unnamed = pd.concat([df[c].isna() | (df[c].astype("string").str.strip() == "")
                             for c in keys], axis=1).all(axis=1)
        if "description" in df:
            text = df["description"].astype("string").str.strip().fillna("")
            unnamed &= (text == "") | text.str.contains("total", case=False, regex=False)
        report.total_rows = df.loc[unnamed & df["amount"].notna(), "row"].tolist()
        report.both_sides_rows = [r for r in report.both_sides_rows if r not in report.total_rows]
        df = df[~unnamed | df["amount"].isna()]
    report.skipped_rows = df.loc[df["amount"].isna(), "row"].tolist()
    df = df[df["amount"].notna()].reset_index(drop=True)
    if df.empty:
        raise LoadError("The file has no rows with a numeric amount.")
    report.rows_used = len(df)
    report.total_debits = float(df.loc[df["amount"] > 0, "amount"].sum())
    report.total_credits = float(-df.loc[df["amount"] < 0, "amount"].sum())
    return Loaded(df, report)


def preview(filename: str, content: bytes, sheet: str | None = None, rows: int = 5) -> dict:
    """Columns, a few sample rows and a suggested mapping, for the mapping screen."""
    sheets = sheet_names(filename, content)
    raw = read_file(filename, content, sheet).dropna(how="all")
    sample = raw.head(rows).apply(_whole_numbers).astype("string").fillna("")
    return {
        "sheets": sheets,
        "sheet": sheet or (sheets[0] if sheets else None),
        "columns": list(raw.columns),
        "rows": sample.values.tolist(),
        "row_count": len(raw),
        "fields": FIELDS,
        "suggested": suggest_mapping(list(raw.columns)),
    }


def load(filename: str, content: bytes, mapping: dict[str, str] | None = None,
         sheet: str | None = None) -> Loaded:
    return normalize(read_file(filename, content, sheet), mapping)
