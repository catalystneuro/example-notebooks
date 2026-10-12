"""Tests for the registry tooling (.github/scripts/{registry,identifiers,lint_registry}.py)."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import identifiers  # noqa: E402
import lint_registry  # noqa: E402
import registry  # noqa: E402
from migrate_to_registry import entry_dir_for, parse_list  # noqa: E402


# --- identifiers -------------------------------------------------------------

@pytest.mark.parametrize("identifier", [
    "dandi:000055",
    "dandi:000055/0.220127.0436",
    "doi:10.1523/ENEURO.0007-21.2021",
    "openneuro:ds000246",
    "rrid:SCR_017571",
    "arxiv:2106.01234v2",
    "github:dandi/dandi-cli",
])
def test_valid_identifiers(identifier):
    assert identifiers.validate(identifier) is None
    assert identifiers.identifier_url(identifier).startswith("https://")


@pytest.mark.parametrize("identifier, problem", [
    ("dandi:55", "not a valid dandi"),
    ("doi:not-a-doi", "not a valid doi"),
    ("bogus:123", "unknown identifier prefix"),
])
def test_invalid_identifiers(identifier, problem):
    assert problem in identifiers.validate(identifier)
    assert identifiers.identifier_url(identifier) is None


def test_dandi_url():
    assert (identifiers.identifier_url("dandi:000055")
            == "https://dandiarchive.org/dandiset/000055")


# --- registry loading --------------------------------------------------------

def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))


@pytest.fixture
def repo(tmp_path):
    write(tmp_path, "000001/Lab/notebook.yaml", """\
        schema_version: 1
        name: 000001-lab
        version: 0.1.0
        title: Demo
        maintainers: [{github: someone}]
        license: MIT
        notebooks:
          - path: a.ipynb
          - path: b.ipynb
            test: false
            test_skip_reason: needs a database
          - path: c.ipynb
            test: false
            test_skip_reason: needs libxcb
            image: true
            colab: false
            colab_skip_reason: broken upstream
        related:
          - {id: "dandi:000001", relation: uses_data}
        collections: [dandi]
        """)
    for nb in ("a", "b", "c", "orphan"):
        write(tmp_path, f"000001/Lab/{nb}.ipynb", "{}")
    registry.load_entries.cache_clear()
    registry.notebook_specs.cache_clear()
    yield tmp_path
    registry.load_entries.cache_clear()
    registry.notebook_specs.cache_clear()


def test_flags(repo):
    assert registry.notebooks_with("test", repo) == ["000001/Lab/a.ipynb"]
    assert registry.notebooks_with("image", repo) == ["000001/Lab/a.ipynb", "000001/Lab/c.ipynb"]
    assert registry.notebooks_with("colab", repo) == ["000001/Lab/a.ipynb", "000001/Lab/b.ipynb"]
    # Unlisted notebooks get no flags.
    assert not registry.flag("000001/Lab/orphan.ipynb", "test", repo)


def test_entry_for_path(repo):
    entry = registry.entry_for_path("000001/Lab/sub/helper.py", repo)
    assert entry is not None and entry.name == "000001-lab"
    assert registry.entry_for_path("000002/x.ipynb", repo) is None


def test_registry_index(repo):
    index = registry.registry_index(repo, has_colab_bootstrap=lambda p: p.endswith("a.ipynb"))
    (entry,) = index["entries"]
    assert entry["related"][0]["url"] == "https://dandiarchive.org/dandiset/000001"
    colab = {nb["path"]: nb["colab_url"] for nb in entry["notebooks"]}
    assert colab["000001/Lab/a.ipynb"].startswith("https://colab.research.google.com/")
    assert colab["000001/Lab/b.ipynb"] is None   # flag set, but no bootstrap cell
    assert colab["000001/Lab/c.ipynb"] is None   # colab: false


# --- schema ------------------------------------------------------------------

def test_schema_requires_skip_reason():
    validator = lint_registry.load_schema("notebook.schema.json")
    doc = {
        "schema_version": 1, "name": "x", "version": "0.1.0", "title": "X",
        "maintainers": [{"github": "someone"}], "license": "MIT",
        "notebooks": [{"path": "a.ipynb", "test": False}],
    }
    errors = lint_registry.schema_errors(validator, doc)
    assert any("test_skip_reason" in e for e in errors)
    doc["notebooks"][0]["test_skip_reason"] = "needs a database"
    assert lint_registry.schema_errors(validator, doc) == []


# --- migration helpers -------------------------------------------------------

def test_parse_list_reasons(tmp_path):
    path = tmp_path / "list.txt"
    path.write_text(textwrap.dedent("""\
        # Preamble, ignored.

        # =====
        # Section banner reason.
        # =====

        **/DataJoint/**

        # Specific reason
        # over two lines.
        a/one.ipynb
        a/two.ipynb

        b/three.ipynb
        """))
    assert parse_list(path) == [
        ("**/DataJoint/**", "Section banner reason."),
        ("a/one.ipynb", "Specific reason over two lines."),
        ("a/two.ipynb", "Specific reason over two lines."),
        ("b/three.ipynb", "Section banner reason."),
    ]


def test_entry_dir_for():
    assert entry_dir_for("000055/BruntonLab/peterson21/x.ipynb") == "000055/BruntonLab/peterson21"
    assert entry_dir_for("000005/DataJoint/DJ/notebooks/x.ipynb") == "000005/DataJoint/DJ"


# --- the real repo -----------------------------------------------------------

def test_repo_lints_clean():
    registry.load_entries.cache_clear()
    registry.notebook_specs.cache_clear()
    report = lint_registry.Report()
    collections = lint_registry.lint_collections(report)
    entries = lint_registry.lint_entries(report, collections)
    lint_registry.lint_notebooks(report, entries, max_mb=5.0)
    assert report.errors == []
