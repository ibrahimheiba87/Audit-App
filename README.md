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

**Sampling**: draw items to test from an upload, by simple random selection, systematic
selection (fixed interval from a random start) or monetary unit sampling (MUS). The
population can be all lines, debits only or credits only, optionally above a minimum amount.
For MUS, leave the sample size blank to calculate it from the confidence level, tolerable
misstatement (performance materiality by default) and expected misstatement:
`n = population value x reliability factor / (tolerable - expected x expansion factor)`, using
the zero-error Poisson factors (95%: 3.0 and 1.6). Lines at or above the sampling interval are
always picked and marked as top stratum. Every sample stores its seed, so the same file,
settings and seed reproduce the same selection. Samples export to an Excel workpaper with
the parameters and blank columns for the test results.

**Findings register**: every audit run adds its findings to the engagement's register.
Running the checks again on the same file keeps the work already done instead of adding
duplicates. Each finding moves from Open to Reviewed to Cleared; clearing needs a note saying
why. Notes, status and severity changes are kept in a review trail with who and when. Evidence files (up to 20 MB each) can
be attached to a finding and are stored with their SHA-256. Select several findings to mark
them reviewed or clear them at once, add findings by hand (for example sample exceptions), and
download the register to Excel with the review trail and evidence list. A file whose findings
have been worked on can't be deleted, so the support stays with the engagement.

**Lead sheets and draft statements**: map each trial balance account to a financial statement
line (cash, receivables, revenue, employee costs and so on). The app suggests a line from the
account name or number, and you confirm or change it. Post adjusting entries (AJE),
reclassifications (RJE) and proposed entries the client hasn't booked (PAJE). Each entry must
balance, and entry numbers are never reused. Booked entries roll into the adjusted trial balance, the lead sheets (unadjusted,
adjustments, adjusted and prior year per account) and a draft income statement and balance
sheet with comparatives. Proposed entries are summarised against materiality as uncorrected
misstatements. Preparers post proposed entries; a reviewer posts booked ones, marks a
proposed entry as booked or deletes an entry. Download it all as one Excel workbook.

**Review and sign-off**: the file is split into sections (planning and materiality, files and
checks, prior-year comparison, sampling, findings, lead sheets and statements, completion).
Whoever prepared a section signs it off, then someone else signs it off as reviewed. If the
section changes afterwards (a new upload or run, a changed finding, a new entry), the sign-off
is marked changed so the reviewer knows to look again. Reviewers raise review notes on a
section; the preparer replies and a reviewer clears them. A section can't be signed off as
reviewed while it has notes to clear, raising a note takes back its review sign-off, and a
partner signs off completion once every other section is reviewed.

**Client requests**: ask the client for documents (title, details for the client, due date).
People at the client sign in with a **Client** account and see only their own company's
requests, in a portal of their own: they upload files against each request and can message the
team. The team downloads the files, accepts them, or sends the request back saying what's
missing; accepted requests are locked until a reviewer sends them back. Overdue requests are
flagged, and the request list downloads as Excel or CSV.

**AI assistant** (optional): on any finding, ask Claude to explain why the entry was flagged
and what to check, or to draft a write-up (condition, criteria, cause, effect,
recommendation). Nothing is saved unless you add the answer as a note. Each request sends:

- the finding: check, severity, status, entry, date, account, description, amount and detail;
- the engagement's framework, period end and materiality figures (not the client's name);
- up to 50 ledger lines of the same entry, always including the flagged line, with their
  dates, accounts, descriptions, amounts and who posted them.

