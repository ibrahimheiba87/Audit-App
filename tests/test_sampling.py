import io
import math
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app import loader, sampling
from app.main import app

client = TestClient(app)
GL = Path(__file__).resolve().parent.parent / "sample_data" / "sample_gl.csv"


def ledger(amounts):
    df = pd.DataFrame({"amount": amounts, "account": "1000", "description": "x"})
    df.insert(0, "row", range(2, len(df) + 2))
    return df


def gl():
    return loader.load(GL.name, GL.read_bytes()).df


def P(method, **kw):
    return sampling.SampleParams(method, **kw)


@pytest.mark.parametrize("method", ["random", "systematic", "mus"])
def test_same_seed_gives_same_sample(method):
    df = gl()
    p = P(method, sample_size=25, seed=1234)
    a, b = sampling.draw(df, p), sampling.draw(df, p)
    assert [i["row"] for i in a["items"]] == [i["row"] for i in b["items"]]
    other = sampling.draw(df, P(method, sample_size=25, seed=99))
    assert [i["row"] for i in a["items"]] != [i["row"] for i in other["items"]]


def test_seed_is_generated_and_stored_when_missing():
    r = sampling.draw(gl(), P("random", sample_size=5))
    assert isinstance(r["seed"], int)
    assert r["params"]["seed"] == r["seed"]
    again = sampling.draw(gl(), P("random", sample_size=5, seed=r["seed"]))
    assert again["items"] == r["items"]


def test_random_and_systematic_sizes():
    df = ledger([float(i) for i in range(1, 101)])
    r = sampling.draw(df, P("random", sample_size=10, seed=1))
    assert r["sample_size"] == 10 and len({i["row"] for i in r["items"]}) == 10
    s = sampling.draw(df, P("systematic", sample_size=10, seed=1))
    rows = [i["row"] for i in s["items"]]
    assert len(rows) == 10 and s["details"]["interval"] == 10
    assert all(b - a == 10 for a, b in zip(rows, rows[1:], strict=False))


def test_sample_size_is_capped_at_population():
    r = sampling.draw(ledger([1.0, 2.0, 3.0]), P("random", sample_size=10, seed=1))
    assert r["sample_size"] == 3
    assert "capped" in r["details"]["note"]


def test_mus_sample_size_formula():
    p = P("mus", tolerable_misstatement=50_000, expected_misstatement=10_000, confidence=95)
    # 1,000,000 x 3.0 / (50,000 - 10,000 x 1.6) = 88.2 -> 89
    assert sampling.mus_sample_size(1_000_000, p) == 89
    with pytest.raises(sampling.SamplingError, match="too high"):
        sampling.mus_sample_size(1_000_000, P("mus", tolerable_misstatement=10, expected_misstatement=10))
    with pytest.raises(sampling.SamplingError, match="tolerable misstatement"):
        sampling.mus_sample_size(1_000_000, P("mus"))
    with pytest.raises(sampling.SamplingError, match="Confidence"):
        sampling.mus_sample_size(1_000_000, P("mus", tolerable_misstatement=1, confidence=70))


def test_mus_always_picks_items_above_the_interval():
    amounts = [100.0] * 50 + [20_000.0, -15_000.0]  # book value 40,000
    r = sampling.draw(ledger(amounts), P("mus", sample_size=4, seed=7))  # interval 10,000
    by_row = {i["row"]: i for i in r["items"]}
    assert r["details"]["interval"] == 10_000
    assert r["details"]["top_stratum_items"] == 2
    assert by_row[52]["hits"] == 2 and by_row[53]["hits"] in (1, 2)
    assert by_row[52]["reason"].startswith("Top stratum")
    assert sum(i["hits"] for i in r["items"]) == 4


def test_mus_size_from_tolerable_misstatement():
    df = gl()
    r = sampling.draw(df, P("mus", tolerable_misstatement=100_000, seed=3))
    expected = math.ceil(df["amount"].abs().sum() * 3.0 / 100_000)
    assert r["details"]["monetary_units"] == expected
    assert r["details"]["reliability_factor"] == 3.0


def test_population_filters():
    df = ledger([500.0, -500.0, 50.0, -50.0, 0.0])
    debits = sampling.draw(df, P("random", sample_size=10, seed=1, population="debits"))
    assert {i["amount"] for i in debits["items"]} == {500.0, 50.0}
    big = sampling.draw(df, P("random", sample_size=10, seed=1, min_amount=100))
    assert {i["amount"] for i in big["items"]} == {500.0, -500.0}
    assert big["population"] == {"label": "All lines", "lines": 2, "value": 1000.0}
    with pytest.raises(sampling.SamplingError, match="No lines"):
        sampling.draw(df, P("random", sample_size=1, min_amount=10_000))
    with pytest.raises(sampling.SamplingError, match="Population"):
        sampling.draw(df, P("random", sample_size=1, population="odd"))
    with pytest.raises(sampling.SamplingError, match="Method"):
        sampling.draw(df, P("haphazard", sample_size=1))
    with pytest.raises(sampling.SamplingError, match="sample size"):
        sampling.draw(df, P("random"))


