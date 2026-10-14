"""The journal entry tests added with risk scoring, and the risk score itself."""

import io
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from app import checks
from app.main import app

client = TestClient(app)
SAMPLE = Path(__file__).resolve().parent.parent / "sample_data" / "sample_gl.csv"


def ledger(rows):
    df = pd.DataFrame(rows)
    df.insert(0, "row", range(2, len(df) + 2))
    for col in ("date", "posted"):
        if col in df:
            df[col] = pd.to_datetime(df[col], format="mixed")
    return df


def test_weekend_follows_the_chosen_days():
    df = ledger([
        {"date": "2026-03-06", "amount": 10.0},  # Friday
        {"date": "2026-03-07", "amount": 10.0},  # Saturday
        {"date": "2026-03-08", "amount": 10.0},  # Sunday
    ])
    days = lambda weekend: [f["detail"] for f in checks.check_weekend(  # noqa: E731
        df, checks.Settings(weekend=weekend)).findings]
    assert days("sat-sun") == ["Recorded on a Saturday", "Recorded on a Sunday"]
    assert days("fri-sat") == ["Recorded on a Friday", "Recorded on a Saturday"]
    assert days("fri") == ["Recorded on a Friday"]
    assert "Friday and Saturday" in checks.check_weekend(df, checks.Settings(weekend="fri-sat")).note


def test_after_hours_needs_times_and_flags_late_entries():
    dates_only = ledger([{"date": "2026-03-02", "amount": 1.0}] * 3)
    assert checks.check_after_hours(dates_only, checks.Settings()).status == "skipped"
    df = ledger([
        {"date": "2026-03-02", "posted": "2026-03-02 06:59", "amount": 1.0},
        {"date": "2026-03-02", "posted": "2026-03-02 07:00", "amount": 1.0},
        {"date": "2026-03-02", "posted": "2026-03-02 18:59", "amount": 1.0},
        {"date": "2026-03-02", "posted": "2026-03-02 23:15", "amount": 1.0},
    ])
    res = checks.check_after_hours(df, checks.Settings())
    assert [(f["row"], f["detail"]) for f in res.findings] == [
        (2, "Recorded at 06:59"), (5, "Recorded at 23:15")]


def test_keywords_match_word_starts_and_arabic_with_prefixes():
    df = ledger([
        {"description": "Year-end Adjustment", "amount": 1.0},
        {"description": "Readjusted pricing", "amount": 1.0},
        {"description": "قيد والتسوية الختامية", "amount": 1.0},
        {"description": "Office rent", "amount": 1.0},
        {"description": None, "amount": 1.0},
    ])
    res = checks.check_keywords(df, checks.Settings())
    assert [f["row"] for f in res.findings] == [2, 4]
    assert res.findings[0]["detail"] == 'Description contains "adjust"'
    assert checks.check_keywords(df, checks.Settings(keywords=())).status == "skipped"


def test_parse_keywords():
    assert checks.parse_keywords(None) == checks.DEFAULT_KEYWORDS
    assert checks.parse_keywords("") == ()
    assert checks.parse_keywords(" plug ,Plug، تسوية\nsuspense  account ") == (
        "plug", "تسوية", "suspense account")


def test_rare_accounts_skip_a_trial_balance():
    tb = ledger([{"account": f"A{i}", "amount": 1.0} for i in range(60)])
    assert checks.check_rare_accounts(tb, checks.Settings()).status == "skipped"
    gl = ledger([{"account": "Cash", "amount": 1.0}] * 30 + [{"account": "Sales", "amount": 1.0}] * 30
                + [{"account": "Suspense", "amount": 5.0}])
    res = checks.check_rare_accounts(gl, checks.Settings())
    assert [(f["account"], f["detail"]) for f in res.findings] == [
        ("Suspense", "Account used on only 1 line")]


