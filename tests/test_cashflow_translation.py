"""The draft cash flow statement, and translating the statements into another currency."""

import io

import pandas as pd

from app import cashflow, statements
from tests.test_statements import client, engagement, map_all

PRIOR = pd.DataFrame([
    ("1000", "Cash", 100), ("1200", "Receivables", 300), ("1300", "Inventory", 200),
    ("1500", "Equipment", 1000), ("2000", "Payables", -250), ("2200", "Tax payable", -20),
    ("2500", "Bank loan", -400), ("3000", "Share capital", -500),
    ("3100", "Retained earnings", -230), ("4000", "Revenue", -2000),
    ("5000", "Cost of sales", 1200), ("6000", "Depreciation", 100), ("6500", "Rent", 400),
    ("8000", "Income tax", 100)], columns=["account", "description", "amount"])
CURRENT = pd.DataFrame([
    ("1000", "Cash", 280), ("1200", "Receivables", 380), ("1300", "Inventory", 150),
    ("1500", "Equipment", 1100), ("2000", "Payables", -300), ("2200", "Tax payable", -30),
    ("2500", "Bank loan", -350), ("3000", "Share capital", -600),
    ("3100", "Retained earnings", -330),  # 230 + last year's 200 profit, less 100 dividends
    ("4000", "Revenue", -2500), ("5000", "Cost of sales", 1500),
    ("6000", "Depreciation", 120), ("6500", "Rent", 440), ("7000", "Interest expense", 20),
    ("8000", "Income tax", 120)], columns=["account", "description", "amount"])
MAPPING = {"1000": "cash", "1200": "receivables", "1300": "inventory", "1500": "ppe",
           "2000": "payables", "2200": "tax_payable", "2500": "borrowings",
           "3000": "share_capital", "3100": "retained_earnings", "4000": "revenue",
           "5000": "cost_of_sales", "6000": "depreciation", "6500": "premises",
           "7000": "finance_costs", "8000": "income_tax"}


def test_cash_flow_adds_up_to_the_movement_in_cash():
    data = statements.build(CURRENT, PRIOR, MAPPING, [])
    cf = cashflow.compute(data)
    amounts = {r["label"]: r["amount"] for r in cf["rows"]}
    assert amounts["Profit before tax"] == 420
    assert amounts["(Increase) / decrease in trade and other receivables"] == -80
    assert amounts["Income tax paid"] == -110
    assert amounts["Purchase of property, plant and equipment (net)"] == -220
    assert amounts["Dividends paid and other movements in reserves"] == -100
    assert amounts["Proceeds from issue of shares"] == 100
    assert cf["net_change"] == 180 and cf["difference"] == 0 and cf["notes"] == []
    assert cashflow.compute(statements.build(CURRENT, None, MAPPING, [])) is None


def test_cash_flow_shows_what_is_left_over():
    data = statements.build(CURRENT, PRIOR, {**MAPPING, "1300": None}, [])
    cf = cashflow.compute(data)
    assert cf["difference"] != 0 and "left over" in cf["notes"][0]


def test_cash_flow_in_the_lead_sheets():
    eid = engagement()
    map_all(eid)
    data = client.get(f"/api/engagements/{eid}/statements").json()
    assert data["cash_flow"]["rows"][0]["label"] == "Cash flows from operating activities"
    book = pd.read_excel(io.BytesIO(
        client.get(f"/api/engagements/{eid}/statements/export").content), sheet_name=None)
    assert "Cash flow" in book


def test_translation_at_closing_average_and_historical_rates():
    eid = engagement(with_prior=False)
    map_all(eid)
    assert client.get(f"/api/engagements/{eid}/translation").json() == {
        "settings": None, "result": None}
    body = {"functional": "egp", "presentation": "USD", "closing_rate": 0.02,
            "average_rate": 0.025, "equity_rate": 0.05}
    assert client.put(f"/api/engagements/{eid}/translation",
                      json={**body, "presentation": "EGP"}).status_code == 400
    assert client.put(f"/api/engagements/{eid}/translation",
                      json={**body, "functional": "E1P"}).status_code == 400
    r = client.put(f"/api/engagements/{eid}/translation", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["settings"]["functional"] == "EGP"
    res = out["result"]
    rates = {x["line"]: (x["rate"], x["basis"]) for x in res["lines"]}
    assert rates["cash"] == (0.02, "closing") and rates["revenue"] == (0.025, "average")
    assert rates["share_capital"] == (0.05, "historical")
    bs = {x["label"]: x for x in res["statements"]["balance_sheet"]}
    assert bs["Total assets"]["presented"] == bs["Total liabilities and equity"]["presented"]
    assert bs["Foreign currency translation reserve"]["presented"] == res["reserve"]
    func = {x["label"]: x for x in res["statements"]["income_statement"]}
    assert func["Revenue"]["presented"] == round(func["Revenue"]["functional"] * 0.025, 2)

    # Opening reserves as already reported, and last year's reserve for the year's movement.
    r = client.put(f"/api/engagements/{eid}/translation",
                   json={**body, "opening_reserves": 9000, "opening_translation_reserve": -100})
    res = r.json()["result"]
    re_line = next(x for x in res["lines"] if x["line"] == "retained_earnings")
    assert re_line["presented"] == 9000 and re_line["basis"] == "as reported"
    assert res["reserve_movement"] == round(res["reserve"] + 100, 2)

    book = pd.read_excel(io.BytesIO(
        client.get(f"/api/engagements/{eid}/statements/export").content), sheet_name=None)
    assert "USD" in book["Translation"].columns
    assert client.get(f"/api/engagements/{eid}/translation/export").status_code == 200
    acts = [a["action"] for a in client.get("/api/activity",
                                            params={"engagement_id": eid}).json()]
    assert acts.count("translation.save") == 2
    assert client.delete(f"/api/engagements/{eid}/translation").status_code == 204
    assert client.delete(f"/api/engagements/{eid}/translation").status_code == 404


def test_dividends_booked_to_reserves_stay_in_equity_when_translated():
    from types import SimpleNamespace

    from app import translation
    prior = pd.DataFrame([("1000", "Cash", 50), ("3100", "Retained earnings", -30),
                          ("4000", "Revenue", -20)], columns=["account", "description", "amount"])
    cur = pd.DataFrame([("1000", "Cash", 40), ("3100", "Retained earnings", -40)],
                       columns=["account", "description", "amount"])  # 30 + 20 less 10 paid
    data = statements.build(cur, prior, {"1000": "cash", "3100": "retained_earnings",
                                         "4000": "revenue"}, [])
    t = SimpleNamespace(closing_rate=2, average_rate=2, equity_rate=2, opening_reserves=100,
                        opening_translation_reserve=None, functional="EGP",
                        presentation="USD", updated_by="", updated_at=None)
    t.updated_at = __import__("datetime").datetime(2026, 1, 1)
    out = translation.translate(data, t)
    assert out["reserve"] == 0


def test_translation_download_needs_mapped_accounts():
    eid = engagement(with_prior=False)
    client.put(f"/api/engagements/{eid}/translation", json={
        "functional": "EGP", "presentation": "USD", "closing_rate": 1, "average_rate": 1,
        "equity_rate": 1})
    assert client.get(f"/api/engagements/{eid}/translation/export").status_code == 400
