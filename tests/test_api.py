import io
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
SAMPLE = Path(__file__).resolve().parent.parent / "sample_data" / "sample_gl.csv"


def upload(**data):
    return {"file": (SAMPLE.name, SAMPLE.read_bytes(), "text/csv")}, data


def test_index_page():
    r = client.get("/")
    assert r.status_code == 200
    assert "Audit App" in r.text


def test_audit_endpoint():
    files, data = upload(period_end="2026-12-31")
    r = client.post("/api/audit", files=files, data=data)
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["rows"] > 0
    assert {c["id"] for c in body["checks"]} >= {"duplicates", "benford", "cutoff", "weekend"}


def test_bad_file_returns_400():
    r = client.post("/api/audit", files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")})
    assert r.status_code == 400
    assert "amount" in r.json()["detail"]


def test_bad_period_end_returns_400():
    files, data = upload(period_end="31/12/2026")
    assert client.post("/api/audit", files=files, data=data).status_code == 400


def test_excel_report():
    files, data = upload(format="xlsx")
    r = client.post("/api/report", files=files, data=data)
    assert r.status_code == 200
    assert "audit-report.xlsx" in r.headers["content-disposition"]
    sheets = pd.read_excel(io.BytesIO(r.content), sheet_name=None)
    assert set(sheets) == {"Summary", "Findings", "Benford"}
    assert len(sheets["Findings"]) > 0


def test_csv_report():
    files, data = upload(format="csv")
    r = client.post("/api/report", files=files, data=data)
    assert r.status_code == 200
    assert r.text.startswith("severity,check,row")
