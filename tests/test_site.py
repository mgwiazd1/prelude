"""The static results page (docs/index.html) is built only from committed public results and must match the README."""
import json
import re

from prelude import site


def test_page_numbers_match_readme_and_page_is_clean():
    views, _ = site.load()
    site.check_against_readme(views)            # raises on any mismatch
    text = site.build(write=False)
    assert site.self_check(text) == []
    assert "default-src 'none'" in text          # CSP: no external requests possible
    assert not re.search(r"<script[^>]+src=", text)
    data = json.loads(re.search(r'<script type="application/json" id="site-data">(.*?)</script>', text, re.S).group(1)
                      .replace("<\\/", "</"))
    assert set(data) == {"age", "mcap"}          # aggregates only — no per-pair records (they carry onset days)
    assert 'data-k="age" aria-pressed="true"' in text   # age-matched is the default view


def test_readme_mismatch_refuses(monkeypatch):
    views, _ = site.load()
    views["age"] = {**views["age"], "lift": "9.9×"}
    try:
        site.check_against_readme(views)
    except SystemExit as e:
        assert "README" in str(e)
    else:
        raise AssertionError("a number that differs from the README must refuse the build")


def test_committed_page_is_current():
    with open(site.OUT, encoding="utf-8") as f:
        assert f.read() == site.build(write=False), "docs/index.html is stale: run `make site`"
