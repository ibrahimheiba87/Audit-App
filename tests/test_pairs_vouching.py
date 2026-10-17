"""Account pairs in the general ledger, and vouching sample items to their documents (with the
Claude API faked)."""

import io
from datetime import date, timedelta

import pandas as pd

from app import account_pairs
from tests.test_ai_tools import PDF, activity, fake_api  # noqa: F401 (fixture)
from tests.test_fixed_assets import upload
from tests.test_statements import client, engagement

LEDGER = ["Entry,Date,Account,Description,Debit,Credit,User"]
for n in range(1, 101):
    day = date(2026, 1, 1) + timedelta(days=n)
    LEDGER += [f"S{n},{day},1100 Trade receivables,Sale {n},300,,Ann",
               f"S{n},{day},4000 Sales,Sale {n},,300,Ann"]
LEDGER += [
    "J1,2026-05-01,6000 Rent,Rent,100,,Ann",            # two debits shared over two credits
    "J1,2026-05-01,7000 Office supplies,Paper,100,,Ann",
    "J1,2026-05-01,1000 Bank,Rent,,150,Ann",
    "J1,2026-05-01,2000 Trade payables,Paper,,50,Ann",
    "J2,2026-06-30,6000 Rent,Odd revenue,800,,Bob",     # revenue credited against an expense
    "J2,2026-06-30,4000 Sales,Odd revenue,,800,Bob",
    "J3,2026-07-01,1500 Equipment,Capitalised repairs,900,,Bob",
    "J3,2026-07-01,6900 Repairs expense,Capitalised repairs,,900,Bob",
    "J4,2026-08-01,3500 Retained earnings,Dividend,400,,Bob",
    "J4,2026-08-01,1000 Bank,Dividend,,400,Bob",
    "J5,2026-08-02,6000 Rent,Half an entry,10,,Bob",    # only a debit
]


def test_account_pairs_pair_debits_with_credits_and_flag_odd_ones():
    eid = engagement(with_prior=False)
    gl = upload(client, eid, "gl", "ledger.csv", "\n".join(LEDGER) + "\n")
    r = client.post(f"/api/uploads/{gl}/account-pairs")
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["entries"], out["one_sided"], out["rare_applies"]) == (104, 1, True)
    pairs = {(p["debit"], p["credit"]): p for p in out["pairs"]}
    assert pairs[("receivables", "revenue")]["entries"] == 100
    assert pairs[("receivables", "revenue")]["amount"] == 30000
    # J1: each debit is shared 3:1 between the bank and payables credits.
    assert pairs[("premises", "cash")]["amount"] == 75
    assert pairs[("admin", "payables")]["amount"] == 25
    checks = {c["check"]: [i["entry_id"] for i in c["items"]] for c in out["checks"]}
    assert checks["revenue_credit"] == ["J2"]
    assert checks["capitalised"] == ["J3"]
    assert checks["cash_equity"] == ["J4"]
    assert "S1" not in checks["rare"] and "J2" in checks["rare"]
    assert pairs[("receivables", "revenue")]["flags"] == []
    assert activity(eid, "analysis.account_pairs")

    book = pd.read_excel(io.BytesIO(client.get(f"/api/uploads/{gl}/account-pairs/export").content),
                         sheet_name=None)
    assert {"About", "Statement lines", "Accounts", "Revenue credited"} <= set(book)


def test_account_pairs_need_entry_numbers_and_a_ledger():
    eid = engagement(with_prior=False)
    gl = upload(client, eid, "gl", "flat.csv",
                "Date,Account,Debit,Credit\n2026-01-01,1000 Bank,5,\n2026-01-01,4000 Sales,,5\n")
    r = client.post(f"/api/uploads/{gl}/account-pairs")
    assert r.status_code == 400 and "entry numbers" in r.json()["detail"]
    tb = next(u["id"] for u in client.get(f"/api/engagements/{eid}").json()["uploads"]
               if u["kind"] == "tb")
    assert client.post(f"/api/uploads/{tb}/account-pairs").status_code == 400


def test_flags_for_each_kind_of_pair():
    assert account_pairs.flags_of("cash", "revenue") == []
    assert account_pairs.flags_of("payables", "revenue") == ["revenue_credit"]
    assert account_pairs.flags_of("revenue", "staff_costs") == ["revenue_debit"]
    assert account_pairs.flags_of("retained_earnings", "admin") == ["equity_pl"]
    assert account_pairs.flags_of("revenue", "unknown") == []


