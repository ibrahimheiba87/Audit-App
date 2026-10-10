"""Regression tests for the review findings on the sampling and findings-register PRs."""

import io
from urllib.parse import quote

import openpyxl
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import db, exports, sampling
from app.main import app

client = TestClient(app)
EVIL = '=HYPERLINK("http://evil.example","click")'


def ledger_csv(description=EVIL):
    return ("Date,Account,Description,Debit,Credit,JE\n"
            f"2026-03-02,6100,\"{description.replace(chr(34), chr(34) * 2)}\",5000,,JE1\n"
            "2026-03-02,1000,+cmd|' /C calc'!A0,,5000,JE1\n"
            "2026-03-03,6100,Rent,100,,JE2\n"
            "2026-03-03,1000,Rent,,100,JE2\n").encode()


def cells(xlsx: bytes):
    book = openpyxl.load_workbook(io.BytesIO(xlsx))
    return [c for ws in book.worksheets for row in ws.iter_rows() for c in row if c.value is not None]


def assert_no_formulas(xlsx: bytes):
    found = cells(xlsx)
    assert not [c.value for c in found if c.data_type == "f"]
    return [c.value for c in found]


# --- Formula injection --------------------------------------------------------


def test_csv_safe():
    assert exports.csv_safe("=1+1") == "'=1+1"
    assert exports.csv_safe("@SUM(A1)") == "'@SUM(A1)"
    assert exports.csv_safe("Rent") == "Rent"
    assert exports.csv_safe(-5.0) == -5.0  # numbers stay numbers


def test_quick_audit_exports_have_no_formulas():
    files = {"file": ("gl.csv", ledger_csv(), "text/csv")}
    data = {"round_threshold": "1000"}
    xlsx = client.post("/api/report", files=files, data={**data, "format": "xlsx"}).content
    values = assert_no_formulas(xlsx)
    assert EVIL in values  # kept as visible text
    csv = client.post("/api/report", files=files, data={**data, "format": "csv"}).text
    assert "'=HYPERLINK" in csv and ",=HYPERLINK" not in csv


def engagement_with_upload(content=None, filename="gl.csv", client_name="Acme"):
    c = client.post("/api/clients", json={"name": client_name}).json()
    e = client.post(f"/api/clients/{c['id']}/engagements",
                    json={"name": "FY2026", "period_end": "2026-12-31"}).json()
    up = client.post(f"/api/engagements/{e['id']}/uploads",
                     files={"file": (filename, content or ledger_csv(), "text/csv")}).json()
    return e["id"], up["id"]


def test_engagement_exports_have_no_formulas():
    eid, uid = engagement_with_upload()
    run = client.post(f"/api/engagements/{eid}/runs", json={"upload_id": uid}).json()
    assert_no_formulas(client.get(f"/api/runs/{run['id']}/report").content)
    s = client.post(f"/api/engagements/{eid}/samples",
                    json={"upload_id": uid, "method": "random", "sample_size": 4, "seed": 1}).json()
    assert EVIL in assert_no_formulas(client.get(f"/api/samples/{s['id']}/export").content)
    f = client.post(f"/api/engagements/{eid}/findings", json={"detail": "=1+2", "account": EVIL}).json()
    client.post(f"/api/findings/{f['id']}/notes", json={"text": "=cmd()"})
    values = assert_no_formulas(client.get(f"/api/engagements/{eid}/findings/export").content)
    assert "=1+2" in values and "=cmd()" in values


def test_comparison_export_has_no_formulas():
    tb = "Account,Description,Amount\n1000,=EVIL(),100\n2000,Loan,-100\n"
    files = {"current": ("a.csv", tb.encode(), "text/csv"), "prior": ("b.csv", tb.encode(), "text/csv")}
    r = client.post("/api/compare", files=files, data={"threshold_amount": "1", "format": "xlsx"})
    assert "=EVIL()" in assert_no_formulas(r.content)


# --- Download filenames -------------------------------------------------------


def test_attachment_header():
    h = exports.attachment('Société "Générale" — FY.xlsx')["Content-Disposition"]
    h.encode("latin-1")  # must not raise
    assert 'filename="Soci_t_ G_n_rale _ FY.xlsx"' in h
    assert "filename*=UTF-8''" + quote("Société Générale — FY.xlsx", safe="") in h


def test_non_latin_names_download():
    eid, uid = engagement_with_upload(client_name="北京贸易")
    r = client.get(f"/api/engagements/{eid}/findings/export")
    assert r.status_code == 200
    assert quote("北京贸易", safe="") in r.headers["content-disposition"]
    f = client.post(f"/api/engagements/{eid}/findings", json={"detail": "x"}).json()
    ev = client.post(f"/api/findings/{f['id']}/evidence",
                     files={"file": ("счёт.pdf", b"%PDF", "application/pdf")}).json()["attachments"][0]
    r = client.get(f"/api/evidence/{ev['id']}")
    assert r.status_code == 200 and r.content == b"%PDF"
    assert quote("счёт.pdf", safe="") in r.headers["content-disposition"]


