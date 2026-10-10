import io
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
SAMPLE = Path(__file__).resolve().parent.parent / "sample_data" / "sample_gl.csv"


def make_engagement(**overrides):
    c = client.post("/api/clients", json={"name": "Acme Ltd", "industry": "Retail"}).json()
    body = {"name": "FY2026 audit", "period_end": "2026-12-31", **overrides}
    r = client.post(f"/api/clients/{c['id']}/engagements", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def upload_sample(eng_id, **data):
    files = {"file": (SAMPLE.name, SAMPLE.read_bytes(), "text/csv")}
    r = client.post(f"/api/engagements/{eng_id}/uploads", files=files, data=data)
    assert r.status_code == 201, r.text
    return r.json()


def test_client_crud():
    r = client.post("/api/clients", json={"name": "Acme Ltd"})
    assert r.status_code == 201
    cid = r.json()["id"]
    assert client.post("/api/clients", json={"name": "Acme Ltd"}).status_code == 409
    assert client.patch(f"/api/clients/{cid}", json={"industry": "Retail"}).json()["industry"] == "Retail"
    assert [c["name"] for c in client.get("/api/clients").json()] == ["Acme Ltd"]
    assert client.delete(f"/api/clients/{cid}").status_code == 204
    assert client.get(f"/api/clients/{cid}").status_code == 404


def test_engagement_validation():
    c = client.post("/api/clients", json={"name": "Beta"}).json()
    url = f"/api/clients/{c['id']}/engagements"
    bad_period = {"name": "x", "period_start": "2027-01-01", "period_end": "2026-12-31"}
    assert client.post(url, json=bad_period).status_code == 400
    assert client.post(url, json={"name": "x", "period_end": "2026-12-31",
                                  "framework": "Klingon GAAP"}).status_code == 400
    e = client.post(url, json={"name": "FY26", "period_end": "2026-12-31"}).json()
    r = client.patch(f"/api/engagements/{e['id']}", json={"framework": "US GAAP"})
    assert r.json()["framework"] == "US GAAP"
    listed = client.get("/api/clients").json()[0]["engagements"]
    assert [x["name"] for x in listed] == ["FY26"]


def test_materiality_calculate_endpoint():
    r = client.post("/api/materiality/calculate",
                    json={"benchmark": "total_assets", "benchmark_amount": 2_000_000})
    assert r.json()["overall_materiality"] == 20_000
    bad = client.post("/api/materiality/calculate", json={"benchmark": "x", "benchmark_amount": 1})
    assert bad.status_code == 400
    assert client.get("/api/materiality/benchmarks").json()["benchmarks"][0]["id"] == "pbt"


def test_set_materiality_on_engagement():
    e = make_engagement()
    r = client.put(f"/api/engagements/{e['id']}/materiality", json={
        "benchmark": "revenue", "benchmark_amount": 3_000_000, "rationale": "Revenue-driven",
    })
    m = r.json()["materiality"]
    assert m["overall_materiality"] == 30_000
    assert m["performance_materiality"] == 22_500
    assert m["trivial_threshold"] == 1_500
    assert m["rationale"] == "Revenue-driven"


def test_upload_and_run_uses_engagement_settings():
    e = make_engagement()
    client.put(f"/api/engagements/{e['id']}/materiality",
               json={"benchmark": "revenue", "benchmark_amount": 6_000_000})
    up = upload_sample(e["id"], kind="gl")
    assert len(up["sha256"]) == 64
    assert up["import"]["rows_read"] > 0

    r = client.post(f"/api/engagements/{e['id']}/runs", json={"upload_id": up["id"]})
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["settings"]["period_end"] == "2026-12-31"
    assert run["settings"]["performance_materiality"] == 45_000
    mat = next(c for c in run["result"]["checks"] if c["id"] == "materiality")
    assert mat["status"] == "ran" and mat["flagged"] > 0  # 48,750 year-end sale and 96,500 travel

    detail = client.get(f"/api/engagements/{e['id']}").json()
    assert [x["id"] for x in detail["runs"]] == [run["id"]]
    assert client.get(f"/api/runs/{run['id']}").json()["result"]["summary"]["rows"] > 0


def test_run_report_has_engagement_context():
    e = make_engagement()
    up = upload_sample(e["id"])
    run = client.post(f"/api/engagements/{e['id']}/runs", json={"upload_id": up["id"]}).json()
    r = client.get(f"/api/runs/{run['id']}/report")
    assert r.status_code == 200
    summary = pd.read_excel(io.BytesIO(r.content), sheet_name="Summary", header=None)
    assert "Acme Ltd" in summary[1].tolist()
    assert client.get(f"/api/runs/{run['id']}/report?format=csv").status_code == 200


def test_upload_rejects_bad_file_and_kind():
    e = make_engagement()
    files = {"file": ("x.csv", b"a,b\n1,2\n", "text/csv")}
    assert client.post(f"/api/engagements/{e['id']}/uploads", files=files).status_code == 400
    files = {"file": (SAMPLE.name, SAMPLE.read_bytes(), "text/csv")}
    r = client.post(f"/api/engagements/{e['id']}/uploads", files=files, data={"kind": "pdf"})
    assert r.status_code == 400


def test_run_rejects_upload_from_other_engagement():
    e1 = make_engagement()
    c2 = client.post("/api/clients", json={"name": "Other"}).json()
    e2 = client.post(f"/api/clients/{c2['id']}/engagements",
                     json={"name": "FY", "period_end": "2026-06-30"}).json()
    up = upload_sample(e1["id"])
    r = client.post(f"/api/engagements/{e2['id']}/runs", json={"upload_id": up["id"]})
    assert r.status_code == 400


def test_delete_upload_removes_its_runs():
    e = make_engagement()
    up = upload_sample(e["id"])
    client.post(f"/api/engagements/{e['id']}/runs", json={"upload_id": up["id"]})
    assert client.delete(f"/api/uploads/{up['id']}").status_code == 204
    detail = client.get(f"/api/engagements/{e['id']}").json()
    assert detail["uploads"] == [] and detail["runs"] == []


def test_quick_audit_accepts_performance_materiality():
    files = {"file": (SAMPLE.name, SAMPLE.read_bytes(), "text/csv")}
    r = client.post("/api/audit", files=files, data={"performance_materiality": "40000"})
    mat = next(c for c in r.json()["checks"] if c["id"] == "materiality")
    assert mat["flagged"] > 0
