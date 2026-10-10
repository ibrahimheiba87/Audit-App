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
"""

from __future__ import annotations

import io
import re

import pandas as pd

# Source header spellings for each normalized column, compared after lowercasing
# and stripping everything that is not a letter or digit.
COLUMN_SYNONYMS: dict[str, list[str]] = {
    "entry_id": ["entryid", "entry", "journalid", "journal", "je", "jeno", "jenumber",
                 "journalentry", "journalentryid", "documentno", "documentnumber",
                 "docno", "voucher", "voucherno", "transactionid", "txnid", "reference", "ref"],
    "date": ["date", "transactiondate", "txndate", "entrydate", "effectivedate",
             "documentdate", "gldate","accountingdate"],
    "posted": ["posteddate", "postingdate", "posted", "postdate", "createddate", "enteredon",
               "entereddate"],
    "account": ["account", "accountno", "accountnumber", "accountcode", "glaccount",
                "glaccountnumber", "acct", "acctno", "accountname", "ledgeraccount"],
    "description": ["description", "memo", "narration", "narrative", "details", "text",
                    "linedescription", "comment"],
    "user": ["user", "createdby", "postedby", "enteredby", "preparer", "username", "userid"],
    "debit": ["debit", "debits", "dr", "debitamount"],
    "credit": ["credit", "credits", "cr", "creditamount"],
    "amount": ["amount", "value", "netamount", "balance", "amt", "signedamount"],
}


class LoadError(ValueError):
    """Raised when an uploaded file can't be turned into ledger data."""


def _key(name: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def read_file(filename: str, content: bytes) -> pd.DataFrame:
    """Read CSV or Excel bytes into a raw DataFrame."""
    lower = filename.lower()
    try:
        if lower.endswith((".xlsx", ".xlsm", ".xls")):
            return pd.read_excel(io.BytesIO(content))
        if lower.endswith((".csv", ".txt")):
            return pd.read_csv(io.BytesIO(content), sep=None, engine="python")
    except Exception as exc:  # pandas raises many different error types
        raise LoadError(f"Could not read {filename}: {exc}") from exc
    raise LoadError("Unsupported file type. Upload a .csv or .xlsx file.")


def _to_number(series: pd.Series) -> pd.Series:
    """Parse amounts like '1,234.50', '(500.00)' and '$ 75' into floats."""
    if pd.api.types.is_numeric_dtype(series):
        return series.astype(float)
    text = series.astype(str).str.strip()
    negative = text.str.match(r"^\(.*\)$") | text.str.endswith("-")
    cleaned = text.str.replace(r"[^0-9.\-]", "", regex=True).str.rstrip("-")
    numbers = pd.to_numeric(cleaned.replace("", None), errors="coerce")
    return numbers.where(~negative, -numbers.abs())


def normalize(raw: pd.DataFrame) -> pd.DataFrame:
    """Map source columns onto the normalized schema."""
    raw = raw.dropna(how="all")
    mapping: dict[str, str] = {}
    for col in raw.columns:
        k = _key(col)
        for target, synonyms in COLUMN_SYNONYMS.items():
            if target not in mapping.values() and k in synonyms:
                mapping[col] = target
                break

    df = raw[list(mapping)].rename(columns=mapping).copy()
    df.insert(0, "row", raw.index.to_numpy() + 2)

    for col in ("debit", "credit", "amount"):
        if col in df:
            df[col] = _to_number(df[col])

    if "amount" not in df:
        if "debit" not in df and "credit" not in df:
            raise LoadError(
                "No amount column found. Expected a column named Amount, or Debit and Credit."
            )
        df["amount"] = df.get("debit", 0).fillna(0) - df.get("credit", 0).fillna(0)

    for col in ("date", "posted"):
        if col in df:
            df[col] = pd.to_datetime(df[col], errors="coerce")

    for col in ("entry_id", "account", "description", "user"):
        if col in df:
            df[col] = df[col].astype("string").str.strip()

    df = df[df["amount"].notna()].reset_index(drop=True)
    if df.empty:
        raise LoadError("The file has no rows with a numeric amount.")
    return df


def load(filename: str, content: bytes) -> pd.DataFrame:
    return normalize(read_file(filename, content))