# --- Sampling input checks ----------------------------------------------------


def frame(n=10):
    df = pd.DataFrame({"amount": [1000.0] * n, "account": "1", "description": "x"})
    df.insert(0, "row", range(2, n + 2))
    return df


def test_mus_size_is_capped():
    p = sampling.SampleParams("mus", tolerable_misstatement=0.01, seed=1)
    with pytest.raises(sampling.SamplingError, match="over the 100,000 limit"):
        sampling.draw(frame(), p)
    with pytest.raises(sampling.SamplingError, match="Sample size"):
        sampling.draw(frame(), sampling.SampleParams("mus", sample_size=10**9, seed=1))


def test_negative_inputs_are_rejected():
    with pytest.raises(sampling.SamplingError, match="Seed"):
        sampling.draw(frame(), sampling.SampleParams("random", sample_size=2, seed=-1))
    with pytest.raises(sampling.SamplingError, match="Expected misstatement"):
        sampling.draw(frame(), sampling.SampleParams("mus", tolerable_misstatement=5000,
                                                     expected_misstatement=-4000))


def test_stateless_sample_rejects_bad_input_with_400():
    files = {"file": ("gl.csv", ledger_csv(), "text/csv")}
    for data in ({"method": "random", "sample_size": "2", "seed": "-5"},
                 {"method": "mus", "tolerable_misstatement": "100", "expected_misstatement": "-50"},
                 {"method": "mus", "tolerable_misstatement": "0.0001"},
                 {"method": "systematic", "sample_size": "5000000"}):
        r = client.post("/api/sample", files=files, data=data)
        assert r.status_code == 400, (data, r.text)


# --- Findings register --------------------------------------------------------


def test_blank_detail_and_note_are_rejected():
    eid, _ = engagement_with_upload()
    url = f"/api/engagements/{eid}/findings"
    assert client.post(url, json={"detail": "   "}).status_code == 422
    f = client.post(url, json={"detail": "  Missing invoice  "}).json()
    assert f["detail"] == "Missing invoice"
    assert client.post(f"/api/findings/{f['id']}/notes", json={"text": " \n "}).status_code == 422


def test_engagement_summary_backfills_old_runs():
    eid, uid = engagement_with_upload()
    client.post(f"/api/engagements/{eid}/runs", json={"upload_id": uid})
    client.post(f"/api/engagements/{eid}/runs", json={"upload_id": uid, "round_threshold": 50})
    with db.SessionLocal() as s:  # as if both runs were saved before the register existed
        s.query(db.Finding).delete()
        s.commit()
    summary = client.get(f"/api/engagements/{eid}").json()["findings_summary"]
    with db.SessionLocal() as s:
        expected = s.query(db.Finding).count()
        assert {f.run_id for f in s.query(db.Finding)} == {1, 2}  # both runs, not just the first
    assert summary["total"] == expected > 0


def test_findings_are_paged_on_the_server():
    eid, _ = engagement_with_upload()
    for i in range(7):
        client.post(f"/api/engagements/{eid}/findings",
                    json={"detail": f"item {i}", "severity": "high" if i % 2 else "low"})
    url = f"/api/engagements/{eid}/findings"
    page = client.get(url, params={"limit": 3, "offset": 3}).json()
    assert [f["detail"] for f in page["findings"]] == ["item 3", "item 4", "item 5"]
    assert page["matching"] == 7 and page["summary"]["total"] == 7
    high = client.get(url, params={"severity": "high", "limit": 2}).json()
    assert high["matching"] == 3 and len(high["findings"]) == 2
    assert client.get(url, params={"limit": 10**6}).json()["limit"] == 1000


def test_listing_findings_uses_a_fixed_number_of_queries():
    from sqlalchemy import event

    eid, _ = engagement_with_upload()
    for i in range(30):
        f = client.post(f"/api/engagements/{eid}/findings", json={"detail": f"f{i}"}).json()
        client.post(f"/api/findings/{f['id']}/evidence", files={"file": ("a.txt", b"abc")})
    statements = []

    def count(*_):
        statements.append(1)

    event.listen(db.engine, "before_cursor_execute", count)
    try:
        r = client.get(f"/api/engagements/{eid}/findings")
    finally:
        event.remove(db.engine, "before_cursor_execute", count)
    assert r.json()["findings"][0]["evidence"] == 1
    assert len(statements) < 15  # not one query per finding
