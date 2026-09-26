"""Load registry entries (`notebook.yaml`) and collections (`collections/*.yaml`).

An entry is any directory containing a `notebook.yaml`. It lists the entry's
notebooks with per-notebook CI flags (`test`, `colab`, `image`), its
maintainers, and the papers and datasets it relates to. The schema lives in
`.github/schemas/notebook.schema.json`; `docs/registry-design.md` explains the
model.

The CI scripts ask this module which notebooks to test, which get a Colab
button, and which get a container image. A notebook that is not listed in any
entry gets none of these (the lint step reports it).

Subcommands:
    index [--output PATH]
        Write `registry.json`, the machine-readable catalog of all entries.
    list --flag {test,colab,image}
        Print the repo-relative notebook paths with that flag set, as JSON.

Assumes `pyyaml` is importable.
"""

from __future__ import annotations

import argparse
import datetime
import functools
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from identifiers import identifier_url  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
ENTRY_FILE = "notebook.yaml"
COLLECTIONS_DIR = REPO_ROOT / "collections"
SCHEMA_DIR = REPO_ROOT / ".github" / "schemas"

GITHUB_REPO = "dandi/example-notebooks"
DEFAULT_BRANCH = "master"

# Directories never searched for entries or notebooks.
SKIP_DIRS = {".git", "output", "node_modules", ".ipynb_checkpoints", "nwb-cache"}


@dataclass(frozen=True)
class NotebookSpec:
    entry_dir: str   # repo-relative entry directory, "" for the repo root
    path: str        # relative to the entry directory
    title: str | None
    test: bool
    colab: bool
    image: bool
    test_skip_reason: str | None
    colab_skip_reason: str | None
    runtime_minutes: int | None

    @property
    def repo_path(self) -> str:
        return f"{self.entry_dir}/{self.path}" if self.entry_dir else self.path


@dataclass(frozen=True)
class Entry:
    directory: str   # repo-relative
    data: dict

    @property
    def name(self) -> str:
        return self.data.get("name", "")

    @property
    def metadata_path(self) -> str:
        return f"{self.directory}/{ENTRY_FILE}" if self.directory else ENTRY_FILE

    @property
    def maintainers(self) -> list[str]:
        return [m["github"] for m in self.data.get("maintainers", []) if "github" in m]

    @property
    def notebooks(self) -> list[NotebookSpec]:
        specs = []
        for nb in self.data.get("notebooks", []):
            test = nb.get("test", True)
            specs.append(NotebookSpec(
                entry_dir=self.directory,
                path=nb["path"],
                title=nb.get("title"),
                test=test,
                colab=nb.get("colab", True),
                image=nb.get("image", test),
                test_skip_reason=nb.get("test_skip_reason"),
                colab_skip_reason=nb.get("colab_skip_reason"),
                runtime_minutes=nb.get("runtime_minutes"),
            ))
        return specs


def _walk(root: Path, filename: str | None = None, suffix: str | None = None):
    """Yield files under root, skipping SKIP_DIRS, in sorted order."""
    for path in sorted(root.iterdir()):
        if path.is_dir():
            if path.name in SKIP_DIRS:
                continue
            yield from _walk(path, filename, suffix)
        elif (filename and path.name == filename) or (suffix and path.name.endswith(suffix)):
            yield path


def entry_files(root: Path = REPO_ROOT) -> list[Path]:
    return list(_walk(root, filename=ENTRY_FILE))


def repo_notebooks(root: Path = REPO_ROOT) -> list[str]:
    """Every `.ipynb` in the repo, repo-relative."""
    return [str(p.relative_to(root)) for p in _walk(root, suffix=".ipynb")]


def load_yaml(path: Path) -> dict:
    with path.open() as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


@functools.lru_cache(maxsize=None)
def load_entries(root: Path = REPO_ROOT) -> tuple[Entry, ...]:
    entries = []
    for path in entry_files(root):
        directory = str(path.parent.relative_to(root))
        entries.append(Entry(directory="" if directory == "." else directory,
                             data=load_yaml(path)))
    return tuple(entries)


