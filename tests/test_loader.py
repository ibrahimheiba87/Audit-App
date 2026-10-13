import io

import pandas as pd
import pytest

from app import loader


def test_debit_credit_columns_become_signed_amount():
    csv = (b"JE Number,Date,Account,Debit,Credit\n"
           b"JE1,2026-01-05,Cash,100,\nJE1,2026-01-05,Revenue,,100\n")
    df = loader.load("gl.csv", csv).df
    assert list(df["amount"]) == [100.0, -100.0]
    assert list(df["row"]) == [2, 3]
    assert df["date"].dt.year.tolist() == [2026, 2026]


def test_amount_formats_are_parsed():
    csv = b'Account,Amount\nCash,"1,234.50"\nCash,(500.00)\nCash,$75\nCash,20-\n'
    df = loader.load("gl.csv", csv).df
    assert list(df["amount"]) == [1234.5, -500.0, 75.0, -20.0]


def test_semicolon_csv_is_detected():
    df = loader.load("gl.csv", b"Account;Amount\nCash;10\nRent;-10\n").df
    assert list(df["amount"]) == [10.0, -10.0]


def test_excel_upload():
    buf = io.BytesIO()
    pd.DataFrame({"GL Account": ["Cash", "Rent"], "Amount": [5.0, -5.0]}).to_excel(buf, index=False)
    df = loader.load("tb.xlsx", buf.getvalue()).df
    assert list(df["account"]) == ["Cash", "Rent"]


def test_missing_amount_column_is_rejected():
    with pytest.raises(loader.LoadError, match="No amount column"):
        loader.load("gl.csv", b"Account,Description\nCash,hello\n")


def test_unsupported_extension_is_rejected():
    with pytest.raises(loader.LoadError, match="Unsupported"):
        loader.load("gl.pdf", b"%PDF")


ODD_HEADERS = (b"Txn Dt,GL,Memo text,Value USD,Ignored\n"
               b"2026-01-05,Cash,Opening,100,x\n2026-01-06,Rent,,-100,y\n")


def test_explicit_mapping_handles_unrecognised_headers():
    with pytest.raises(loader.LoadError, match="No amount column"):
        loader.load("gl.csv", ODD_HEADERS)
    mapping = {"date": "Txn Dt", "account": "GL", "description": "Memo text",
               "amount": "Value USD"}
    loaded = loader.load("gl.csv", ODD_HEADERS, mapping)
    assert list(loaded.df["account"]) == ["Cash", "Rent"]
    assert list(loaded.df["amount"]) == [100.0, -100.0]
    assert "Ignored" not in loaded.df
    assert loaded.report.mapping == mapping


def test_empty_mapping_entries_are_ignored():
    loaded = loader.load("gl.csv", ODD_HEADERS, {"amount": "Value USD", "date": ""})
    assert "date" not in loaded.df


@pytest.mark.parametrize("mapping, message", [
    ({"amount": "Nope"}, "not in the file"),
    ({"amount": "GL", "account": "GL"}, "only be mapped once"),
    ({"amount": "Value USD", "colour": "GL"}, "Unknown field"),
    ({"account": "GL"}, "No amount column"),
])
def test_bad_mappings_are_rejected(mapping, message):
    with pytest.raises(loader.LoadError, match=message):
        loader.load("gl.csv", ODD_HEADERS, mapping)


def test_import_report_counts_skipped_rows_and_bad_dates():
    csv = (b"Date,Account,Debit,Credit\n"
           b"2026-01-05,Cash,100,\n"
           b"not a date,Rent,,100\n"
           b"2026-01-07,Cash,,\n"
           b"2026-01-08,Cash,50,20\n")
    loaded = loader.load("gl.csv", csv)
    r = loaded.report.to_dict()
    assert r["rows_read"] == 4
    assert r["rows_used"] == 3
    assert r["rows_skipped"] == 1
    assert r["total_debits"] == 130.0
    assert r["total_credits"] == 100.0
    text = " ".join(r["warnings"])
    assert "rows 4" in text  # blank amount row
    assert "transaction date" in text and "rows 3" in text
    assert "both a debit and a credit" in text