def test_gaps_in_entry_numbers():
    ids = [f"JE{n:04d}" for n in range(1, 21) if n not in (5, 9, 10, 11)] + ["JE9999"]
    df = ledger([{"entry_id": i, "amount": 1.0} for i in ids])
    res = checks.check_gaps(df, checks.Settings())
    assert [(f["entry_id"], f["detail"]) for f in res.findings] == [
        ("JE0005", "1 missing entry number"), ("JE0009–JE0011", "3 missing entry numbers")]
    assert res.note == "JE0001–JE0020: 4 missing."  # JE9999 is a separate block, not a gap


def test_gaps_skip_codes_that_are_not_a_sequence():
    df = ledger([{"entry_id": str(n * 37), "amount": 1.0} for n in range(1, 20)])
    res = checks.check_gaps(df, checks.Settings())
    assert res.findings == [] and "not a running sequence" in res.note


def test_risk_score_adds_up_checks_per_entry():
    df = ledger([
        # Weekend, round, keyword: three checks on one entry.
        {"entry_id": "J1", "date": "2026-03-07", "description": "manual plug", "amount": 5000.0},
        {"entry_id": "J1", "date": "2026-03-07", "description": "manual plug", "amount": -5000.0},
        # Weekend only.
        {"entry_id": "J2", "date": "2026-03-08", "description": "Rent", "amount": 120.0},
        {"entry_id": "J2", "date": "2026-03-08", "description": "Rent", "amount": -120.0},
        {"entry_id": "J3", "date": "2026-03-09", "description": "Rent", "amount": 130.0},
        {"entry_id": "J3", "date": "2026-03-09", "description": "Rent", "amount": -130.0},
    ])
    risk = checks.run_all(df, checks.Settings(period_end=pd.Timestamp("2026-12-31").date()))["risk"]
    assert risk["basis"] == "entry" and risk["scored"] == 2
    first, second = risk["entries"]
    assert first["entry_id"] == "J1" and first["lines"] == 2 and first["amount"] == 5000
    assert first["account"] is None and first["accounts"] == 0
    assert set(first["reasons"]) == {"weekend", "round_amounts", "keywords"}
    assert first["score"] > second["score"] and second["entry_id"] == "J2"
    assert [first["rank"], second["rank"]] == [1, 2]
    assert all(0 <= e["score"] <= 100 for e in risk["entries"])


def test_risk_size_uses_performance_materiality():
    rows = [{"date": "2026-03-07", "amount": a} for a in (100.0, 1000.0)]
    small = checks.run_all(ledger(rows), checks.Settings(performance_materiality=1_000_000))
    big = checks.run_all(ledger(rows), checks.Settings(performance_materiality=1000.01))
    assert small["risk"]["basis"] == "line" and big["risk"]["size_basis"] == "performance materiality"
    score = lambda r, row: next(e["score"] for e in r["risk"]["entries"] if e["row"] == row)  # noqa: E731
    assert score(big, 3) - score(small, 3) == checks.SIZE_POINTS


def test_quick_audit_takes_weekend_and_keywords():
    files = {"file": (SAMPLE.name, SAMPLE.read_bytes(), "text/csv")}
    r = client.post("/api/audit", files=files, data={"weekend": "fri-sat", "keywords": "toner"})
    assert r.status_code == 200, r.text
    body = r.json()
    kw = next(c for c in body["checks"] if c["id"] == "keywords")
    assert kw["note"] == "Looking for: toner." and kw["flagged"] > 0
    assert "Friday and Saturday" in next(c for c in body["checks"] if c["id"] == "weekend")["note"]
    assert body["risk"]["entries"][0]["rank"] == 1
    bad = client.post("/api/audit", files=files, data={"weekend": "mon"})
    assert bad.status_code == 400


def test_check_options():
    opts = client.get("/api/checks/options").json()
    assert [w["id"] for w in opts["weekends"]] == ["sat-sun", "fri-sat", "fri"]
    assert "تسوية" in opts["keywords"]