def test_excel_workpaper():
    r = sampling.draw(gl(), P("mus", sample_size=20, seed=5))
    sheets = pd.read_excel(io.BytesIO(sampling.to_excel(r, {"Client": "Acme"})), sheet_name=None)
    assert set(sheets) == {"Parameters", "Sample"}
    params = dict(zip(sheets["Parameters"]["Item"], sheets["Parameters"]["Value"], strict=True))
    assert params["Client"] == "Acme" and params["Seed"] == 5
    assert {"Tested by", "Result", "Exception amount"} <= set(sheets["Sample"].columns)
    assert len(sheets["Sample"]) == r["sample_size"]


# --- API ---------------------------------------------------------------------


def setup_engagement(materiality=True):
    c = client.post("/api/clients", json={"name": "Acme"}).json()
    e = client.post(f"/api/clients/{c['id']}/engagements",
                    json={"name": "FY2026", "period_end": "2026-12-31"}).json()
    if materiality:
        client.put(f"/api/engagements/{e['id']}/materiality",
                   json={"benchmark": "revenue", "benchmark_amount": 6_000_000})
    up = client.post(f"/api/engagements/{e['id']}/uploads",
                     files={"file": (GL.name, GL.read_bytes(), "text/csv")}).json()
    return e["id"], up["id"]


def test_options_endpoint():
    r = client.get("/api/sampling/options").json()
    assert [m["id"] for m in r["methods"]] == ["random", "systematic", "mus"]
    assert 95 in r["confidence_levels"]


def test_engagement_sample_is_saved_and_reproducible():
    eid, uid = setup_engagement()
    r = client.post(f"/api/engagements/{eid}/samples",
                    json={"upload_id": uid, "method": "random", "sample_size": 15, "seed": 42})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["seed"] == 42 and body["sample_size"] == 15
    detail = client.get(f"/api/engagements/{eid}").json()
    assert [s["id"] for s in detail["samples"]] == [body["id"]]
    again = client.post(f"/api/engagements/{eid}/samples",
                        json={"upload_id": uid, "method": "random", "sample_size": 15, "seed": 42})
    assert again.json()["result"]["items"] == body["result"]["items"]
    got = client.get(f"/api/samples/{body['id']}").json()
    assert got["result"]["items"] == body["result"]["items"]
    xlsx = client.get(f"/api/samples/{body['id']}/export")
    params = pd.read_excel(io.BytesIO(xlsx.content), sheet_name="Parameters")
    assert "Acme" in params["Value"].tolist()


def test_engagement_mus_defaults_to_performance_materiality():
    eid, uid = setup_engagement()
    r = client.post(f"/api/engagements/{eid}/samples",
                    json={"upload_id": uid, "method": "mus", "seed": 1})
    assert r.status_code == 201, r.text
    assert r.json()["result"]["params"]["tolerable_misstatement"] == 45_000


def test_engagement_mus_without_materiality_needs_input():
    eid, uid = setup_engagement(materiality=False)
    r = client.post(f"/api/engagements/{eid}/samples", json={"upload_id": uid, "method": "mus"})
    assert r.status_code == 400
    assert "tolerable misstatement" in r.json()["detail"]


def test_deleting_upload_removes_its_samples():
    eid, uid = setup_engagement()
    s = client.post(f"/api/engagements/{eid}/samples",
                    json={"upload_id": uid, "method": "systematic", "sample_size": 5}).json()
    assert client.delete(f"/api/uploads/{uid}").status_code in (200, 204)
    assert client.get(f"/api/samples/{s['id']}").status_code == 404


def test_stateless_sample_endpoint():
    files = {"file": (GL.name, GL.read_bytes(), "text/csv")}
    r = client.post("/api/sample", files=files,
                    data={"method": "systematic", "sample_size": "10", "seed": "8"})
    assert r.status_code == 200, r.text
    assert r.json()["sample_size"] == 10
    x = client.post("/api/sample", files=files,
                    data={"method": "systematic", "sample_size": "10", "seed": "8", "format": "xlsx"})
    assert len(pd.read_excel(io.BytesIO(x.content), sheet_name="Sample")) == 10
    bad = client.post("/api/sample", files=files, data={"method": "mus"})
    assert bad.status_code == 400