@functools.lru_cache(maxsize=None)
def notebook_specs(root: Path = REPO_ROOT) -> dict[str, NotebookSpec]:
    """Map repo-relative notebook path -> its spec, for every listed notebook."""
    return {
        spec.repo_path: spec
        for entry in load_entries(root)
        for spec in entry.notebooks
    }


def entry_for_path(repo_path: str, root: Path = REPO_ROOT) -> Entry | None:
    """The innermost entry whose directory contains `repo_path`."""
    best = None
    for entry in load_entries(root):
        d = entry.directory
        if d == "" or repo_path == d or repo_path.startswith(d + "/"):
            if best is None or len(d) > len(best.directory):
                best = entry
    return best


def notebooks_with(flag: str, root: Path = REPO_ROOT) -> list[str]:
    """Sorted repo-relative paths of listed notebooks with `flag` set."""
    return sorted(
        path for path, spec in notebook_specs(root).items()
        if getattr(spec, flag) and (root / path).exists()
    )


def flag(repo_path: str, name: str, root: Path = REPO_ROOT) -> bool:
    """A notebook's flag; False for notebooks not listed in any entry."""
    spec = notebook_specs(root).get(repo_path)
    return bool(spec and getattr(spec, name))


def load_collections(root: Path = REPO_ROOT) -> dict[str, dict]:
    directory = root / "collections"
    if not directory.is_dir():
        return {}
    return {p.stem: load_yaml(p) for p in sorted(directory.glob("*.yaml"))}


def github_url(repo_path: str) -> str:
    return f"https://github.com/{GITHUB_REPO}/blob/{DEFAULT_BRANCH}/{quote(repo_path)}"


def colab_url(repo_path: str) -> str:
    return (f"https://colab.research.google.com/github/{GITHUB_REPO}/blob/"
            f"{DEFAULT_BRANCH}/{quote(repo_path)}")


def registry_index(root: Path = REPO_ROOT,
                   has_colab_bootstrap=None,
                   docker_images: dict[str, str] | None = None) -> dict:
    """The `registry.json` catalog.

    `has_colab_bootstrap(repo_path) -> bool` gates the Colab link on the
    notebook actually carrying the install cell; `docker_images` maps
    repo-relative notebook paths to public image refs. Both are optional so
    the catalog can be built offline.
    """
    docker_images = docker_images or {}
    collections = load_collections(root)
    entries = []
    for entry in load_entries(root):
        d = entry.data
        notebooks = []
        for spec in entry.notebooks:
            path = spec.repo_path
            colab = spec.colab and (has_colab_bootstrap is None or has_colab_bootstrap(path))
            image = docker_images.get(path)
            notebooks.append({
                "path": path,
                "title": spec.title,
                "test": spec.test,
                "github_url": github_url(path),
                "colab_url": colab_url(path) if colab else None,
                "docker_image": image,
            })
        entries.append({
            "name": entry.name,
            "version": d.get("version"),
            "title": d.get("title"),
            "description": d.get("description"),
            "directory": entry.directory,
            "metadata_url": github_url(entry.metadata_path),
            "authors": d.get("authors", []),
            "maintainers": entry.maintainers,
            "license": d.get("license"),
            "keywords": d.get("keywords", []),
            "collections": d.get("collections", []),
            "related": [
                {**r, "url": identifier_url(r["id"])} for r in d.get("related", [])
            ],
            "notebooks": notebooks,
        })
    return {
        "schema_version": 1,
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": f"https://github.com/{GITHUB_REPO}",
        "collections": {
            name: {k: c.get(k) for k in ("title", "description", "homepage", "curators")}
            for name, c in collections.items()
        },
        "entries": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p_index = sub.add_parser("index")
    p_index.add_argument("--output", type=Path, default=REPO_ROOT / "output" / "registry.json")
    p_list = sub.add_parser("list")
    p_list.add_argument("--flag", choices=["test", "colab", "image"], required=True)
    args = parser.parse_args()

    if args.command == "index":
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(registry_index(), indent=2) + "\n")
        print(f"wrote {args.output}")
    else:
        print(json.dumps(notebooks_with(args.flag)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
