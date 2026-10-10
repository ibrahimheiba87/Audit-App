# Audit App

A small web app for first-pass audit analytics on a general ledger or trial balance.
Upload a CSV or Excel file, run a set of standard audit checks, review the findings in
the browser and export them as an Excel workbook or CSV.

![Screenshot](docs/screenshot.png)

## Checks

| Check | What it flags | Severity |
| --- | --- | --- |
| Debits equal credits | The file doesn't net to zero, or a journal entry doesn't balance | High |
| Duplicate entries | Rows with the same date, account, description and amount | High |
| Period cutoff | Entries dated after period end, dated in the period but posted after it, or in the last *N* days | High / Low |
| Weekend entries | Entries posted (or dated, if there's no posting date) on a Saturday or Sunday | Medium |
| Round-number amounts | Amounts that are exact multiples of the threshold (default 1,000) | Medium |
| Unusually large amounts | Amounts above Q3 + 1.5×IQR of their account's amounts, on a log scale | Medium |
| Benford's law | First-digit distribution of debit amounts vs Benford; when it doesn't conform (Nigrini MAD > 0.015), rows with over-represented leading digits | Low |

Checks that need a column the file doesn't have (for example dates in a trial balance)
are skipped and say why.

## File format

The first row must be a header. Columns are matched by common names, case-insensitively:

| Field | Recognised headers (examples) |
| --- | --- |
| Amount | `Amount`, or `Debit` and `Credit` |
| Date | `Date`, `Transaction Date`, `Entry Date`, `Effective Date` |
| Posting date | `Posted Date`, `Posting Date`, `Created Date` |
| Account | `Account`, `Account No`, `GL Account`, `Account Name` |
| Entry ID | `JE Number`, `Journal ID`, `Document No`, `Voucher`, `Reference` |
| Description | `Description`, `Memo`, `Narration` |
| User | `Created By`, `Posted By`, `User` |

Amounts like `1,234.50`, `(500.00)`, `$75` and `20-` are understood. Only an amount column
is required. `sample_data/sample_gl.csv` is a sample ledger with planted issues
(regenerate it with `python scripts/make_sample.py`).

## Running locally

Requires Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000 and upload `sample_data/sample_gl.csv` to try it.

## API

- `POST /api/audit`: multipart form with `file`, and optional `period_end` (YYYY-MM-DD),
  `cutoff_days`, `round_threshold`. Returns JSON with `summary`, `checks`, `findings` and `benford`.
- `POST /api/report`: same fields plus `format` (`xlsx` or `csv`). Returns the report file.

Uploaded files are processed in memory and not stored.

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```
