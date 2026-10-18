"""Fixes from the October 2026 review of rounds 7 and 8."""

from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from app import account_pairs, doctext, statements, translation
from tests.test_ai_tools import fake_api  # noqa: F401 (fixture)
from tests.test_auth import add_user, admin, join
from tests.test_fixed_assets import upload
from tests.test_group_vat import LEDGER as VAT_LEDGER
from tests.test_pairs_vouching import PDF, SALES
from tests.test_statements import client, engagement


def test_closing_entries_are_in_no_check():
    e = SimpleNamespace(account_mappings=[SimpleNamespace(account=a, line=line) for a, line in [
        ("4000", "revenue"), ("3000", "retained_earnings"), ("1100", "receivables"),
        ("6000", "admin")]])
    rows = [("E1", "1100", 100), ("E1", "4000", -100),
            ("C1", "4000", 100), ("C1", "6000", -40), ("C1", "3000", -60)]
    df = pd.DataFrame([{"entry_id": a, "account": b, "amount": float(c), "row": i + 1}
                       for i, (a, b, c) in enumerate(rows)])
    out = account_pairs.analyse(df, e)
    assert out["closing_entries"] == 1
    assert all("C1" not in [i["entry_id"] for i in c["items"]] for c in out["checks"])


def test_an_unbalanced_trial_balance_stays_out_of_the_translation_reserve():
    m = {"cash": "cash", "sc": "share_capital", "re": "retained_earnings", "rev": "revenue",
         "adm": "admin"}
    t = SimpleNamespace(functional="EGP", presentation="USD", closing_rate=2.0,
                        average_rate=1.5, equity_rate=1.0, opening_reserves=None,
                        opening_translation_reserve=None, updated_by="",
                        updated_at=datetime(2026, 1, 1))

    def reserve(rows):
        tb = pd.DataFrame([{"account": a, "amount": b} for a, b in rows])
        return translation.translate(statements.build(tb, None, m, []), t)

    balanced = reserve([("cash", 100), ("sc", -50), ("re", -30), ("rev", -40), ("adm", 20)])
    assert balanced["notes"] == []
    # 10 of the cash is in an unmapped account: the reserve is the same, the gap shows.
    gap = reserve([("cash", 90), ("x", 10), ("sc", -50), ("re", -30), ("rev", -40),
                   ("adm", 20)])
    assert gap["reserve"] == balanced["reserve"]
    assert gap["notes"] == [translation.UNBALANCED]


def test_vat_settlement_entries_are_left_out():
    eid = engagement(with_prior=False)
    settle = ["V1,2026-01-31,2210 VAT output,Settle,140,",
              "V1,2026-01-31,1410 VAT input recoverable,Settle,,70",
              "V1,2026-01-31,2300 VAT payable,Settle,,70"]
    gl = upload(client, eid, "gl", "ledger.csv", "\n".join(VAT_LEDGER + settle) + "\n")
    accounts = {"output_accounts": ["2210 VAT output"],
                "input_accounts": ["1410 VAT input recoverable"]}
    r = client.put(f"/api/engagements/{eid}/vat",
                   json={"upload_id": gl, "rate_pct": 14, **accounts})
    assert r.status_code == 200, r.text
    out = client.post(f"/api/engagements/{eid}/vat/returns", json={
        "period_start": "2026-01-01", "period_end": "2026-01-31", "taxable_sales": 1000,
        "output_vat": 140, "input_vat": 70}).json()
    [row] = out["result"]["rows"]
    assert (row["ledger_output"], row["ledger_input"], row["flags"]) == (140, 70, [])
    both = client.put(f"/api/engagements/{eid}/vat", json={
        "upload_id": gl, "rate_pct": 14, "output_accounts": ["2210 VAT output"],
        "input_accounts": ["2210 VAT output"]})
    assert both.status_code == 400


