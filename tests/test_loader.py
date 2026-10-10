import io

import pandas as pd
import pytest

from app import loader


def test_debit_credit_columns_become_signed_amount():
    csv = (b"JE Number,Date,Account,Debit,Credit\n"
           b"JE1,2026-01-05,Cash,100,\nJE1,2026-01-05,Revenue,,100\n")
    df = loader.load("gl.csv", csv)
    assert list(df["amount"]) == [100.0, -100.0]
    assert list(df["row"]) == [2, 3]
    assert df["date"].dt.year.tolist() == [2026, 2026]


def test_amount_formats_are_parsed():
    csv = b'Account,Amount\nCash,"1,234.50"\nCash,(500.00)\nCash,$75\nCash,20-\n'
    df = loader.load("gl.csv", csv)
    assert list(df["amount"]) == [1234.5, -500.0, 75.0, -20.0]


def test_semicolon_csv_is_detected():
    df = loader.load("gl.csv", b"Account;Amount\nCash;10\nRent;-10\n")
    assert list(df["amount"]) == [10.0, -10.0]


def test_excel_upload():
    buf = io.BytesIO()
    pd.DataFrame({"GL Account": ["Cash", "Rent"], "Amount": [5.0, -5.0]}).to_excel(buf, index=False)
    df = loader.load("tb.xlsx", buf.getvalue())
    assert list(df["account"]) == ["Cash", "Rent"]


def test_missing_amount_column_is_rejected():
    with pytest.raises(loader.LoadError, match="No amount column"):
        loader.load("gl.csv", b"Account,Description\nCash,hello\n")


def test_unsupported_extension_is_rejected():
    with pytest.raises(loader.LoadError, match="Unsupported"):
        loader.load("gl.pdf", b"%PDF")
