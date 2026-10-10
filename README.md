# Audit App

A small web app for first-pass audit analytics on a general ledger or trial balance.
Upload a CSV or Excel file, run a set of standard audit checks, review the findings in
the browser and export them as an Excel workbook or CSV.

![Screenshot](docs/screenshot.png)

## Engagements and materiality

The **Engagements** tab keeps work organised by client and period:

- Add clients, and engagements for each client with a period, period end and framework.
- Set materiality with the calculator: pick a benchmark (profit before tax, revenue, total
  assets, equity or expenses) and a percentage, and it works out overall materiality,
  performance materiality (default 75% of overall) and the clearly trivial threshold
  (default 5%). Record the rationale alongside.
- Upload general ledgers and trial balances to the engagement. Each file is stored with its
  SHA-256 hash and column mapping, so runs can be repeated and traced.
- Run the audit on an upload. Runs use the engagement's period end and performance
  materiality, are saved with their settings, and can be reopened or exported later.

**Prior-year comparison**: upload this year's and last year's trial balances (or ledgers,
which are summed per account) and compare them. Each account shows this year, last year, the
change and the change as a % of last year. Accounts are flagged when the change is at least
the threshold amount (performance materiality by default) and at least the threshold
percentage (default 10%), when they are new or gone, or when the balance switched between
debit and credit. Comparisons are saved and export to Excel. `sample_data/sample_tb_2025.csv`
and `sample_tb_2026.csv` are sample trial balances for trying it.

The **Quick audit** tab runs the checks on a file without saving anything.

Data is stored in SQLite (`audit.db` in the working directory). Set `AUDIT_DB_URL` to use
another database supported by SQLAlchemy.

## Checks

| Check | What it flags | Severity |
| --- | --- | --- |
| Debits equal credits | The file doesn't net to zero, or a journal entry doesn't balance | High |
| At or above performance materiality | Lines whose amount is at least performance materiality (when set) | High |
| Duplicate entries | Rows with the same date, account, description and amount | High |
| Period cutoff | Entries dated after period end, dated in the period but posted after it, or in the last *N* days | High / Low |
| Weekend entries | Entries posted (or dated, if there's no posting date) on a Saturday or Sunday | Medium |
| Round-number amounts | Amounts that are exact multiples of the threshold (default 1,000) | Medium |
| Unusually large amounts | Amounts above Q3 + 1.5×IQR of their account's amounts, on a log scale | Medium |
| Benford's law | First-digit distribution of debit amounts vs Benford; when it doesn't conform (Nigrini MAD > 0.015), rows with over-represented leading digits | Low |

Checks that need a column the file doesn't have (for example dates in a trial balance)
are skipped and say why.

## File format

The first row must be a header. After you pick a file, the app shows its first rows and a
column mapping screen, so you can say which column holds each field. For Excel files with
several sheets you can pick the sheet. The mapping is pre-filled by matching common header
names, case-insensitively:

| Field | Recognised headers (examples) |
| --- | --- |
| Amount | `Amount`, or `Debit` and `Credit` |
| Date | `Date`, `Transaction Date`, `Entry Date`, `Effective Date` |
| Posting date | `Posted Date`, `Posting Date`, `Created Date` |
| Account | `Account`, `Account No`, `GL Account`, `Account Name` |
| Entry ID | `JE Number`, `Journal ID`, `Document No`, `Voucher`, `Reference` |
| Description | `Description`, `Memo`, `Narration` |
| User | `Created By`, `Posted By`, `User` |

After import the summary shows rows read vs analysed, control totals, and warnings for rows
skipped (blank or non-numeric amount), dates that couldn't be read, and rows with both a debit
and a credit.

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

- `POST /api/preview`: multipart form with `file` and optional `sheet`. Returns the columns,
  sheet names, first rows and a suggested mapping.
- `POST /api/audit`: multipart form with `file`, and optional `period_end` (YYYY-MM-DD),
  `cutoff_days`, `round_threshold`, `sheet` and `mapping` (JSON object of field to column, e.g.
  `{"date": "Txn Dt", "amount": "Value"}`; fields are `date`, `posted`, `account`, `entry_id`,
  `description`, `user`, `amount`, `debit`, `credit`). Returns JSON with `summary`, `import`,
  `checks`, `findings` and `benford`.
- `POST /api/report`: same fields plus `format` (`xlsx` or `csv`). Returns the report file.

Engagement endpoints (JSON unless noted):

- `GET/POST /api/clients`, `GET/PATCH/DELETE /api/clients/{id}`
- `POST /api/clients/{id}/engagements`, `GET/PATCH/DELETE /api/engagements/{id}`
- `GET /api/materiality/benchmarks`, `POST /api/materiality/calculate`,
  `PUT /api/engagements/{id}/materiality`
- `POST /api/engagements/{id}/uploads` (multipart: `file`, `kind` = `gl`, `tb` or `prior_tb`,
  `sheet`, `mapping`), `DELETE /api/uploads/{id}`
- `POST /api/engagements/{id}/runs` (`upload_id`, `cutoff_days`, `round_threshold`),
  `GET /api/runs/{id}`, `GET /api/runs/{id}/report?format=xlsx|csv`

- `POST /api/engagements/{id}/comparisons` (`current_upload_id`, `prior_upload_id`,
  `threshold_amount`, `threshold_pct`), `GET /api/comparisons/{id}`,
  `GET /api/comparisons/{id}/report`
- `POST /api/compare`: compare two files without saving (multipart: `current`, `prior`,
  `threshold_amount`, optional `threshold_pct`, `current_mapping`, `prior_mapping`,
  `current_sheet`, `prior_sheet`, `format=json|xlsx`)

Quick audit uploads are processed in memory and not stored.

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```
