import io
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from app import db
from app.main import app

client = TestClient(app)
GL = Path(__file__).resolve().parent.parent / "sample_data" / "sample_gl.csv"


def setup_run():
    c = client.post("/api/clients", json={"name": "Acme"}).json()
    e = client.post(f"/api/clients/{c['id']}/engagements",
                    json={"name": "FY2026", "period_end": "2026-12-31"}).json()
    client.put(f"/api/engagements/{e['id']}/materiality",
               json={"benchmark": "revenue", "benchmark_amount": 6_000_000})
    up = client.post(f"/api/engagements/{e['id']}/uploads",
                     files={"file": (GL.name, GL.read_bytes(), "text/csv")}).json()
    run = client.post(f"/api/engagements/{e['id']}/runs", json={"upload_id": up["id"]}).json()
    return e["id"], up["id"], run


def findings(eid, **params):
    r = client.get(f"/api/engagements/{eid}/findings", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def test_run_findings_land_on_the_register_once():
    eid, uid, run = setup_run()
    reg = findings(eid)
    assert reg["summary"]["total"] == len(run["result"]["findings"])
    assert reg["summary"]["open"] == reg["summary"]["total"]
    assert {c["id"] for c in reg["checks"]} >= {"duplicates", "benford"}
    names = {c["id"]: c["name"] for c in reg["checks"]}
    assert names["duplicates"] != "duplicates"
    # Running the checks again on the same file doesn't duplicate the register.
    client.post(f"/api/engagements/{eid}/runs", json={"upload_id": uid})
    assert findings(eid)["summary"]["total"] == reg["summary"]["total"]
    high = findings(eid, severity="high")["findings"]
    assert high and all(f["severity"] == "high" for f in high)
    detail = client.get(f"/api/engagements/{eid}").json()
    assert detail["findings_summary"]["total"] == reg["summary"]["total"]


def test_status_workflow_and_trail():
    eid, _, _ = setup_run()
    fid = findings(eid)["findings"][0]["id"]
    url = f"/api/findings/{fid}"
    r = client.patch(url, json={"status": "reviewed", "author": "Sara"})
    assert r.json()["status"] == "reviewed"
    bad = client.patch(url, json={"status": "cleared"})
    assert bad.status_code == 400 and "note" in bad.json()["detail"]
    r = client.patch(url, json={"status": "cleared", "note": "Agreed to invoice 4411",
                                "author": "Omar"})
    f = r.json()
    assert f["status"] == "cleared"
    trail = [(n["status_from"], n["status_to"], n["author"]) for n in f["trail"]]
    assert trail == [("open", "reviewed", "Sara"), ("reviewed", "cleared", "Omar")]
    r = client.patch(url, json={"severity": "low", "author": "Omar"})
    assert r.json()["severity"] == "low"
    assert "Severity changed" in r.json()["trail"][-1]["text"]
    assert client.patch(url, json={"status": "pending"}).status_code == 422
    assert findings(eid, status="cleared")["summary"]["cleared"] == 1


def test_notes():
    eid, _, _ = setup_run()
    fid = findings(eid)["findings"][0]["id"]
    r = client.post(f"/api/findings/{fid}/notes", json={"text": "Asked client for support",
                                                         "author": "Sara"})
    assert r.status_code == 201
    assert r.json()["notes"] == 1
    assert r.json()["trail"][0]["text"] == "Asked client for support"
    assert client.post(f"/api/findings/{fid}/notes", json={"text": ""}).status_code == 422


def test_evidence_upload_download_and_delete():
    eid, _, _ = setup_run()
    fid = findings(eid)["findings"][0]["id"]
    r = client.post(f"/api/findings/{fid}/evidence", data={"author": "Sara"},
                    files={"file": ("invoice 4411.pdf", b"%PDF-1.4 test", "application/pdf")})
    assert r.status_code == 201, r.text
    ev = r.json()["attachments"][0]
    assert ev["size"] == 13 and len(ev["sha256"]) == 64 and ev["author"] == "Sara"
    got = client.get(f"/api/evidence/{ev['id']}")
    assert got.content == b"%PDF-1.4 test"
    assert "attachment" in got.headers["content-disposition"]
    assert got.headers["x-content-type-options"] == "nosniff"
    empty = client.post(f"/api/findings/{fid}/evidence", files={"file": ("x.txt", b"")})
    assert empty.status_code == 400
    # Cleared findings keep their evidence.
    client.patch(f"/api/findings/{fid}", json={"status": "cleared", "note": "ok"})
    assert client.delete(f"/api/evidence/{ev['id']}").status_code == 400
    client.patch(f"/api/findings/{fid}", json={"status": "open"})
    assert client.delete(f"/api/evidence/{ev['id']}").status_code == 204
    f = client.get(f"/api/findings/{fid}").json()
    assert f["attachments"] == []
    assert "Removed evidence invoice 4411.pdf" in f["trail"][-1]["text"]


def test_bulk_update():
    eid, _, _ = setup_run()
    low = [f["id"] for f in findings(eid, check="benford")["findings"]]
    url = f"/api/engagements/{eid}/findings/bulk"
    assert client.post(url, json={"ids": low, "status": "cleared"}).status_code == 400
    r = client.post(url, json={"ids": low, "status": "reviewed", "author": "Sara"})
    assert r.json()["changed"] == len(low)
    assert r.json()["summary"]["reviewed"] == len(low)
    again = client.post(url, json={"ids": low, "status": "reviewed"})
    assert again.json()["changed"] == 0
    assert client.post(url, json={"ids": [999999], "status": "reviewed"}).status_code == 400


def test_manual_findings():
    eid, _, _ = setup_run()
    url = f"/api/engagements/{eid}/findings"
    r = client.post(url, json={"severity": "high", "detail": "Sample item 12 has no invoice",
                               "account": "5000", "amount": 2599.59, "date": "2026-01-23",
                               "author": "Sara"})
    assert r.status_code == 201, r.text
    f = r.json()
    assert f["source"] == "manual" and f["check_name"] == "Added by hand"
    assert client.post(url, json={"detail": ""}).status_code == 422
    assert client.delete(f"/api/findings/{f['id']}").status_code == 204
    run_finding = findings(eid)["findings"][0]["id"]
    assert client.delete(f"/api/findings/{run_finding}").status_code == 400


def test_upload_with_worked_findings_is_kept():
    eid, uid, _ = setup_run()
    fid = findings(eid)["findings"][0]["id"]
    client.post(f"/api/findings/{fid}/notes", json={"text": "Checked"})
    r = client.delete(f"/api/uploads/{uid}")
    assert r.status_code == 409
    eid2, uid2, _ = setup_run_other()
    assert client.delete(f"/api/uploads/{uid2}").status_code == 204
    assert findings(eid2)["summary"]["total"] == 0


def setup_run_other():
    c = client.post("/api/clients", json={"name": "Beta"}).json()
    e = client.post(f"/api/clients/{c['id']}/engagements",
                    json={"name": "FY2026", "period_end": "2026-12-31"}).json()
    up = client.post(f"/api/engagements/{e['id']}/uploads",
                     files={"file": (GL.name, GL.read_bytes(), "text/csv")}).json()
    run = client.post(f"/api/engagements/{e['id']}/runs", json={"upload_id": up["id"]}).json()
    return e["id"], up["id"], run


def test_runs_saved_before_the_register_are_synced():
    eid, _, run = setup_run()
    with db.SessionLocal() as s:
        for f in s.query(db.Finding).all():
            s.delete(f)
        s.commit()
    assert findings(eid)["summary"]["total"] == len(run["result"]["findings"])


def test_register_export():
    eid, _, _ = setup_run()
    fid = findings(eid)["findings"][0]["id"]
    client.patch(f"/api/findings/{fid}", json={"status": "cleared", "note": "Fine",
                                              "author": "Omar"})
    client.post(f"/api/findings/{fid}/evidence", files={"file": ("a.txt", b"abc", "text/plain")})
    r = client.get(f"/api/engagements/{eid}/findings/export")
    sheets = pd.read_excel(io.BytesIO(r.content), sheet_name=None)
    assert set(sheets) == {"Summary", "Findings", "Review trail", "Evidence"}
    summary = dict(zip(sheets["Summary"]["Item"], sheets["Summary"]["Value"], strict=True))
    assert summary["Client"] == "Acme" and summary["Cleared"] == 1
    assert sheets["Review trail"]["Who"].tolist() == ["Omar"]
    assert sheets["Evidence"]["File"].tolist() == ["a.txt"]
    assert sheets["Findings"].iloc[0]["Status"] == "Cleared"
