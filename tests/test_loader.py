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