@pytest.mark.usefixtures("real_auth")
def test_only_reviewers_remove_or_replace_vouched_documents(fake_api):  # noqa: F811
    a = admin()
    prep = add_user(a, "preparer")
    acme = a.post("/api/clients", json={"name": "Acme"}).json()
    eid = a.post(f"/api/clients/{acme['id']}/engagements",
                 json={"name": "FY2026", "period_end": "2026-12-31"}).json()["id"]
    join(a, eid, prep)
    gl = upload(a, eid, "gl", "purchases.csv", "\n".join(SALES) + "\n")
    s = a.post(f"/api/engagements/{eid}/samples", json={
        "upload_id": gl, "method": "random", "sample_size": 10, "population": "debits",
        "seed": 1}).json()
    row = s["result"]["items"][0]["row"]
    fake_api["answers"] = [{"issuer": "Acme", "total": 1}]
    files = {"file": ("invoice.pdf", PDF, "application/pdf")}
    v = prep.post(f"/api/samples/{s['id']}/items/{row}/vouch", data={"language": "en"},
                  files=files)
    assert v.status_code == 201, v.text
    again = prep.post(f"/api/samples/{s['id']}/items/{row}/vouch", data={"language": "en"},
                      files=files)
    assert again.status_code == 403
    assert prep.delete(f"/api/vouchings/{v.json()['id']}").status_code == 403
    assert a.delete(f"/api/vouchings/{v.json()['id']}").status_code == 204


@pytest.mark.parametrize(("path", "body"), [
    ("leases", {"name": "  ", "start_date": "2026-01-01", "term_months": 24, "payment": 100,
                "rate_pct": 5}),
    ("loans", {"name": "  "}),
])
def test_names_of_only_spaces_are_refused(path, body):
    eid = engagement(with_prior=False)
    assert client.post(f"/api/engagements/{eid}/{path}", json=body).status_code == 422


def test_a_reused_file_id_in_another_engagement_is_read_again():
    a = engagement(with_prior=False, client_name="A")
    b = engagement(with_prior=False, client_name="B")
    words = b"standard bank letter qwertyzz"

    def attach(eid, name):
        f = client.post(f"/api/engagements/{eid}/findings", json={"detail": "d"}).json()["id"]
        r = client.post(f"/api/findings/{f}/evidence", files={"file": (name, words)})
        return f, r.json()["attachments"][0]["id"]

    _, first = attach(a, "letter.txt")
    assert client.delete(f"/api/evidence/{first}").status_code == 204
    fb, second = attach(b, "B-confirmation.txt")
    doctext.backfill()
    found = client.get("/api/search", params={"q": "qwertyzz"}).json()
    docs = [i for g in found["groups"] if g["kind"] == "document" for i in g["items"]]
    assert [(i["engagement_id"], i["title"]) for i in docs] == [(b, "B-confirmation.txt")]
    assert second  # SQLite may or may not reuse the id; either way B's file is found


def test_saving_a_component_keeps_fields_left_out():
    from tests.test_group_vat import group_and_component
    parent, sub = group_and_component()
    res = client.post(f"/api/engagements/{parent}/group/components",
                      json={"component_engagement_id": sub}).json()
    cid = res["components"][0]["id"]
    assert client.patch(f"/api/group-components/{cid}",
                        json={"ownership_pct": 100, "scope": "full",
                              "note": "Visited in May"}).status_code == 200
    out = client.patch(f"/api/group-components/{cid}", json={"ownership_pct": 80, "scope": "full"}).json()
    assert out["components"][0]["note"] == "Visited in May"


@pytest.mark.usefixtures("real_auth")
def test_only_reviewers_post_or_delete_eliminations():
    a = admin()
    prep = add_user(a, "preparer")
    acme = a.post("/api/clients", json={"name": "Acme"}).json()
    eid = a.post(f"/api/clients/{acme['id']}/engagements",
                 json={"name": "FY2026", "period_end": "2026-12-31"}).json()["id"]
    join(a, eid, prep)
    entry = {"description": "Intra-group", "lines": [{"line": "payables", "debit": 5},
                                                      {"line": "receivables", "credit": 5}]}
    assert prep.post(f"/api/engagements/{eid}/group/entries", json=entry).status_code == 403
    [x] = a.post(f"/api/engagements/{eid}/group/entries", json=entry).json()["entries"]
    assert prep.delete(f"/api/consolidation-entries/{x['id']}").status_code == 403
    assert a.delete(f"/api/consolidation-entries/{x['id']}").status_code == 200
