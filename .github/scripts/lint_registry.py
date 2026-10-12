"""Validate registry metadata: every `notebook.yaml` and `collections/*.yaml`.

Errors (exit 1):
- a file fails its JSON Schema (.github/schemas/)
- an entry is nested inside another entry, or two entries share a name
- a listed notebook is missing, listed twice, or a repo notebook is listed nowhere
- a `related` identifier has an unknown prefix or malformed id
- an entry names a collection that does not exist, or misses the collection's
  `requires_related_prefix`
- a release version (>= 1.0.0) has no `authors`
- a CI-tested notebook's install cell disagrees with its lock file
  (`requirements.lock.txt`), or the lock file is missing

Warnings (reported, exit 0):
- notebooks larger than --max-notebook-mb (default 5)
- headless-execution hazards in CI-tested notebooks (see docs/adding-notebooks.md)
- entries with neither a README.md nor a description

With --online, also checks that maintainer and curator GitHub accounts exist
and that `dandi:` and `doi:` identifiers resolve.

Usage:
    python .github/scripts/lint_registry.py [--online] [--max-notebook-mb N]

Assumes `pyyaml`, `jsonschema`, and `nbformat` are importable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema
import nbformat
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_notebook_image import real_pins  # noqa: E402
from identifiers import split, validate  # noqa: E402
from lock_notebook import lock_path_for, read_lock, requirements_for  # noqa: E402
from registry import (  # noqa: E402
    ENTRY_FILE, REPO_ROOT, SCHEMA_DIR, Entry, entry_files, repo_notebooks,
)
from run_notebook import find_install_cell  # noqa: E402

HEADLESS_HAZARDS = [
    (re.compile(r"^\s*%matplotlib\s+(notebook|widget|ipympl|tk|qt\S*)\b", re.M),
     "interactive matplotlib backend; use `%matplotlib inline`"),
    (re.compile(r"\bcv2\.(imshow|namedWindow)\("), "cv2 GUI window"),
    (re.compile(r"(?<![\w.])input\("), "`input()` hangs without stdin"),
    (re.compile(r"\bgetpass\("), "`getpass()` hangs without stdin"),
    (re.compile(r"\bwebbrowser\.open"), "`webbrowser.open` needs a browser"),
]
PLOTLY_IMPORT = re.compile(r"^\s*(import|from)\s+plotly\b", re.M)
PLOTLY_SHOW = re.compile(r"(?<!plt)\.show\(\s*\)")
PLOTLY_RENDERER = re.compile(r"pio\.renderers\.default\s*=|plotly\.io\.renderers\.default\s*=")


@dataclass
class Report:
    errors: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[tuple[str, str]] = field(default_factory=list)

    def error(self, path: str, msg: str) -> None:
        self.errors.append((path, msg))

    def warn(self, path: str, msg: str) -> None:
        self.warnings.append((path, msg))

    def emit(self) -> None:
        annotate = os.environ.get("GITHUB_ACTIONS") == "true"
        for kind, items in (("error", self.errors), ("warning", self.warnings)):
            for path, msg in items:
                if annotate:
                    print(f"::{kind} file={path}::{msg}")
                else:
                    print(f"{kind}: {path}: {msg}")
        print(f"{len(self.errors)} error(s), {len(self.warnings)} warning(s)")


def load_schema(name: str) -> jsonschema.Validator:
    schema = json.loads((SCHEMA_DIR / name).read_text())
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema, format_checker=cls.FORMAT_CHECKER)


def schema_errors(validator: jsonschema.Validator, data) -> list[str]:
    out = []
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in err.absolute_path) or "(top level)"
        out.append(f"{where}: {err.message}")
    return out


def rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def read_yaml(path: Path, report: Report):
    try:
        return yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        report.error(rel(path), f"invalid YAML: {e}")
        return None


def version_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split("."))
    except (AttributeError, ValueError):
        return (0,)


def lint_collections(report: Report) -> dict[str, dict]:
    validator = load_schema("collection.schema.json")
    collections: dict[str, dict] = {}
    directory = REPO_ROOT / "collections"
    for path in sorted(directory.glob("*.yaml")) if directory.is_dir() else []:
        data = read_yaml(path, report)
        if data is None:
            continue
        for msg in schema_errors(validator, data):
            report.error(rel(path), msg)
        if isinstance(data, dict):
            if data.get("name") != path.stem:
                report.error(rel(path), f"name {data.get('name')!r} must match file name {path.stem!r}")
            collections[path.stem] = data
    return collections


def lint_entries(report: Report, collections: dict[str, dict]) -> list[Entry]:
    validator = load_schema("notebook.schema.json")
    entries: list[Entry] = []
    names: dict[str, str] = {}
    for path in entry_files():
        data = read_yaml(path, report)
        if data is None:
            continue
        where = rel(path)
        errors = schema_errors(validator, data)
        for msg in errors:
            report.error(where, msg)
        if not isinstance(data, dict):
            continue
        directory = str(path.parent.relative_to(REPO_ROOT))
        entry = Entry(directory="" if directory == "." else directory, data=data)
        entries.append(entry)
        if errors:
            continue  # semantic checks assume a schema-valid document

        if entry.name in names:
            report.error(where, f"name {entry.name!r} is already used by {names[entry.name]}")
        names[entry.name] = where

        paths = [nb["path"] for nb in data["notebooks"]]
        for p in sorted({p for p in paths if paths.count(p) > 1}):
            report.error(where, f"notebook {p!r} is listed more than once")
        for spec in entry.notebooks:
            if not (REPO_ROOT / spec.repo_path).is_file():
                report.error(where, f"listed notebook {spec.path!r} does not exist")

        for r in data.get("related", []):
            problem = validate(r["id"])
            if problem:
                report.error(where, f"related: {problem}")

        prefixes = {split(r["id"])[0] for r in data.get("related", [])}
        for c in data.get("collections", []):
            if c not in collections:
                report.error(where, f"unknown collection {c!r} (no collections/{c}.yaml)")
                continue
            required = collections[c].get("intake", {}).get("requires_related_prefix", [])
            if required and not prefixes & set(required):
                report.error(where, f"collection {c!r} requires a related identifier with "
                                    f"prefix {' or '.join(required)}")

        if version_tuple(data["version"]) >= (1, 0, 0) and not data.get("authors"):
            report.error(where, "releases (version >= 1.0.0) must list authors")

        if not data.get("description") and not (REPO_ROOT / entry.directory / "README.md").exists():
            report.warn(where, "no README.md and no description")

    # Nesting: an entry directory inside another entry directory.
    dirs = sorted(e.directory for e in entries)
    for d in dirs:
        for other in dirs:
            if other != d and (other == "" or d.startswith(other + "/")):
                report.error(f"{d}/{ENTRY_FILE}", f"entry is nested inside entry {other or '(root)'}")
    return entries


def lint_notebooks(report: Report, entries: list[Entry], max_mb: float) -> None:
    listed = {spec.repo_path: spec for e in entries for spec in e.notebooks}
    for path in repo_notebooks():
        if path not in listed:
            report.error(path, f"notebook is not listed in any {ENTRY_FILE}")

    lock_cache: dict[Path, list[str] | None] = {}
    for repo_path, spec in sorted(listed.items()):
        nb_path = REPO_ROOT / repo_path
        if not nb_path.is_file():
            continue
        size_mb = nb_path.stat().st_size / 1e6
        if size_mb > max_mb:
            report.warn(repo_path, f"notebook is {size_mb:.1f} MB (limit {max_mb:g} MB); "
                                   "clear large outputs before committing")
        if not (spec.test or spec.image):
            continue
        try:
            nb = nbformat.read(nb_path, as_version=4)
        except Exception as e:  # noqa: BLE001 - report any unreadable notebook
            report.error(repo_path, f"cannot read notebook: {e}")
            continue

        # Lock file agreement.
        try:
            pins, _, _ = find_install_cell(nb)
        except RuntimeError:
            report.error(repo_path, "CI-tested notebook has no Colab-bootstrap install cell; "
                                    "run lock_notebook.py, or set `test: false` with a reason")
            pins = None
        if pins is not None:
            try:
                requirements = requirements_for(nb_path)
            except FileNotFoundError as e:
                report.error(repo_path, str(e))
            else:
                lock = lock_path_for(requirements)
                if lock not in lock_cache:
                    lock_cache[lock] = read_lock(lock) if lock.exists() else None
                expected = lock_cache[lock]
                kept, _ = real_pins(pins)
                if expected is None:
                    report.error(repo_path, f"missing lock file {rel(lock)}; run "
                                            f"`python .github/scripts/lock_notebook.py {repo_path!r}`")
                elif kept != expected:
                    report.error(repo_path, f"install cell pins differ from {rel(lock)}; re-run "
                                            "lock_notebook.py on every notebook sharing "
                                            f"{requirements.name}")

        if not spec.test:
            continue
        code = "\n".join(
            c.source for c in nb.cells
            if c.cell_type == "code" and "uv pip install --system" not in c.source
        )
        for pattern, why in HEADLESS_HAZARDS:
            if pattern.search(code):
                report.warn(repo_path, f"headless hazard: {why}")
        if PLOTLY_IMPORT.search(code) and PLOTLY_SHOW.search(code) and not PLOTLY_RENDERER.search(code):
            report.warn(repo_path, "headless hazard: plotly `.show()` without setting "
                                   "`pio.renderers.default`")


def _url_ok(url: str, headers: dict | None = None) -> bool | None:
    """True/False for reachable/404; None when the check itself failed."""
    req = urllib.request.Request(url, method="HEAD", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except urllib.error.HTTPError as e:
        return False if e.code == 404 else None
    except Exception:  # noqa: BLE001 - network hiccups are not lint failures
        return None


def lint_online(report: Report, entries: list[Entry], collections: dict[str, dict]) -> None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    gh_headers = {"Authorization": f"Bearer {token}"} if token else {}
    handles: dict[str, list[str]] = {}
    for e in entries:
        for h in e.maintainers:
            handles.setdefault(h, []).append(e.metadata_path)
    for name, c in collections.items():
        for h in c.get("curators", []):
            handles.setdefault(h, []).append(f"collections/{name}.yaml")
    for handle, where in sorted(handles.items()):
        if _url_ok(f"https://api.github.com/users/{handle}", gh_headers) is False:
            report.error(where[0], f"GitHub account {handle!r} does not exist")

    for e in entries:
        for r in e.data.get("related", []):
            prefix, local = split(r["id"])
            if prefix == "dandi":
                url = f"https://api.dandiarchive.org/api/dandisets/{local.split('/')[0]}/"
            elif prefix == "doi":
                url = f"https://doi.org/api/handles/{local}"
            else:
                continue
            if _url_ok(url) is False:
                report.error(e.metadata_path, f"related identifier {r['id']!r} does not resolve")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--max-notebook-mb", type=float, default=5.0)
    args = parser.parse_args()

    report = Report()
    collections = lint_collections(report)
    entries = lint_entries(report, collections)
    lint_notebooks(report, entries, args.max_notebook_mb)
    if args.online:
        lint_online(report, entries, collections)
    report.emit()
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