def test_preview_suggests_mapping_and_lists_sheets():
    buf = io.BytesIO()
    with pd.ExcelWriter(buf) as xl:
        pd.DataFrame({"Notes": ["x"]}).to_excel(xl, sheet_name="Cover", index=False)
        pd.DataFrame({"Posting Date": ["2026-02-01"], "Acct No": [1000],
                      "Amount": [5]}).to_excel(xl, sheet_name="GL", index=False)
    content = buf.getvalue()
    p = loader.preview("gl.xlsx", content)
    assert p["sheets"] == ["Cover", "GL"]
    assert p["columns"] == ["Notes"]
    p = loader.preview("gl.xlsx", content, sheet="GL")
    assert p["suggested"] == {"posted": "Posting Date", "account": "Acct No", "amount": "Amount"}
    assert p["rows"] == [["2026-02-01", "1000", "5"]]
    loaded = loader.load("gl.xlsx", content, sheet="GL")
    assert list(loaded.df["amount"]) == [5.0]


def test_missing_sheet_is_rejected():
    buf = io.BytesIO()
    pd.DataFrame({"Amount": [1]}).to_excel(buf, index=False)
    with pytest.raises(loader.LoadError, match="not found"):
        loader.load("gl.xlsx", buf.getvalue(), sheet="Nope")


def export_xlsx(rows):
    """A spreadsheet laid out like an accounting system's report: title rows, then a table."""
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False, header=False)
    return buf.getvalue()


def test_title_rows_above_the_header_are_skipped():
    content = export_xlsx([
        ["Nile Trading S.A.E.", None, None, None, None, None],
        ["General Ledger Detail", None, None, None, None, None],
        [None] * 6,
        ["Date", "Journal No.", "Account Code", "Account Name", "Debit (EGP)", "Credit (EGP)"],
        ["13/03/2026", "JV-1", 6500, "Professional Fees", "250,000.00", None],
        ["13/03/2026", "JV-1", 1010, "Cash at Bank", None, "250,000.00"],
        [None, None, None, "Total", "250,000.00", "250,000.00"],
    ])
    preview = loader.preview("gl.xlsx", content)
    assert preview["columns"][:3] == ["Date", "Journal No.", "Account Code"]
    assert preview["suggested"]["debit"] == "Debit (EGP)"
    loaded = loader.load("gl.xlsx", content)
    df = loaded.df
    assert list(df["row"]) == [5, 6]  # numbered as in the spreadsheet
    assert list(df["account"]) == ["6500", "1010"] and list(df["entry_id"]) == ["JV-1", "JV-1"]
    assert df["date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-03-13", "2026-03-13"]
    report = loaded.report.to_dict()
    assert report["rows_skipped"] == 1 and report["total_debits"] == 250_000
    assert report["warnings"] == ["1 rows left out because they look like totals: they have an "
                                  "amount but no account, entry or date (rows 7)."]


def test_title_rows_in_a_csv_are_skipped():
    csv = (b"Trial Balance\nAs at 31 December 2026\n\n"
           b"Account Code,Account Name,Debit,Credit\n1010,Cash,100,\n4010,Sales,,100\n,Total,100,100\n")
    loaded = loader.load("tb.csv", csv)
    assert list(loaded.df["account"]) == ["1010", "4010"]
    assert list(loaded.df["row"]) == [5, 6]
    assert loaded.report.total_rows == [7]


def test_closer_header_matches_win():
    mapping = loader.suggest_mapping(["Account Name", "Narration", "Account Code", "Amount"])
    assert mapping == {"account": "Account Code", "description": "Narration", "amount": "Amount"}
    # With nothing closer, an account name is still the account.
    assert loader.suggest_mapping(["Account Name", "Amount"])["account"] == "Account Name"


@pytest.mark.parametrize("dates,expected", [
    (["03/04/2026", "13/04/2026"], ["2026-04-03", "2026-04-13"]),  # day first
    (["03/04/2026", "04/13/2026"], ["2026-03-04", "2026-04-13"]),  # month first
    (["03/04/2026", "05/06/2026"], ["2026-04-03", "2026-06-05"]),  # can't tell: day first
    (["2026-03-04", "2026-04-13"], ["2026-03-04", "2026-04-13"]),  # ISO
    (["2026-03-04", "13/04/2026"], ["2026-03-04", "2026-04-13"]),  # ISO stays ISO
    (["2026-03-04 10:00:00", "13/04/2026"], ["2026-03-04", "2026-04-13"]),
])
def test_slash_dates_are_read_the_way_the_file_writes_them(dates, expected):
    csv = "Date,Account,Amount\n" + "".join(f"{d},Cash,{i + 1}\n" for i, d in enumerate(dates))
    df = loader.load("gl.csv", csv.encode()).df
    assert df["date"].dt.strftime("%Y-%m-%d").tolist() == expected


