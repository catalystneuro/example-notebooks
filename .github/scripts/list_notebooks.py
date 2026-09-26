"""List notebooks that should be tested by CI.

A notebook is tested when an entry's `notebook.yaml` lists it without
`test: false` (see registry.py). Optionally restricts the list to notebooks
affected by a set of changed files supplied on stdin (for the per-PR
workflow): a changed `.ipynb` selects itself, and a changed `notebook.yaml`
selects every notebook of that entry, so flipping a flag gets tested.

Output is JSON (an array of paths), suitable for a GitHub Actions matrix.

Usage:
    python .github/scripts/list_notebooks.py [--changed-only]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import ENTRY_FILE, REPO_ROOT, entry_for_path, notebooks_with  # noqa: E402,F401


def affected_notebooks(changed: list[str]) -> set[str]:
    """Repo notebooks touched by `changed` paths (directly or via notebook.yaml)."""
    out = set()
    for path in changed:
        if path.endswith(".ipynb"):
            out.add(path)
        elif Path(path).name == ENTRY_FILE:
            entry = entry_for_path(path)
            if entry is not None:
                out.update(spec.repo_path for spec in entry.notebooks)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--changed-only",
        action="store_true",
        help="Read newline-separated changed file paths from stdin and "
             "restrict output to the testable notebooks they affect",
    )
    args = parser.parse_args()

    notebooks = notebooks_with("test")
    if args.changed_only:
        changed = [line.strip() for line in sys.stdin.read().splitlines() if line.strip()]
        affected = affected_notebooks(changed)
        notebooks = [p for p in notebooks if p in affected]
    print(json.dumps(notebooks))


if __name__ == "__main__":
    main()
