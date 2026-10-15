"""AI account mapping and reading documents, with the Claude API faked: no real calls."""

import json
import re

import pytest

from app import ai_tools, assistant
from tests.test_statements import DATA, client, engagement

PDF = b"%PDF-1.4 fake statement"
GL = DATA / "sample_gl.csv"


@pytest.fixture
def fake_api(monkeypatch):
    """Answer each request with the next JSON in `answers`; keep what was sent in `calls`."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    state = {"answers": [], "calls": []}

    def fake(system, content, **kw):
        state["calls"].append((system, content, kw))
        answer = state["answers"].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, str) else json.dumps(answer)

    monkeypatch.setattr(assistant, "request_claude", fake)
    return state


def activity(eid, action):
    return client.get("/api/activity", params={"engagement_id": eid, "action": action}).json()


def with_gl(eid):
    r = client.post(f"/api/engagements/{eid}/uploads", data={"kind": "gl"},
                    files={"file": (GL.name, GL.read_bytes(), "text/csv")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def read(eid, kind, content=PDF, name="statement.pdf", **data):
    return client.post(f"/api/engagements/{eid}/documents/read", data={"kind": kind, **data},
                       files={"file": (name, content, "application/pdf")})


def test_off_without_a_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    eid = engagement(with_prior=False)
    r = client.post(f"/api/engagements/{eid}/mapping/suggest", json={})
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]
    assert read(eid, "bank").status_code == 503
    assert not activity(eid, "assistant.mapping") and not activity(eid, "document.")


def test_mapping_suggestions_are_checked_and_not_saved(fake_api):
    eid = engagement(with_prior=False)
    fake_api["answers"].append({"mappings": [
        {"account": "1000", "line": "cash", "confidence": "high", "reason": "Named Cash"},
        {"account": "1200", "line": "not_a_line", "confidence": "high", "reason": "x"},
        {"account": "9999", "line": "cash", "confidence": "high", "reason": "not sent"},
        {"account": "1000", "line": "revenue", "confidence": "low", "reason": "a repeat"},
        {"account": "4000", "line": "revenue", "confidence": "sure", "reason": "Revenue"},
        "nonsense"]})
    r = client.post(f"/api/engagements/{eid}/mapping/suggest", json={"language": "ar"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["sent"] == out["total"] == 16
    assert [(s["account"], s["line"], s["confidence"]) for s in out["suggestions"]] == [
        ("1000", "cash", "high"), ("4000", "revenue", "low")]
    assert out["suggestions"][0]["line_label"] == "Cash and cash equivalents"
    system, prompt, _ = fake_api["calls"][0]
    assert system == ai_tools.MAPPING_SYSTEM and '"account": "1000"' in prompt
    assert "<lines>" in prompt and "Modern Standard Arabic" in prompt
    # Nothing saved: the accounts are still unmapped.
    data = client.get(f"/api/engagements/{eid}/statements").json()
    assert len(data["unmapped"]) == 16
    [entry] = activity(eid, "assistant.mapping")
    assert entry["summary"] == "Asked the AI assistant to suggest the mapping of 16 accounts"
    assert entry["details"]["outcome"] == "ok" and entry["details"]["accounts_sent"] == 16


def test_mapping_only_the_accounts_asked_for(fake_api):
    eid = engagement(with_prior=False)
    fake_api["answers"].append({"mappings": [{"account": "1200", "line": "receivables"}]})
    r = client.post(f"/api/engagements/{eid}/mapping/suggest", json={"accounts": ["1200"]})
    assert r.json()["sent"] == 1 and r.json()["suggestions"][0]["confidence"] == "low"
    assert '"account": "1000"' not in fake_api["calls"][0][1]


def test_mapping_needs_a_trial_balance_and_unmapped_accounts(fake_api):
    c = client.post("/api/clients", json={"name": "Empty"}).json()
    e = client.post(f"/api/clients/{c['id']}/engagements",
                    json={"name": "FY", "period_end": "2026-12-31"}).json()
    r = client.post(f"/api/engagements/{e['id']}/mapping/suggest", json={})
    assert r.status_code == 400 and "trial balance" in r.json()["detail"]
    eid = engagement(with_prior=False)
    data = client.get(f"/api/engagements/{eid}/statements").json()
    client.put(f"/api/engagements/{eid}/mapping",
               json={"mappings": {a["account"]: a["suggested"] for a in data["accounts"]}})
    r = client.post(f"/api/engagements/{eid}/mapping/suggest", json={})
    assert r.status_code == 400 and r.json()["detail"] == "Every account is already mapped."
    assert not fake_api["calls"]


def test_failed_and_unreadable_answers_are_logged(fake_api):
    eid = engagement(with_prior=False)
    fake_api["answers"] += [assistant.AssistantError("The Claude API is unavailable."),
                            "Sorry, I can't help with that."]
    r = client.post(f"/api/engagements/{eid}/mapping/suggest", json={})
    assert r.status_code == 502 and r.json()["detail"] == "The Claude API is unavailable."
    r = client.post(f"/api/engagements/{eid}/mapping/suggest", json={})
    assert r.status_code == 502 and "expected form" in r.json()["detail"]
    log = activity(eid, "assistant.mapping")
    assert [e["summary"].endswith(" (failed)") for e in log] == [True, True]


def test_busy_when_all_slots_are_taken(fake_api, monkeypatch):
    monkeypatch.setattr(assistant, "_slots", assistant.threading.BoundedSemaphore(1))
    eid = engagement(with_prior=False)
    assistant._slots.acquire()
    try:
        r = read(eid, "invoice")
    finally:
        assistant._slots.release()
    assert r.status_code == 429 and not fake_api["calls"]
    [entry] = activity(eid, "document.read_invoice")
    assert entry["summary"] == "Asked the AI assistant to read the invoice statement.pdf (busy)"


def test_only_pdf_and_images_and_not_too_big(fake_api, monkeypatch):
    eid = engagement(with_prior=False)
    r = read(eid, "invoice", content=b"Date,Amount\n", name="a.csv")
    assert r.status_code == 400 and r.json()["detail"] == "Upload a PDF, PNG or JPEG file."
    monkeypatch.setattr(ai_tools, "MAX_DOCUMENT_BYTES", 10)
    assert read(eid, "invoice").status_code == 413
    assert read(eid, "receipt").status_code == 422
    assert not fake_api["calls"]


def test_images_are_sent_as_images(fake_api):
    eid = engagement(with_prior=False)
    fake_api["answers"].append({"total": 5})
    r = read(eid, "invoice", content=b"\x89PNG\r\n\x1a\nimage", name="photo.png")
    assert r.status_code == 201, r.text
    block = fake_api["calls"][0][1][0]
    assert block["type"] == "image" and block["source"]["media_type"] == "image/png"


def test_invoice_is_checked_and_found_in_the_ledger(fake_api):
    eid = engagement(with_prior=False)
    with_gl(eid)
    # JE1001 in sample_gl.csv: office supplies, 33.32, on 2026-05-21.
    fake_api["answers"].append({
        "supplier": "Paper Co", "customer": "Acme", "invoice_number": "INV-77",
        "invoice_date": "2026-05-19", "due_date": None, "currency": "USD",
        "net": 27.77, "tax": 5.55, "total": 33.32,
        "lines": [{"description": "Paper", "quantity": 2, "unit_price": 10, "amount": 20},
                  {"description": "Pens", "quantity": None, "unit_price": None, "amount": 7.0}]})
    r = read(eid, "invoice", name="inv.pdf")
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["invoice"]["invoice_date"] == "2026-05-19" and out["ledger"] == GL.name
    assert [c["ok"] for c in out["checks"]] == [True, False]
    assert out["checks"][1]["text"] == "The lines add up to 27.00, not 27.77."
    first = out["matches"][0]
    assert first["entry_id"] == "JE1001" and abs(first["amount"]) == 33.32
    assert first["reasons"] == ["Same amount as the invoice"] and first["days_from_invoice"] == 2
    system, content, kw = fake_api["calls"][0]
    assert system == ai_tools.INVOICE_SYSTEM and content[0]["type"] == "document"
    assert kw["max_tokens"] == ai_tools.DOCUMENT_TOKENS
    [entry] = activity(eid, "document.read_invoice")
    assert entry["summary"] == "Asked the AI assistant to read the invoice inv.pdf"
    assert entry["details"]["sha256"] == out["sha256"]


def test_invoice_number_in_the_ledger_and_missing_total():
    gl = None
    inv = {"total": None, "net": None, "tax": None, "lines": [], "invoice_number": "X"}
    assert ai_tools.invoice_checks(inv) == [
        {"ok": False, "text": "No total could be read from the invoice."}]
    assert ai_tools.ledger_matches(gl, inv) == []


def test_bank_statement_becomes_an_upload_for_the_reconciliation(fake_api):
    eid = engagement(with_prior=False)
    gl = with_gl(eid)
    fake_api["answers"].append({
        "bank": "Nile Bank", "account": "12345", "currency": "USD",
        "opening_balance": 1000, "closing_balance": 1180,
        "lines": [
            {"date": "2026-12-30", "description": "TRANSFER IN", "money_in": 200,
             "money_out": None, "balance": 1200},
            {"date": "2026-12-31", "description": "CHQ 4101", "money_in": None,
             "money_out": "50.00", "balance": 1150},
            {"date": "2027-01-04", "description": "DEPOSIT", "money_in": 30, "money_out": None,
             "balance": 1180},
            {"date": "", "description": "Page total", "money_in": 230, "money_out": 50}]})
    r = read(eid, "bank", name="December.pdf")
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["lines"] == 3 and out["upload"]["kind"] == "bank"
    assert out["upload"]["filename"] == "December (read from PDF).csv"
    assert out["read"]["balance_at_period_end"] == 1150.0
    assert out["read"]["problems"] == [
        "1 lines without a readable date or amount were left out."]
    [entry] = activity(eid, "upload.create")[:1]
    assert entry["summary"] == ("Uploaded December (read from PDF).csv (Bank statement) read by "
                                "the AI assistant from December.pdf")
    assert activity(eid, "document.read_bank")[0]["details"]["outcome"] == "ok"
    # The reconciliation takes the statement's balance at the period end.
    r = client.post(f"/api/engagements/{eid}/bank-reconciliations", json={
        "bank_upload_id": out["upload"]["id"], "gl_upload_id": gl, "accounts": ["1000 Cash"]})
    assert r.status_code == 201, r.text
    settings = r.json()["result"]["settings"]
    assert settings["bank_balance"] == 1150.0 and settings["bank_balance_source"] == "statement"
    # An amount entered by hand still wins.
    r = client.post(f"/api/engagements/{eid}/bank-reconciliations", json={
        "bank_upload_id": out["upload"]["id"], "gl_upload_id": gl, "accounts": ["1000 Cash"],
        "bank_balance": 7})
    assert r.json()["result"]["settings"]["bank_balance_source"] == "entered"


def test_bank_balances_that_dont_add_up():
    answer = {"opening_balance": 100, "closing_balance": 500, "lines": [
        {"date": "2026-01-02", "money_in": 50, "balance": 150},
        {"date": "2026-01-03", "money_out": 20, "balance": 999},
        {"date": "2026-01-04", "money_in": 1, "balance": None}]}
    lines, problems = ai_tools.bank_lines(answer)
    assert len(lines) == 3
    assert problems == [
        "1 lines don't agree with the running balance; check them against the statement.",
        "Opening balance plus the lines comes to 131.00, not the closing balance of 500.00; "
        "some lines may be missing or misread."]
    from datetime import date  # noqa: PLC0415
    assert ai_tools.balance_at(lines, answer, date(2026, 1, 1)) == 100
    assert ai_tools.balance_at(lines, answer, date(2026, 1, 4)) == 1000.0
    assert ai_tools.balance_at(lines, {"lines": []}, date(2026, 1, 1)) is None


def test_bank_statement_with_no_lines(fake_api):
    eid = engagement(with_prior=False)
    fake_api["answers"].append({"lines": []})
    r = read(eid, "bank")
    assert r.status_code == 400 and "No transaction lines" in r.json()["detail"]


def test_the_csv_is_safe_in_excel():
    lines, _ = ai_tools.bank_lines({"lines": [
        {"date": "2026-01-02", "description": "=HYPERLINK(\"x\")", "money_in": 5}]})
    # The bank CSV keeps the text; exports escape it (exports.finish_workbook).
    assert b'"=HYPERLINK(""x"")"' in ai_tools.bank_csv(lines)


def test_parse_json_allows_a_code_fence():
    assert ai_tools.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    for bad in ("", "[1]", "{not json}"):
        with pytest.raises(assistant.AssistantError):
            ai_tools.parse_json(bad)


def test_new_text_has_arabic():
    from tests.test_i18n import dictionary  # noqa: PLC0415

    d = dictionary()
    patterns = [re.compile("^" + re.sub(r"\\\{(?:t:)?\w+\\\}", ".+", re.escape(p)) + "$")
                for p in d["patterns"]]

    def translated(text):
        return text in d["exact"] or any(p.match(text) for p in patterns)

    texts = ["Upload a PDF, PNG or JPEG file.", "Upload this year's trial balance first.",
             "Every account is already mapped.",
             "The AI's answer wasn't in the expected form. Try again.",
             "No transaction lines could be read from the statement.",
             "Pick a general ledger uploaded to this engagement.",
             "The file is larger than 15 MB. Split the statement into smaller files.",
             "The engagement was deleted while the AI was answering.",
             "Net plus tax equals the total.", "The lines add up to the invoice amount.",
             "Net plus tax is 10.00, not the total of 11.00.",
             "The lines add up to 27.00, not 27.77.",
             "No total could be read from the invoice.",
             "Same amount as the invoice", "Invoice number in the entry",
             "1 lines without a readable date or amount were left out.",
             "2 lines don't agree with the running balance; check them against the statement.",
             "Opening balance plus the lines comes to 131.00, not the closing balance of "
             "500.00; some lines may be missing or misread.",
             "Asked the AI assistant to suggest the mapping of 17 accounts",
             "Asked the AI assistant to suggest the mapping of 17 accounts (busy)",
             "Asked the AI assistant to suggest the mapping of 17 accounts (failed)",
             "Asked the AI assistant to read the invoice inv.pdf",
             "Asked the AI assistant to read the bank statement a.pdf (busy)",
             "Asked the AI assistant to read the bank statement a.pdf (failed)",
             "Uploaded a (read from PDF).csv (Bank statement) read by the AI assistant from a.pdf"]
    missing = [t for t in texts if not translated(t)]
    assert not missing, missing