SALES = ["Entry,Date,Account,Description,Debit,Credit"]
for n in range(1, 6):
    SALES += [f"V{n},2026-12-{20 + n},6000 Purchases,Acme Supplies invoice INV-{n},{n}000,",
              f"V{n},2026-12-{20 + n},2000 Payables,Acme Supplies invoice INV-{n},,{n}000"]


def sample(eid):
    gl = upload(client, eid, "gl", "purchases.csv", "\n".join(SALES) + "\n")
    r = client.post(f"/api/engagements/{eid}/samples",
                    json={"upload_id": gl, "method": "random", "sample_size": 10,
                          "population": "debits", "seed": 1})
    assert r.status_code == 201, r.text
    return gl, r.json()


def vouch(sid, row, content=PDF, name="invoice.pdf"):
    return client.post(f"/api/samples/{sid}/items/{row}/vouch", data={"language": "en"},
                       files={"file": (name, content, "application/pdf")})


def test_vouching_compares_the_document_with_the_ledger_line(fake_api):  # noqa: F811
    eid = engagement(with_prior=False)
    gl, s = sample(eid)
    item = next(i for i in s["result"]["items"] if i["amount"] == 2000)
    row = item["row"]
    fake_api["answers"] = [
        {"document_type": "invoice", "issuer": "Acme Supplies Ltd", "recipient": "Client",
         "number": "INV-2", "date": "2026-12-20", "net": 2000, "tax": 300, "total": 2300},
        {"issuer": "Acme Supplies Ltd", "number": "INV-2", "date": "2027-01-05",
         "total": 2100},
    ]
    r = vouch(s["id"], row)
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["status"] == "agrees" and v["suggested_amount"] == 2000
    assert {c["check"] for c in v["checks"]} == {"amount", "date", "party", "number"}
    assert "Read this document." in str(fake_api["calls"][0][1])

    # Vouching again replaces it; a later date and another amount differ.
    v2 = vouch(s["id"], row).json()
    assert v2["status"] == "differs" and v2["suggested_amount"] == 2100
    texts = " ".join(c["text"] for c in v2["checks"])
    assert "Cut-off" in texts and "difference of -100.00" in texts
    got = client.get(f"/api/samples/{s['id']}").json()["vouchings"]
    assert [x["id"] for x in got] == [v2["id"]]
    doc = client.get(f"/api/vouchings/{v2['id']}/document")
    assert doc.content == PDF and doc.headers["content-type"] == "application/pdf"

    # The workpaper and the file pack carry it; the file is now kept as support.
    book = pd.read_excel(io.BytesIO(client.get(f"/api/samples/{s['id']}/export").content),
                         sheet_name=None)
    assert book["Vouching"]["Result"].tolist() == ["Differs from the document"]
    assert client.delete(f"/api/uploads/{gl}").status_code == 409
    assert activity(eid, "sample.vouch") and activity(eid, "sample.vouch_read")

    assert client.delete(f"/api/vouchings/{v2['id']}").status_code == 204
    assert client.get(f"/api/samples/{s['id']}/vouchings").json() == []
    assert client.delete(f"/api/uploads/{gl}").status_code == 204


def test_vouching_needs_the_ai_a_sample_row_and_an_unlocked_file(fake_api, monkeypatch):  # noqa: F811
    eid = engagement(with_prior=False)
    _, s = sample(eid)
    assert vouch(s["id"], 999).status_code == 404
    assert vouch(s["id"], s["result"]["items"][0]["row"], content=b"not a pdf").status_code == 400
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert vouch(s["id"], s["result"]["items"][0]["row"]).status_code == 503
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert client.post(f"/api/engagements/{eid}/lock", json={}).status_code == 200
    assert vouch(s["id"], s["result"]["items"][0]["row"]).status_code == 409


def test_an_unreadable_amount_is_left_for_the_auditor(fake_api):  # noqa: F811
    eid = engagement(with_prior=False)
    _, s = sample(eid)
    fake_api["answers"] = [{"issuer": "Someone Else", "date": "2026-12-22"}]
    v = vouch(s["id"], s["result"]["items"][0]["row"]).json()
    assert v["status"] == "check" and v["suggested_amount"] is None
