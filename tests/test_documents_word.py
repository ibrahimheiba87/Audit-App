"""Search inside attached documents, and the financial statements in Word."""

import io
import re
import zipfile

import pytest

from app import db, doctext, docx, fs_word, fs_wording, textread
from tests.test_auth import add_user
from tests.test_review import team


def pdf(*pages: str) -> bytes:
    """A small PDF with a line of text on each page."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [{}] /Count {} >>".format(
                " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages))), len(pages)),
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for i, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {5 + 2 * i}"
                    " 0 R /Resources << /Font << /F1 3 0 R >> >> >>")
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode()}\nendstream")
    out, offsets = b"%PDF-1.4\n", []
    for n, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


def kinds(c, q):
    r = c.get("/api/search", params={"q": q})
    assert r.status_code == 200, r.text
    return {g["kind"]: g["items"] for g in r.json()["groups"]}


def bomb() -> bytes:
    """A .docx whose text unpacks to far more than the limit."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", b"<w:document>" + b" " * (textread.MAX_XML + 1))
    return buf.getvalue()


def test_files_are_read_by_kind():
    assert textread.extract("a.pdf", pdf("Hello", "Quokka invoice 77"))[::2] == ("ok", 2)
    status, text, _ = textread.extract("a.docx", docx.build([("text", "Wombat lease")]))
    assert status == "ok" and "Wombat lease" in text
    assert textread.extract("a.txt", "سطر عربي".encode("cp1256"))[1] == "سطر عربي"
    assert textread.extract("a.png", b"\x89PNG\r\n")[0] == "unsupported"
    assert textread.extract("a.pdf", b"%PDF-1.4 broken")[0] in ("unreadable", "empty")
    assert textread.extract("big.docx", bomb())[0] == "unreadable"
    # Arabic letters a PDF keeps in their joined shapes, or with diacritics and tatweel,
    # match the letters typed; a lone surrogate, which no database can store, is dropped.
    assert textread.normal("ﻣﺮﺣﺒﺎ مَرحـبا") == "مرحبا مرحبا"
    assert textread.normal("a\ud800b") == "a b"


def test_files_are_read_in_a_separate_process():
    assert doctext.extract_safely("a.txt", b"Bilby note") == ("ok", "Bilby note", 1)
    assert doctext.extract_safely("a.png", b"\x89PNG")[0] == "unsupported"
    assert doctext.extract_safely("big.docx", bomb())[0] == "unreadable"


@pytest.mark.usefixtures("real_auth")
def test_search_finds_words_inside_documents():
    a, prep, rev, partner, eid, _ = team()
    f = prep.post(f"/api/engagements/{eid}/findings",
                  json={"severity": "low", "detail": "Support missing"}).json()
    r = prep.post(f"/api/findings/{f['id']}/evidence",
                  files={"file": ("scan.pdf", pdf("Cover page", "Quokka Trading invoice 77"),
                                  "application/pdf")})
    assert r.status_code == 201, r.text
    req = prep.post(f"/api/engagements/{eid}/requests", json={"title": "Contracts"}).json()
    r = prep.post(f"/api/requests/{req['id']}/files", files={
        "file": ("lease.docx", docx.build([("text", "The quokka lease runs five years")]),
                 docx.MIME)})
    assert r.status_code == 201, r.text

    hits = kinds(prep, "QUOKKA")["document"]
    by = {h["source"]: h for h in hits}
    assert set(by) == {"evidence", "request_file"}
    assert by["evidence"]["page"] == 2 and by["evidence"]["finding_id"] == f["id"]
    assert "Quokka Trading invoice" in by["evidence"]["snippet"]
    assert by["request_file"]["panel"] == "rq-panel" and by["request_file"]["page"] is None

    other = add_user(a, "preparer", "doc-other@example.com")
    assert kinds(other, "quokka") == {}

    # A removed file isn't found any more.
    ev = prep.get(f"/api/findings/{f['id']}").json()["attachments"][0]
    assert rev.delete(f"/api/evidence/{ev['id']}").status_code == 204
    assert {h["source"] for h in kinds(prep, "quokka")["document"]} == {"request_file"}


