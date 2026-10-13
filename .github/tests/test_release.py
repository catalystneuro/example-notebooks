"""Tests for the release tooling (.github/scripts/release.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import release  # noqa: E402
from registry import Entry, citation  # noqa: E402

ENTRY = Entry(directory="000055/BruntonLab/peterson21", data={
    "schema_version": 1, "name": "000055-bruntonlab-peterson21", "version": "1.0.0",
    "title": "Naturalistic arm movements", "description": "Recreates figures.",
    "authors": [{"name": "Peterson, Steven M.", "orcid": "0000-0002-1825-0097",
                 "affiliation": "University of Washington"},
                {"name": "Brunton, Bingni W."}],
    "maintainers": [{"github": "bendichter"}], "license": "Apache-2.0",
    "keywords": ["ecog"],
    "notebooks": [{"path": "a.ipynb"}],
    "related": [{"id": "dandi:000055", "relation": "uses_data"},
                {"id": "doi:10.1523/ENEURO.0007-21.2021", "relation": "reproduces"}],
})
PLAN = {"name": ENTRY.name, "directory": ENTRY.directory, "previous_version": "0.1.0",
        "version": "1.0.0", "tag": "000055-bruntonlab-peterson21/v1.0.0", "commit": "abc123",
        "swhid": "swh:1:dir:" + "0" * 40, "doi": True, "images": ["000055-bruntonlab-peterson21"],
        "problem": None}


@pytest.mark.parametrize("version, expected", [("0.9.9", False), ("1.0.0", True), ("2.3.1", True)])
def test_only_versions_from_1_0_0_get_a_doi(version, expected):
    assert release.gets_doi(version) is expected


def test_tag_name_is_namespaced_by_entry():
    assert release.tag_name("demos", "1.2.0") == "demos/v1.2.0"


def test_zenodo_metadata_comes_from_notebook_yaml():
    m = release.zenodo_metadata(ENTRY, PLAN, site_url="https://example.org/site")
    assert m["title"] == "Naturalistic arm movements"
    assert m["version"] == "1.0.0"
    assert m["license"] == "apache-2.0"
    assert m["keywords"] == ["ecog"]
    assert m["creators"] == [
        {"name": "Peterson, Steven M.", "orcid": "0000-0002-1825-0097",
         "affiliation": "University of Washington"},
        {"name": "Brunton, Bingni W."},
    ]
    related = {(r["identifier"], r["scheme"], r["relation"]) for r in m["related_identifiers"]}
    assert ("10.1523/ENEURO.0007-21.2021", "doi", "isSupplementTo") in related
    assert any(i.endswith("/dandiset/000055") and s == "url" and rel == "requires"
               for i, s, rel in related)
    assert (f"https://archive.softwareheritage.org/{PLAN['swhid']}", "url", "isIdenticalTo") in related
    assert ("https://example.org/site/", "url", "isPartOf") in related


def test_record_keeps_one_release_per_version_and_the_concept_doi():
    state = {"schema_version": 1, "entries": {}}
    release.record(state, PLAN, {"doi": "10.5072/zenodo.2", "concept_doi": "10.5072/zenodo.1",
                                 "zenodo_id": 2, "sandbox": True})
    release.record(state, PLAN, {"doi": "10.5072/zenodo.3", "concept_doi": "10.5072/zenodo.1",
                                 "zenodo_id": 3, "sandbox": True})
    entry = state["entries"][ENTRY.name]
    assert entry["concept_doi"] == "10.5072/zenodo.1"
    assert [r["doi"] for r in entry["releases"]] == ["10.5072/zenodo.3"]
    assert entry["releases"][0]["tag"] == PLAN["tag"]
    assert release.latest_release(state, ENTRY.name)["zenodo_id"] == 3


def test_a_release_without_a_doi_is_recorded_but_not_a_zenodo_parent():
    state = {"schema_version": 1, "entries": {}}
    release.record(state, {**PLAN, "version": "0.2.0", "doi": False}, {})
    assert "doi" not in state["entries"][ENTRY.name]["releases"][0]
    assert release.latest_release(state, ENTRY.name) is None


class FakeZenodo:
    """Stands in for requests.Session and records the calls made."""

    def __init__(self):
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kw):
        self.calls.append((method, url))

        class Response:
            status_code = 200
            content = b"{}"
            text = ""

            def json(inner):
                if url.endswith("/actions/newversion"):
                    return {"links": {"latest_draft": "https://z/api/deposit/depositions/8"}}
                if url.endswith("/depositions/8"):
                    return {"id": 8, "files": [{"id": "old"}], "links": {"bucket": "https://z/bucket/8"}}
                if url.endswith("/depositions"):
                    return {"id": 7, "links": {"bucket": "https://z/bucket/7"}}
                if url.endswith("/actions/publish"):
                    return {"id": 9, "doi": "10.5072/zenodo.9", "conceptdoi": "10.5072/zenodo.6",
                            "links": {"html": "https://z/records/9"}}
                return {}
        return Response()


@pytest.fixture
def fake_zenodo(monkeypatch):
    import requests
    fake = FakeZenodo()
    monkeypatch.setattr(requests, "Session", lambda: fake)
    return fake


def test_first_release_creates_a_record(fake_zenodo, tmp_path):
    archive = tmp_path / "entry.zip"
    archive.write_bytes(b"zip")
    result = release.zenodo_deposit(ENTRY, PLAN, archive, None, "token", "https://z/api")
    assert result == {"doi": "10.5072/zenodo.9", "concept_doi": "10.5072/zenodo.6",
                      "zenodo_id": 9, "zenodo_url": "https://z/records/9", "sandbox": False}
    assert [c[0] + " " + c[1].removeprefix("https://z/") for c in fake_zenodo.calls] == [
        "POST api/deposit/depositions",
        "PUT bucket/7/entry.zip",
        "PUT api/deposit/depositions/7",
        "POST api/deposit/depositions/7/actions/publish",
    ]


def test_a_later_release_adds_a_version_and_replaces_the_files(fake_zenodo, tmp_path):
    archive = tmp_path / "entry.zip"
    archive.write_bytes(b"zip")
    release.zenodo_deposit(ENTRY, PLAN, archive, {"zenodo_id": 5}, "token", "https://z/api")
    assert [c[0] + " " + c[1].removeprefix("https://z/") for c in fake_zenodo.calls] == [
        "POST api/deposit/depositions/5/actions/newversion",
        "GET api/deposit/depositions/8",
        "DELETE api/deposit/depositions/8/files/old",
        "PUT bucket/8/entry.zip",
        "PUT api/deposit/depositions/8",
        "POST api/deposit/depositions/8/actions/publish",
    ]


def test_citation_needs_authors_and_a_doi():
    released = {"releases": [{"version": "1.0.0", "date": "2026-10-13T00:00:00+00:00",
                              "doi": "10.5072/zenodo.9"}]}
    assert citation(ENTRY.data, released) == (
        "Peterson, Steven M. and Brunton, Bingni W. (2026). Naturalistic arm movements "
        "(v1.0.0). Zenodo. https://doi.org/10.5072/zenodo.9")
    assert citation(ENTRY.data, {"releases": [{"version": "0.2.0", "date": "2026-10-13"}]}) is None
    assert citation({**ENTRY.data, "authors": []}, released) is None
