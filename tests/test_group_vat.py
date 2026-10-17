"""Group consolidation with components and eliminations, and the VAT reconciliation."""

import io

import pandas as pd

from tests.test_fixed_assets import upload
from tests.test_statements import client, engagement, map_all


def group_and_component():
    parent = engagement(with_prior=False)
    sub = engagement(with_prior=False, client_name="Subsidiary")
    map_all(parent)
    map_all(sub)
    return parent, sub


def line(rows, label):
    return next(r for r in rows if r["label"] == label)


def test_consolidation_adds_components_and_eliminations():
    parent, sub = group_and_component()
    g = client.get(f"/api/engagements/{parent}/group").json()
    assert g["result"] is None and sub in [c["id"] for c in g["choices"]]

    r = client.post(f"/api/engagements/{parent}/group/components",
                    json={"component_engagement_id": sub, "ownership_pct": 80,
                          "materiality": 10**9, "scope": "analytical"})
    assert r.status_code == 201, r.text
    g = r.json()
    [comp] = g["components"]
    assert comp["significant"] and comp["revenue_pct"] == 50.0
    assert any("not in full scope" in f for f in comp["flags"])
    res = g["result"]
    assert [c["key"] for c in res["columns"]][-1] == "group"
    revenue = line(res["income_statement"], "Revenue")["values"]
    assert revenue[-1] == revenue[0] + revenue[1]
    a = res["attributable"]
    assert a["profit_nci"] == round(0.2 * res["profit"] / 2, 2)

    assert client.post(f"/api/engagements/{parent}/group/components",
                       json={"component_engagement_id": sub}).status_code == 409
    assert client.post(f"/api/engagements/{sub}/group/components",
                       json={"component_engagement_id": parent}).status_code == 400
    assert client.post(f"/api/engagements/{parent}/group/components",
                       json={"component_engagement_id": parent}).status_code == 400

    bad = {"description": "Intra-group", "lines": [
        {"line": "payables", "debit": 1000}, {"line": "receivables", "credit": 900}]}
    assert client.post(f"/api/engagements/{parent}/group/entries", json=bad).status_code == 400
    bad["lines"][1]["credit"] = 1000
    g = client.post(f"/api/engagements/{parent}/group/entries", json=bad).json()
    res = g["result"]
    recv = line(res["balance_sheet"], "Trade and other receivables")["values"]
    assert recv[-1] == round(recv[0] + recv[1] - 1000, 2) and recv[2] == -1000

    book = pd.read_excel(io.BytesIO(
        client.get(f"/api/engagements/{parent}/group/export").content), sheet_name=None)
    assert {"Income statement", "Balance sheet", "Components", "Eliminations"} <= set(book)

    entry = g["entries"][0]["id"]
    assert client.delete(f"/api/consolidation-entries/{entry}").json()["entries"] == []
    cid = g["components"][0]["id"]
    r = client.patch(f"/api/group-components/{cid}",
                     json={"ownership_pct": 100, "scope": "full", "materiality": 5})
    assert r.json()["result"]["attributable"]["profit_nci"] == 0
    # Deleting the component's engagement takes it out of the group.
    assert client.delete(f"/api/engagements/{sub}").status_code == 204
    assert client.get(f"/api/engagements/{parent}/group").json()["components"] == []
    acts = {x["action"] for x in client.get("/api/activity",
                                            params={"engagement_id": parent}).json()}
    assert {"group.component_add", "group.entry_add", "group.component_update"} <= acts


def test_translated_component_is_added_in_the_presentation_currency():
    parent, sub = group_and_component()
    client.put(f"/api/engagements/{sub}/translation", json={
        "functional": "EGP", "presentation": "USD", "closing_rate": 0.5, "average_rate": 0.4,
        "equity_rate": 1})
    res = client.post(f"/api/engagements/{parent}/group/components",
                      json={"component_engagement_id": sub}).json()["result"]
    revenue = line(res["income_statement"], "Revenue")["values"]
    assert revenue[1] == round(revenue[0] * 0.4, 2)
    assert any(r["label"] == "Foreign currency translation reserve"
               for r in res["balance_sheet"])


LEDGER = ["Entry,Date,Account,Description,Debit,Credit"]
for m in (1, 2, 3):
    LEDGER += [f"S{m},2026-0{m}-15,1200 Receivables,Sale,1140,",
               f"S{m},2026-0{m}-15,4000 Sales,Sale,,1000",
               f"S{m},2026-0{m}-15,2210 VAT output,Sale,,140",
               f"P{m},2026-0{m}-20,5000 Purchases,Buy,500,",
               f"P{m},2026-0{m}-20,1410 VAT input recoverable,Buy,70,",
               f"P{m},2026-0{m}-20,2000 Payables,Buy,,570"]