@pytest.mark.usefixtures("real_auth")
def test_files_attached_earlier_are_read_once():
    a, prep, rev, partner, eid, _ = team()
    f = prep.post(f"/api/engagements/{eid}/findings",
                  json={"severity": "low", "detail": "Old file"}).json()
    prep.post(f"/api/findings/{f['id']}/evidence",
              files={"file": ("old.txt", b"Numbat receipt", "text/plain")})
    with db.SessionLocal() as s:  # as if it came before search could read files
        s.query(db.DocumentText).filter_by(source="evidence").delete()
        s.commit()
    assert "document" not in kinds(prep, "numbat")
    assert doctext.backfill() >= 1
    assert kinds(prep, "numbat")["document"][0]["title"] == "old.txt"
    assert doctext.backfill() == 0

    # A file that can't be saved doesn't stop the files after it.
    for name in ("bad.txt", "later.txt"):
        prep.post(f"/api/findings/{f['id']}/evidence",
                  files={"file": (name, f"Bandicoot {name}".encode(), "text/plain")})
    with db.SessionLocal() as s:
        s.query(db.DocumentText).filter(db.DocumentText.filename != "old.txt").delete()
        s.commit()
    real = doctext.read_one

    def flaky(source, file_id):
        with db.SessionLocal() as s:
            if s.get(db.Evidence, file_id).filename == "bad.txt":
                raise RuntimeError("boom")
        real(source, file_id)

    doctext.read_one = flaky
    try:
        doctext.backfill()
    finally:
        doctext.read_one = real
    assert [h["title"] for h in kinds(prep, "bandicoot")["document"]] == ["later.txt"]


def document_xml(content: bytes) -> str:
    return zipfile.ZipFile(io.BytesIO(content)).read("word/document.xml").decode()


def test_the_statements_download_in_word():
    from tests.test_statements import client, engagement, map_all  # noqa: PLC0415

    eid = engagement()
    map_all(eid)
    r = client.get(f"/api/engagements/{eid}/statements/word", params={"language": "en"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == docx.MIME
    xml = document_xml(r.content)
    for text in ("Statement of financial position", "Statement of profit or loss",
                 "Notes to the financial statements", "Basis of preparation", "Total assets",
                 "Draft, not audited"):
        assert text in xml, text
    assert "<w:tbl>" in xml and "3. " in xml
    # Notes are numbered in the order their lines appear: non-current assets come first.
    first = re.findall(r"<w:t[^>]*>(\d+)\. ([^<]+)</w:t>", xml)
    names = [n for _, n in first]
    numbers = [int(k) for k, _ in first]
    assert numbers == sorted(numbers) and names.index("Property, plant and equipment") < \
        names.index("Cash and cash equivalents")

    r = client.get(f"/api/engagements/{eid}/statements/word", params={"language": "ar"})
    xml = document_xml(r.content)
    assert "قائمة المركز المالي" in xml and "<w:bidiVisual/>" in xml
    assert "إجمالي الأصول" in xml and "Total assets" not in xml
    acts = {a["action"] for a in client.get("/api/activity",
                                            params={"engagement_id": eid}).json()}
    assert "statements.word" in acts

    c = client.post("/api/clients", json={"name": "No TB Ltd"}).json()
    empty = client.post(f"/api/clients/{c['id']}/engagements",
                        json={"name": "FY2026", "period_end": "2026-12-31"}).json()["id"]
    assert client.get(f"/api/engagements/{empty}/statements/word").status_code == 400


def test_figures():
    assert [fs_word.figure(x) for x in (1234.4, -1234.6, 0.2, None)] == \
        ["1,234", "(1,235)", "-", ""]


def test_new_text_has_arabic():
    from tests.test_i18n import dictionary  # noqa: PLC0415

    d = dictionary()
    patterns = [re.compile("^" + re.sub(r"\\\{(?:t:)?\w+\\\}", ".+", re.escape(p)) + "$")
                for p in d["patterns"]]

    def translated(text):
        return text in d["exact"] or any(p.match(text) for p in patterns)

    texts = list(doctext.LABELS.values()) + ["Inside documents"]
    texts += [re.findall(r'HTTPException\(\d+, "([^"]+)"', open(fs_word.__file__).read())[0]]
    assert not [t for t in texts if not translated(t)]
    assert set(fs_wording.TEXT["en"]) == set(fs_wording.TEXT["ar"])
