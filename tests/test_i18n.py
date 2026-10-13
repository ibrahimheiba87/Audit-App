"""The Arabic dictionary in app/static/ar.js stays loadable and its patterns line up."""

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def dictionary() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {"exact": {}, "patterns": {}}
    section = None
    for line in (STATIC / "ar.js").read_text(encoding="utf-8").splitlines():
        if m := re.match(r"\s+(exact|patterns): \{$", line):
            section = m[1]
        elif section and line.strip().startswith('"'):
            out[section].update(json.loads("{" + line.strip().rstrip(",") + "}"))
    return out


def test_dictionary_entries_are_complete():
    d = dictionary()
    assert len(d["exact"]) > 400 and len(d["patterns"]) > 150
    for en, ar in {**d["exact"], **d["patterns"]}.items():
        assert ar.strip(), en
        words = re.sub(r"\{(?:t:)?\w+\}|Excel|CSV", "", en)
        if re.search("[a-z]{3}", words):  # has words to translate, not only values or names
            assert re.search("[\u0600-\u06ff]", ar), en
        assert not re.search("[\u0660-\u0669\u06f0-\u06f9]", ar), f"Arabic-Indic digits in {en!r}"


def test_pattern_placeholders_match():
    for en, ar in dictionary()["patterns"].items():
        assert set(re.findall(r"\{(?:t:)?(\w+)\}", en)) == set(re.findall(r"\{(\w+)\}", ar)), en


def test_page_loads_the_language_switch():
    page = TestClient(app).get("/").text
    assert page.index("/static/i18n.js") < page.index("/static/ar.js") < page.index("/static/app.js")
    assert 'id="lang-toggle"' in page


def test_page_asks_for_the_current_scripts():
    res = TestClient(app).get("/")
    assert res.headers["cache-control"] == "no-cache"
    for name in ("i18n.js", "ar.js", "app.js", "app.css"):
        assert re.search(rf'"/static/{re.escape(name)}\?v=[0-9a-f]{{12}}"', res.text), name


def test_figures_always_use_western_digits():
    # Without a locale, a browser set to an Arabic region would show ٠١٢٣ in figures and dates.
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'numberingSystem: "latn"' in script
    assert not re.search(r"toLocale\w*String\((?!LOCALE)", script)
