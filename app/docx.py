"""A plain Word document (.docx) from a list of paragraphs, without extra packages.

Enough for letters and reports: a title, headings, body text and lines in bold, left to right
or right to left (Arabic). A .docx is a zip of a few XML parts; this writes the smallest set
Word, LibreOffice and Google Docs all open.
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.sax.saxutils import escape

MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
# Characters XML 1.0 doesn't allow; a pasted note could carry them.
INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/'
    'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '</Types>')
RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="word/document.xml"/>'
    '</Relationships>')

# style: (bold, size in half points, alignment, space after in twentieths of a point)
STYLES = {
    "title": (True, 32, "center", 240),
    "heading": (True, 24, None, 120),
    "subheading": (True, 22, None, 80),
    "text": (False, 22, "both", 160),
    "line": (False, 22, None, 0),  # signature blocks and addresses: no space between lines
    "bold": (True, 22, None, 160),
}


def paragraph(style: str, text: str, rtl: bool) -> str:
    bold, size, align, after = STYLES[style]
    # Child elements in the order the Office Open XML schema sets.
    ppr = ("<w:bidi/>" if rtl else "") \
        + f'<w:spacing w:after="{after}" w:line="276" w:lineRule="auto"/>' \
        + (f'<w:jc w:val="{align}"/>' if align else "")
    rpr = ('<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Arial"/>'
           + ("<w:b/><w:bCs/>" if bold else "")
           + f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>' + ("<w:rtl/>" if rtl else "")
           + ('<w:lang w:bidi="ar-SA"/>' if rtl else ""))
    lines = INVALID.sub("", text).split("\n")
    body = "<w:br/>".join(f'<w:t xml:space="preserve">{escape(x)}</w:t>' for x in lines)
    return f"<w:p><w:pPr>{ppr}</w:pPr><w:r><w:rPr>{rpr}</w:rPr>{body}</w:r></w:p>"


def _run(text: str, bold: bool, rtl: bool, size: int = 20) -> str:
    rpr = ('<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Arial"/>'
           + ("<w:b/><w:bCs/>" if bold else "")
           + f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>' + ("<w:rtl/>" if rtl else ""))
    return f'<w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{escape(INVALID.sub("", text))}</w:t></w:r>'


# Table rows: (kind, cells). Bold kinds, and the border above or below a row's figures.
ROW_BOLD = {"head", "heading", "subtotal", "total"}
ROW_RULE = {"subtotal": '<w:top w:val="single" w:sz="4" w:space="0" w:color="000000"/>',
            "total": '<w:top w:val="single" w:sz="4" w:space="0" w:color="000000"/>'
                     '<w:bottom w:val="double" w:sz="4" w:space="0" w:color="000000"/>'}


def table(spec: dict, rtl: bool) -> str:
    """A table: spec has "widths" (twentieths of a point, the first column is text and the
    rest are figures, written left to right and lined up at the end) and "rows"."""
    widths = spec["widths"]
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
    rows = []
    for kind, cells in spec["rows"]:
        bold = kind in ROW_BOLD
        tcs = []
        for i, (w, text) in enumerate(zip(widths, cells, strict=True)):
            figure = i > 0
            borders = f"<w:tcBorders>{ROW_RULE[kind]}</w:tcBorders>" \
                if figure and kind in ROW_RULE and text else ""
            ppr = ("<w:bidi/>" if rtl and not figure else "") \
                + '<w:spacing w:before="20" w:after="20"/>' \
                + ('<w:jc w:val="right"/>' if figure else "")
            tcs.append(f'<w:tc><w:tcPr><w:tcW w:w="{w}" w:type="dxa"/>{borders}</w:tcPr>'
                       f'<w:p><w:pPr>{ppr}</w:pPr>{_run(text, bold, rtl and not figure)}</w:p>'
                       '</w:tc>')
        rows.append(f"<w:tr>{''.join(tcs)}</w:tr>")
    tblpr = ("<w:bidiVisual/>" if rtl else "") + \
        f'<w:tblW w:w="{sum(widths)}" w:type="dxa"/><w:tblLayout w:type="fixed"/>'
    # An empty paragraph after it, so two tables in a row stay apart.
    return (f"<w:tbl><w:tblPr>{tblpr}</w:tblPr><w:tblGrid>{grid}</w:tblGrid>{''.join(rows)}"
            '</w:tbl><w:p><w:pPr><w:spacing w:after="120"/></w:pPr></w:p>')


def page_break() -> str:
    return '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'


def build(paragraphs: list[tuple[str, str | dict]], rtl: bool = False) -> bytes:
    """The .docx for (style, text) pairs; styles are the keys of STYLES, or "table" with a
    table spec, or "page" (a page break, text ignored)."""
    body = "".join(table(text, rtl) if style == "table" else page_break() if style == "page"
                   else paragraph(style, text, rtl) for style, text in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W}"><w:body>{body}'
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1300" w:bottom="1440" w:left="1300" w:header="708" '
        'w:footer="708" w:gutter="0"/>' + ("<w:bidi/>" if rtl else "") +
        '</w:sectPr></w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"],
    # Gregorian month names, as the rest of the app shows them in Arabic.
    "ar": ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر",
           "أكتوبر", "نوفمبر", "ديسمبر"],
}


def long_date(d, language: str) -> str:
    """31 December 2026, or its Arabic form with Western digits."""
    return f"{d.day} {MONTHS[language][d.month - 1]} {d.year}"
