"""Reading the words in a file: PDFs, Word (.docx) and Excel (.xlsx) files and plain text.

Imports nothing from the app, so doctext.py runs it as a separate process with limits on its
time and memory: `python -I textread.py NAME` with the file on stdin prints its text as JSON.
"""

from __future__ import annotations

import io
import json
import logging
import re
import sys
import unicodedata
import zipfile
from xml.etree import ElementTree

log = logging.getLogger(__name__)

MAX_CHARS = 500_000  # kept of one file
MAX_PAGES = 500
MAX_CELLS = 200_000
MAX_XML = 50 * 1024 * 1024  # unpacked size of the part of a .docx or .xlsx that is read
MAX_RATIO = 100  # how much a part may grow when unpacked
TEXT_TYPES = (".txt", ".csv", ".tsv", ".md", ".json", ".xml", ".html", ".htm")
WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
SPACES = re.compile(r"[^\S\f]+")
# Control characters PostgreSQL won't store and lone surrogates no database can encode
# (spaces), and the Arabic diacritics and tatweel that would stop a plain word matching
# (dropped).
CONTROL = re.compile("[\x00-\x08\x0b\x0e-\x1f\ud800-\udfff]")
MARKS = re.compile("[\u0640\u064b-\u065f\u0670]")


def normal(value: str) -> str:
    """Text as stored and searched: compatibility form, single spaces, pages split by \\f."""
    value = unicodedata.normalize("NFKC", value[:MAX_CHARS * 2])
    value = MARKS.sub("", CONTROL.sub(" ", value))
    return SPACES.sub(" ", value).strip()[:MAX_CHARS]


def _pdf(content: bytes) -> tuple[str, int]:
    from pypdf import PdfReader  # noqa: PLC0415 - only needed for PDFs

    reader = PdfReader(io.BytesIO(content))
    if reader.is_encrypted and not reader.decrypt(""):
        raise ValueError("encrypted")
    pages = []
    for page in reader.pages[:MAX_PAGES]:
        try:
            pages.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - one bad page shouldn't lose the rest
            pages.append("")
        if sum(map(len, pages)) > MAX_CHARS:
            break
    return "\f".join(pages), len(reader.pages)


def _part(z: zipfile.ZipFile, name: str) -> bytes:
    """One part of a .docx or .xlsx, refused when it would unpack to too much."""
    info = z.getinfo(name)
    if info.file_size > MAX_XML or info.file_size > MAX_RATIO * max(info.compress_size, 1024):
        raise ValueError(f"{name} is too large unpacked")
    return z.read(name)


def _docx(z: zipfile.ZipFile) -> str:
    root = ElementTree.fromstring(_part(z, "word/document.xml"))
    return "\n".join("".join(t.text or "" for t in p.iter(f"{WORD}t"))
                     for p in root.iter(f"{WORD}p"))


def _xlsx(z: zipfile.ZipFile, content: bytes) -> str:
    from openpyxl import load_workbook  # noqa: PLC0415

    for name in z.namelist():
        if name.startswith("xl/") and name.endswith(".xml"):
            _part(z, name)
    book = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    out, cells = [], 0
    try:
        for sheet in book.worksheets:
            for row in sheet.iter_rows(values_only=True):
                values = [str(v) for v in row if v is not None and str(v).strip()]
                cells += len(row)
                if values:
                    out.append(" ".join(values))
                if cells > MAX_CELLS:
                    return "\n".join(out)
    finally:
        book.close()
    return "\n".join(out)


def _plain(content: bytes) -> str:
    content = content[:MAX_CHARS * 4]
    for encoding in ("utf-8-sig", "cp1256"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1")


def extract(filename: str, content: bytes) -> tuple[str, str, int]:
    """(status, text, pages) for a file, read in this process; status is one of STATUSES."""
    name = filename.lower()
    try:
        if content.startswith(b"%PDF"):
            value, pages = _pdf(content)
        elif content.startswith(b"PK"):
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                parts = set(z.namelist())
                if "word/document.xml" in parts:
                    value, pages = _docx(z), 1
                elif "xl/workbook.xml" in parts:
                    value, pages = _xlsx(z, content), 1
                else:
                    return "unsupported", "", 0
        elif name.endswith(TEXT_TYPES):
            value, pages = _plain(content), 1
        else:
            return "unsupported", "", 0
    except Exception as exc:  # noqa: BLE001 - a damaged file is noted, not an error
        log.info("Couldn't read %s: %s", filename, exc)
        return "unreadable", "", 0
    value = normal(value)
    return ("ok" if value.replace("\f", "").strip() else "empty"), value, pages



if __name__ == "__main__":
    status, value, pages = extract(sys.argv[1], sys.stdin.buffer.read())
    sys.stdout.write(json.dumps({"status": status, "text": value, "pages": pages}))