It is off, and hidden, until the server has an API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...          # turns the assistant on
export AUDIT_AI_MODEL=claude-opus-5-5         # optional; this is the default
```

Each request is logged, including failed ones, with the model, how many ledger lines were sent
and the outcome. At most four requests run at once; more get "busy, try again". Ledger data goes
to the Claude API when someone uses the assistant, so only turn it on if your firm allows it.

The **Quick audit** tab runs the checks on a file without saving anything.

## Users, roles and the activity log

Everyone signs in with an email and password. The first time the app opens it asks for the
first account, which becomes an admin; the admin then adds the team under **Users** with a
temporary password and a role. Each role can do everything the roles above it in this list
can:

- **Preparer**: upload files, run the checks, comparisons and samples, add findings, notes and
  evidence, mark findings reviewed, map accounts, post proposed adjusting entries, sign off
  sections as prepared and reply to review notes.
- **Reviewer**: also clear and reopen findings, change severity, remove evidence and files,
  post booked entries or mark proposed ones booked, delete adjusting entries, raise and clear
  review notes, sign off sections as reviewed, and add or edit clients and engagements.
- **Partner**: also set materiality, sign off completion, and delete clients and engagements.
- **Admin**: also manage users, choose each engagement's team, and see the whole activity log.

**Engagement teams.** Admins see every engagement. Everyone else sees only the engagements
whose team they are on: the others don't appear in the client list, and their pages, files,
exports, findings, requests and AI assistant answer "not found". An admin picks the team with
**Change team** on the engagement page; whoever creates an engagement joins its team. A client
with no engagements yet is visible to the whole audit team, so someone can add its first
engagement. Deleting a client needs someone on every one of its engagements (or an admin).
Team changes are logged, and making someone a client user takes them off every team. Admins
aren't added to teams, since they already see everything.

Client names are unique across the firm, so adding or renaming a client to a name that's taken
says so even when the existing client belongs to engagements you aren't on. Adding a new
engagement to such a client is for an admin or someone on its team.

**Upgrading from a version without teams:** engagements that already exist start with an empty
team, so only admins see them. Before your team signs in, an admin opens each engagement and
uses **Change team** to add the people who work on it.

**Client** users are outside this list: an admin links each one to a client. They only see the
document request portal for that client's engagements (upload files, remove their own files
until a request is accepted, send messages), and every other part of the app refuses them.

Every change and download is written to the activity log with who, when, the engagement, the
file's SHA-256 and the settings used (run settings, sampling parameters and seed, comparison
thresholds, materiality). Sign-ins, failed sign-ins and user changes are logged with the IP
address. Each engagement page shows its own log; admins see everything under **Activity**. The
log keeps names, so it survives deleting users' work or whole engagements.

Passwords are hashed with scrypt; sessions last 12 hours in an HttpOnly, SameSite=Lax cookie.
Five failed sign-ins for an email lock it for 15 minutes. Deactivating a user or resetting
their password signs them out. Behind an HTTPS proxy, set `AUDIT_SECURE_COOKIES=1` so the
cookie is only sent over HTTPS.

Data is stored in SQLite (`audit.db` in the working directory). Set `AUDIT_DB_URL` to use
PostgreSQL 13 or later instead (for example `postgresql+psycopg://user:pass@host/audit`; the
driver is in `requirements.txt`). Other databases are refused at startup: the checks that must not race (first-run
setup, the sign-in lockout, the last admin, entry numbering) rely on an atomic
`INSERT ... ON CONFLICT` that the app only uses on these two.

On a server that is online before anyone has signed up, set `AUDIT_SETUP_CODE` to a random
value. The first-run form then asks for it, so nobody else can create the first admin.

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

Exports straight from an accounting system work as they are: title rows above the table (company
name, report name, period) are skipped, because the header is taken to be the first row that
names at least two known columns. After you pick a file, the app shows its first rows and a
column mapping screen, so you can say which column holds each field. For Excel files with
several sheets you can pick the sheet. The mapping is pre-filled by matching common header
names, case-insensitively and ignoring anything in brackets (`Debit (EGP)` is a debit column):

| Field | Recognised headers (examples) |
| --- | --- |
| Amount | `Amount`, or `Debit` and `Credit` |
| Date | `Date`, `Transaction Date`, `Entry Date`, `Effective Date` |
| Posting date | `Posted Date`, `Posting Date`, `Created Date` |
| Account | `Account`, `Account No`, `GL Account`, `Account Name` |
| Entry ID | `JE Number`, `Journal No.`, `JV No`, `Document No`, `Voucher`, `Reference` |
| Description | `Description`, `Memo`, `Narration` |
| User | `Created By`, `Posted By`, `User` |

After import the summary shows rows read vs analysed, control totals, and warnings for rows
skipped (blank or non-numeric amount), total rows left out (an amount with no account, entry or
date, and "Total" or nothing as the description), dates that couldn't be read, and rows with
both a debit and a credit. Row numbers are the spreadsheet's own.

Dates like `03/04/2026` are read day first (3 April) unless the file has a date that only works
month first, such as `04/13/2026`. ISO dates and real spreadsheet dates are always unambiguous.

