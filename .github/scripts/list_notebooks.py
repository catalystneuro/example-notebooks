"""List notebooks that should be tested by CI.

A notebook is tested when an entry's `notebook.yaml` lists it without
`test: false` (see registry.py). Optionally restricts the list to notebooks
affected by a set of changed files supplied on stdin (for the per-PR
workflow): a changed `.ipynb` selects itself, and a changed `notebook.yaml`
selects every notebook of that entry, so flipping a flag gets tested. With
`--base REF`, a `notebook.yaml` whose changes since REF are purely descriptive
(titles, descriptions, people, license, links, collections) selects nothing,
and a change to one notebook's settings selects only that notebook.

With `--tiered`, the list follows the scheduled sweep's tiers
(docs/registry-design.md, "Two kinds of status"): every week, entries with
activity in the last RECENT_DAYS days and entries a collection features;
on the first run of each month (day 1-7), everything.

Output is JSON (an array of paths), suitable for a GitHub Actions matrix.

Usage:
    python .github/scripts/list_notebooks.py [--changed-only [--base REF]]
    python .github/scripts/list_notebooks.py --tiered [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import (  # noqa: E402,F401
    ENTRY_FILE, REPO_ROOT, entry_activity, entry_for_path, load_collections, load_entries,
    notebooks_with,
)

RECENT_DAYS = 180


# Fields that describe an entry or a notebook without changing how it runs.
# A change to anything else (version, a test/colab/image flag, a new field)
# is treated as affecting execution.
DESCRIPTIVE_ENTRY_KEYS = {"title", "description", "authors", "maintainers", "license",
                          "related", "keywords", "collections"}
DESCRIPTIVE_NOTEBOOK_KEYS = {"title", "description", "test_skip_reason", "colab_skip_reason"}


def _without(data: dict, keys: set[str]) -> dict:
    return {k: v for k, v in data.items() if k not in keys}


def notebooks_changed_by_metadata(old: dict | None, new: dict) -> set[str] | None:
    """Notebook paths (relative to the entry) that an edit to notebook.yaml affects.

    None means every notebook of the entry: the entry is new or unreadable at
    the base, or a field outside the descriptive ones changed.
    """
    if not isinstance(old, dict) or not isinstance(new, dict):
        return None
    entry_keys = DESCRIPTIVE_ENTRY_KEYS | {"notebooks"}
    if _without(old, entry_keys) != _without(new, entry_keys):
        return None
    try:
        before = {n["path"]: _without(n, DESCRIPTIVE_NOTEBOOK_KEYS) for n in old.get("notebooks", [])}
        after = {n["path"]: _without(n, DESCRIPTIVE_NOTEBOOK_KEYS) for n in new.get("notebooks", [])}
    except (KeyError, TypeError):
        return None
    return {path for path, spec in after.items() if before.get(path) != spec}


def entry_file_at(ref: str, path: str) -> dict | None:
    """The parsed notebook.yaml at a git ref, or None if absent or unreadable."""
    r = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=REPO_ROOT,
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    try:
        return yaml.safe_load(r.stdout)
    except yaml.YAMLError:
        return None


def affected_notebooks(changed: list[str], base: str | None = None) -> set[str]:
    """Repo notebooks touched by `changed` paths (directly or via notebook.yaml)."""
    out = set()
    for path in changed:
        if path.endswith(".ipynb"):
            out.add(path)
        elif Path(path).name == ENTRY_FILE:
            entry = entry_for_path(path)
            if entry is None:
                continue
            selected = None
            if base:
                try:
                    current = yaml.safe_load((REPO_ROOT / path).read_text())
                except (OSError, yaml.YAMLError):
                    current = None
                selected = notebooks_changed_by_metadata(entry_file_at(base, path), current)
            if selected is None:
                out.update(spec.repo_path for spec in entry.notebooks)
            else:
                directory = Path(path).parent
                out.update((directory / rel).as_posix() for rel in selected)
    return out


def weekly_tier(today: datetime.date) -> set[str]:
    """Entry directories tested every week: recently active or featured."""
    cutoff = datetime.datetime.combine(today - datetime.timedelta(days=RECENT_DAYS),
                                       datetime.time(), tzinfo=datetime.timezone.utc)
    recent = {d for d, when in entry_activity().items() if when >= cutoff}
    featured_names = {n for c in load_collections().values() for n in c.get("featured", [])}
    featured = {e.directory for e in load_entries() if e.name in featured_names}
    return recent | featured


def tiered(notebooks: list[str], today: datetime.date) -> list[str]:
    if today.day <= 7:          # first scheduled run of the month: everything
        return notebooks
    weekly = weekly_tier(today)
    return [p for p in notebooks if (e := entry_for_path(p)) is not None and e.directory in weekly]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--changed-only",
        action="store_true",
        help="Read newline-separated changed file paths from stdin and "
             "restrict output to the testable notebooks they affect",
    )
    parser.add_argument("--base", metavar="REF",
                        help="with --changed-only: git ref to compare notebook.yaml files "
                             "against, so purely descriptive edits select no notebooks")
    parser.add_argument("--tiered", action="store_true",
                        help="apply the scheduled sweep's weekly/monthly tiers")
    parser.add_argument("--date", type=datetime.date.fromisoformat,
                        default=datetime.datetime.now(datetime.timezone.utc).date(),
                        help="date the tiers are computed for (default: today, UTC)")
    args = parser.parse_args()

    notebooks = notebooks_with("test")
    if args.tiered:
        notebooks = tiered(notebooks, args.date)
    if args.changed_only:
        changed = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
        affected = affected_notebooks(changed, args.base)
        notebooks = [p for p in notebooks if p in affected]
    print(json.dumps(notebooks))


if __name__ == "__main__":
    main()