def test_rows_without_an_account_are_kept_unless_they_say_total():
    csv = b"Account,Description,Amount\n1000,Cash,5\n,Misc,1\n,Subtotal,6\n,,6\n"
    loaded = loader.load("tb.csv", csv)
    assert list(loaded.df["amount"]) == [5.0, 1.0]
    assert loaded.report.total_rows == [4, 5]


def test_iso_dates_keep_their_order_when_another_column_is_day_first():
    csv = b"Date,Posted Date,Account,Amount\n2026-03-04,13/03/2026 09:00,1000,1\n"
    df = loader.load("gl.csv", csv).df
    assert df["date"][0] == pd.Timestamp("2026-03-04")
    assert df["posted"][0] == pd.Timestamp("2026-03-13 09:00")


def test_rows_with_an_unreadable_date_are_not_totals():
    csv = b"Date,Description,Amount\n01/03/2026,Sale,5\n31/02/2026,,7\nTBD,,9\n"
    loaded = loader.load("gl.csv", csv)
    assert list(loaded.df["amount"]) == [5.0, 7.0, 9.0]
    assert loaded.report.total_rows == [] and loaded.report.bad_dates["transaction date"] == [3, 4]


def test_a_description_starting_with_total_is_not_a_total():
    csv = b"Account,Description,Amount\n1000,Cash,5\n,TotalEnergies fuel accrual,-5\n,Grand Total,0\n"
    loaded = loader.load("tb.csv", csv)
    assert list(loaded.df["amount"]) == [5.0, -5.0] and loaded.report.total_rows == [4]


def test_bracketed_dr_and_cr_mean_debit_and_credit():
    assert loader.suggest_mapping(["Account", "Amount (Dr)", "Amount (Cr)"]) == {
        "account": "Account", "debit": "Amount (Dr)", "credit": "Amount (Cr)"}
    assert loader.suggest_mapping(["Account", "Balance (Dr)", "Balance (Cr)"])["credit"] == \
        "Balance (Cr)"


def test_a_data_row_does_not_replace_a_header_with_unusual_names():
    content = export_xlsx([
        ["GL Code", "Particulars", "Source", "Dr/Cr", "Amount"],
        [1000, "Cash", "Journal", "Dr", 5],
        [2000, "Sales", "Journal", "Cr", -5],
    ])
    mapping = {"account": "GL Code", "description": "Particulars", "amount": "Amount"}
    df = loader.load("gl.xlsx", content, mapping).df
    assert list(df["account"]) == ["1000", "2000"] and list(df["row"]) == [2, 3]


def test_mappings_saved_before_title_rows_were_skipped_still_load():
    content = export_xlsx([
        ["Nile Trading GL", None, None],
        ["Date", "Account", "Amount"],
        ["01/03/2026", 1000, 5],
    ])
    loaded = loader.load("gl.xlsx", content, {"account": "Unnamed: 1", "amount": "Unnamed: 2"})
    assert list(loaded.df["account"]) == ["1000"]


def test_sample_exports_import_cleanly():
    from pathlib import Path
    data = Path(__file__).resolve().parent.parent / "sample_data"
    gl = loader.load("gl.xlsx", (data / "nile_trading_gl_2026.xlsx").read_bytes())
    assert gl.report.mapping["description"] == "Narration" and gl.report.total_rows
    assert gl.df["date"].notna().all() and gl.df["posted"].notna().all()
    assert round(gl.report.total_debits - gl.report.total_credits, 2) == 200  # planted
    for year in (2025, 2026):
        tb = loader.load("tb.xlsx", (data / f"nile_trading_tb_{year}.xlsx").read_bytes())
        assert round(tb.df["amount"].sum(), 2) == 0 and len(tb.df) == 18


def test_a_one_cell_title_does_not_count_as_two_columns():
    content = export_xlsx([["Account Name", None], ["Account", "Amount"], [1000, 5]])
    df = loader.load("tb.xlsx", content).df
    assert list(df["account"]) == ["1000"] and list(df["row"]) == [3]


def test_csv_row_numbers_count_blank_lines():
    csv = b"\nDate,Account,Amount\n01/03/2026,1000,5\n\n02/03/2026,,x\n"
    loaded = loader.load("gl.csv", csv)
    assert list(loaded.df["row"]) == [3] and loaded.report.skipped_rows == [5]