Amounts like `1,234.50`, `(500.00)`, `$75` and `20-` are understood. Only an amount column
is required. `sample_data/sample_gl.csv` is a sample ledger with planted issues
(regenerate it with `python scripts/make_sample.py`). The `sample_data/nile_trading_*.xlsx`
files are a ledger and two trial balances laid out like a real system's export, for trying a
whole engagement (`python scripts/make_sample_exports.py` regenerates them).

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

All endpoints need a signed-in session cookie except `/api/health`, `GET /api/auth/status`,
`POST /api/auth/setup`, `POST /api/auth/login` and `POST /api/auth/logout`.

- `GET /api/auth/status`, `POST /api/auth/setup` (first admin, only while there are no users),
  `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`, `POST /api/auth/password`
- `GET|POST /api/users`, `PATCH /api/users/{id}` (`name`, `role`, `active`, `password`, `client_id` for client users); admin only
- `GET /api/activity?engagement_id=&user_id=&action=&limit=`; without `engagement_id`, admin
  only; with it, admins and the engagement's team

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
- `POST /api/clients/{id}/engagements`, `GET/PATCH/DELETE /api/engagements/{id}` (the
  engagement includes its `team`)
- `PUT /api/engagements/{id}/team` (`user_ids`): set who is on the engagement (not admins or
  client users); admin only
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
- `GET /api/sampling/options`
- `POST /api/engagements/{id}/samples` (`upload_id`, `method=random|systematic|mus`,
  `sample_size`, `seed`, `population=all|debits|credits`, `min_amount`, and for MUS
  `confidence`, `tolerable_misstatement`, `expected_misstatement`), `GET /api/samples/{id}`,
  `GET /api/samples/{id}/export`
- `GET /api/engagements/{id}/findings?status=&severity=&check=`,
  `POST /api/engagements/{id}/findings` (add by hand), `POST /api/engagements/{id}/findings/bulk`
  (`ids`, `status`, `note`), `GET /api/engagements/{id}/findings/export`
- `GET|PATCH|DELETE /api/findings/{id}` (`status`, `severity`, `note`, `author`),
  `POST /api/findings/{id}/notes`, `POST /api/findings/{id}/evidence` (multipart `file`),
  `GET|DELETE /api/evidence/{id}`
- `GET /api/engagements/{id}/statements?upload_id=&prior_upload_id=`: mapping, adjusted TB,
  lead sheets, statements and uncorrected misstatements (defaults to the latest trial balances);
  `GET /api/engagements/{id}/statements/export`
- `PUT /api/engagements/{id}/mapping` (`mappings`: account to line id, or `null` to unmap)
- `POST /api/engagements/{id}/adjusting-entries` (`kind=adjusting|reclassification|proposed`,
  `description`, `date`, `lines`: `account`, `debit`, `credit`, `memo`),
  `PATCH /api/adjusting-entries/{id}` (`kind`), `DELETE /api/adjusting-entries/{id}`
- `GET /api/engagements/{id}/review`: each section's sign-offs and status, and the review notes
- `POST /api/engagements/{id}/signoffs` (`section`, `kind=prepared|reviewed`),
  `DELETE /api/engagements/{id}/signoffs/{section}/{kind}`
- `POST /api/engagements/{id}/review-notes` (`section`, `text`),
  `POST /api/review-notes/{id}/replies` (`text`), `PATCH /api/review-notes/{id}`
  (`status=open|cleared`), `DELETE /api/review-notes/{id}`
- `GET|POST /api/engagements/{id}/requests` (`title`, `details`, `due_date`),
  `GET /api/engagements/{id}/requests/export?format=xlsx|csv`,
  `PATCH|DELETE /api/requests/{id}`, `POST /api/requests/{id}/status`
  (`status=accepted|returned`, `message`)
- `POST /api/requests/{id}/files` (multipart `file`), `GET|DELETE /api/request-files/{id}`,
  `POST /api/requests/{id}/messages` (`text`): for the team and the request's client users
- `GET /api/portal/requests`: a client user's engagements and requests
- `GET /api/assistant/status`, `POST /api/findings/{id}/assistant` (`task=explain|draft`):
  the AI assistant; 503 while it is off, 502 when the Claude API fails, 429 when busy
- `POST /api/sample`: draw a sample from a file without saving (multipart: `file`, the same
  fields as above, optional `mapping`, `sheet`, `format=json|xlsx`)

Quick audit uploads are processed in memory and not stored.

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```