def test_engagement_run_saves_weekend_and_keywords():
    c = client.post("/api/clients", json={"name": "Nile Co"}).json()
    e = client.post(f"/api/clients/{c['id']}/engagements",
                    json={"name": "FY2026", "period_end": "2026-12-31"}).json()
    files = {"file": (SAMPLE.name, SAMPLE.read_bytes(), "text/csv")}
    up = client.post(f"/api/engagements/{e['id']}/uploads", files=files, data={"kind": "gl"}).json()
    r = client.post(f"/api/engagements/{e['id']}/runs", json={
        "upload_id": up["id"], "weekend": "fri-sat", "keywords": ["Manual", " ", "toner"]})
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["settings"]["weekend"] == "fri-sat"
    assert run["settings"]["keywords"] == ["Manual", "toner"]
    report = client.get(f"/api/runs/{run['id']}/report")
    ranking = pd.read_excel(io.BytesIO(report.content), sheet_name="Risk ranking")
    assert ranking["Rank"].tolist()[:3] == [1, 2, 3] and ranking["Why"].notna().all()
    findings = client.get(f"/api/engagements/{e['id']}/findings?check=keywords").json()
    assert findings["findings"] and all(f["check"] == "keywords" for f in findings["findings"])
    assert client.post(f"/api/engagements/{e['id']}/runs",
                       json={"upload_id": up["id"], "weekend": "mon"}).status_code == 422


def test_keywords_starting_with_punctuation_match():
    df = ledger([{"description": "(manual) fix", "amount": 1.0},
                 {"description": "#plug x", "amount": 1.0}])
    res = checks.check_keywords(df, checks.Settings(keywords=checks.parse_keywords("(manual), #plug")))
    assert [f["row"] for f in res.findings] == [2, 3]


def test_dates_with_time_zones_run():
    csv = (b"Date,Account,Amount\n2026-01-03T22:00:00+02:00,Cash,100\n"
           b"2026-01-04T09:00:00+02:00,Sales,-100\n")
    r = client.post("/api/audit", files={"file": ("tz.csv", csv, "text/csv")})
    assert r.status_code == 200, r.text
    after = next(c for c in r.json()["checks"] if c["id"] == "after_hours")
    assert after["flagged"] == 1  # 22:00 local time, not converted to UTC


def test_dates_with_different_offsets_keep_their_clock_time():
    csv = (b"Date,Account,Amount\n2026-03-28T22:00:00+02:00,Cash,100\n"
           b"2026-03-30T09:00:00+03:00,Sales,-100\n")
    r = client.post("/api/audit", files={"file": ("dst.csv", csv, "text/csv")})
    assert r.status_code == 200, r.text
    assert next(c for c in r.json()["checks"] if c["id"] == "after_hours")["flagged"] == 1


def test_after_hours_ignores_rows_without_a_time():
    df = ledger([
        {"date": "2026-03-02 23:30", "amount": 1.0},
        {"date": "2026-03-03", "amount": 1.0},  # date only: not midnight work
        {"date": "2026-03-04", "amount": 1.0},
    ])
    res = checks.check_after_hours(df, checks.Settings())
    assert [f["row"] for f in res.findings] == [2]


def test_rare_accounts_follow_the_upload_kind():
    small_gl = ledger([{"account": "Cash", "amount": 1.0}] * 5 + [{"account": "Suspense", "amount": 1.0}])
    assert checks.check_rare_accounts(small_gl, checks.Settings()).status == "skipped"  # guessed TB
    res = checks.check_rare_accounts(small_gl, checks.Settings(kind="gl"))
    assert [f["account"] for f in res.findings] == ["Suspense"]
    big_tb = ledger([{"account": "Cash", "amount": 1.0}] * 60)
    assert checks.check_rare_accounts(big_tb, checks.Settings(kind="tb")).status == "skipped"


def test_gaps_split_short_sequences_at_big_jumps():
    ids = [f"JE{n:04d}" for n in range(1, 21) if n != 7] + ["JE0099"]
    res = checks.check_gaps(ledger([{"entry_id": i, "amount": 1.0} for i in ids]), checks.Settings())
    assert [f["entry_id"] for f in res.findings] == ["JE0007"]
