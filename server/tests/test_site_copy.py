"""Drift between the pages and the strings/anchors they reference.

Both failures this catches have already shipped once:

* Removing the workspace pass took the neighbouring TEAM section out of the
  pricing page with it. Seats stayed on sale in Waffo and behind /console/team,
  but vanished from the page people go to buy on, and /solutions kept linking to
  a #team anchor that no longer existed. Nothing failed; the section was simply
  gone.
* A key present in one catalogue and not the other renders as the raw key id
  ("pricing.plan.subscribe") to whichever language is missing it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = sorted((ROOT / "app" / "templates").glob("*.html"))
CATALOGS = {p.stem: json.loads(p.read_text())
            for p in (ROOT / "config" / "i18n").glob("*.json")}
APP_JS = (ROOT / "app" / "static" / "app.js").read_text()

# t("key") / _t("key") / T("key") — literal keys only. Keys built by
# concatenation (t("pricing.tagline." ~ tier)) cannot be checked statically.
KEY_CALL = re.compile(r'\b_?[tT]\(\s*"([a-z0-9_]+(?:\.[a-z0-9_]+)+)"')


def literal_keys() -> set[str]:
    keys = set(KEY_CALL.findall(APP_JS))
    for p in TEMPLATES:
        keys |= set(KEY_CALL.findall(p.read_text()))
    return keys


def test_catalogues_have_identical_key_sets():
    sets = {lang: set(c) for lang, c in CATALOGS.items()}
    base = next(iter(sets.values()))
    for lang, keys in sets.items():
        assert keys == base, f"{lang} differs: {sorted(keys ^ base)[:10]}"


@pytest.mark.parametrize("lang", sorted(CATALOGS))
def test_every_referenced_key_exists(lang):
    missing = sorted(k for k in literal_keys() if k not in CATALOGS[lang])
    assert not missing, f"{lang} is missing {len(missing)} keys: {missing[:10]}"


def test_placeholders_match_across_languages():
    """A {name} present in one language and not the other formats to nothing —
    "valid for  days" — rather than raising."""
    langs = sorted(CATALOGS)
    base_lang, *rest = langs
    ph = lambda s: set(re.findall(r"\{([a-z_][a-z0-9_]*)\}", s))
    for key, value in CATALOGS[base_lang].items():
        if not isinstance(value, str):
            continue
        for lang in rest:
            other = CATALOGS[lang].get(key)
            if isinstance(other, str):
                assert ph(value) == ph(other), f"{key}: {base_lang}{sorted(ph(value))} vs {lang}{sorted(ph(other))}"


def test_in_page_anchors_exist():
    """href="/pricing#team" must land on something."""
    ids = set()
    hrefs = set()
    for p in TEMPLATES:
        src = p.read_text()
        ids |= set(re.findall(r'id="([a-zA-Z0-9_-]+)"', src))
        hrefs |= {a for a in re.findall(r'href="/[a-z0-9/_-]*#([a-zA-Z0-9_-]+)"', src)}
    dangling = sorted(hrefs - ids)
    assert not dangling, f"links point at anchors no page defines: {dangling}"


def test_tax_copy_matches_what_we_tell_the_provider():
    """The page said "excludes tax, added at checkout" while every Waffo payload
    carried taxIncluded: true, so buyers were quoted a number that was already
    gross and told it was net. Whichever way this is decided, both sides move
    together."""
    import inspect
    from app.payments import waffo_provider

    src = inspect.getsource(waffo_provider)
    inclusive = '"taxIncluded": True' in src
    assert '"taxIncluded": False' not in src, "mixed tax modes across payloads"
    for lang, catalog in CATALOGS.items():
        note = catalog["pricing.tax_note"].lower()
        says_included = ("已含" in note) or ("include any applicable tax" in note)
        assert says_included == inclusive, f"{lang} tax copy disagrees with the payload"