def test_vat_returns_against_the_ledger():
    eid = engagement(with_prior=False)
    gl = upload(client, eid, "gl", "ledger.csv", "\n".join(LEDGER) + "\n")
    v = client.get(f"/api/engagements/{eid}/vat").json()
    assert v["result"] is None and v["ledgers"][0]["id"] == gl
    r = client.put(f"/api/engagements/{eid}/vat", json={"upload_id": gl, "rate_pct": 14})
    assert r.status_code == 200, r.text
    sug = r.json()["suggested"]
    assert sug["output"] == ["2210 VAT output"] and sug["input"] == ["1410 VAT input recoverable"]
    client.put(f"/api/engagements/{eid}/vat", json={
        "upload_id": gl, "rate_pct": 14, "output_accounts": sug["output"],
        "input_accounts": sug["input"]})
    ok = {"period_start": "2026-01-01", "period_end": "2026-01-31", "taxable_sales": 1000,
          "output_vat": 140, "input_vat": 70}
    assert client.post(f"/api/engagements/{eid}/vat/returns",
                       json={**ok, "period_end": "2025-12-01"}).status_code == 400
    client.post(f"/api/engagements/{eid}/vat/returns", json=ok)
    out = client.post(f"/api/engagements/{eid}/vat/returns", json={
        "period_start": "2026-02-01", "period_end": "2026-02-28", "taxable_sales": 800,
        "output_vat": 140, "input_vat": 70}).json()
    rows = out["result"]["rows"]
    assert rows[0]["flags"] == []
    assert rows[1]["diff_sales"] == -200 and set(rows[1]["flags"]) == {"sales", "rate"}
    assert out["result"]["uncovered_sales"] == 1000  # March has no return
    assert "no return" in out["result"]["notes"][0]

    book = pd.read_excel(io.BytesIO(client.get(f"/api/engagements/{eid}/vat/export").content),
                         sheet_name="VAT reconciliation")
    assert book["Sales difference"].tolist() == [0, -200]
    rid = out["returns"][1]["id"]
    assert len(client.delete(f"/api/vat-returns/{rid}").json()["returns"]) == 1
    tb = next(u["id"] for u in client.get(f"/api/engagements/{eid}").json()["uploads"]
              if u["kind"] == "tb")
    assert client.put(f"/api/engagements/{eid}/vat", json={"upload_id": tb}).status_code == 400


def test_a_locked_group_keeps_its_components():
    parent, sub = group_and_component()
    client.post(f"/api/engagements/{parent}/group/components",
                json={"component_engagement_id": sub})
    assert client.post(f"/api/engagements/{parent}/lock", json={}).status_code == 200
    r = client.delete(f"/api/engagements/{sub}")
    assert r.status_code == 409 and "locked" in r.json()["detail"]
    assert len(client.get(f"/api/engagements/{parent}/group").json()["components"]) == 1


def test_deleting_a_component_is_logged_on_the_group():
    parent, sub = group_and_component()
    client.post(f"/api/engagements/{parent}/group/components",
                json={"component_engagement_id": sub})
    assert client.delete(f"/api/engagements/{sub}").status_code == 204
    acts = {x["action"] for x in client.get("/api/activity",
                                            params={"engagement_id": parent}).json()}
    assert "group.component_remove" in acts


def test_a_translated_component_out_of_balance_is_still_flagged():
    parent, sub = group_and_component()
    accounts = client.get(f"/api/engagements/{sub}/statements").json()["accounts"]
    cash = next(a["account"] for a in accounts if a["line"] == "cash")
    client.put(f"/api/engagements/{sub}/mapping", json={"mappings": {cash: None}})
    client.put(f"/api/engagements/{sub}/translation", json={
        "functional": "EGP", "presentation": "USD", "closing_rate": 1, "average_rate": 1,
        "equity_rate": 1})
    g = client.post(f"/api/engagements/{parent}/group/components",
                    json={"component_engagement_id": sub}).json()
    res = g["result"]
    assert res["reserve"] == 0
    assert {u["label"] for u in res["unbalanced"]} >= {"Group"}
    assert any("translated into USD" in f for f in g["components"][0]["flags"])


def test_a_ledger_without_dates_is_refused_for_vat():
    eid = engagement(with_prior=False)
    no_dates = "\n".join(",".join(x for i, x in enumerate(row.split(",")) if i != 1)
                         for row in LEDGER) + "\n"
    gl = upload(client, eid, "gl", "ledger.csv", no_dates)
    r = client.put(f"/api/engagements/{eid}/vat", json={"upload_id": gl, "rate_pct": 14})
    assert r.status_code == 400 and "no dates" in r.json()["detail"]
    assert client.get(f"/api/engagements/{eid}/vat").status_code == 200
