"""List notebooks that should be tested by CI.

A notebook is tested when an entry's `notebook.yaml` lists it without
`test: false` (see registry.py). Optionally restricts the list to notebooks
affected by a set of changed files supplied on stdin (for the per-PR
workflow): a changed `.ipynb` selects itself, and a changed `notebook.yaml`
selects every notebook of that entry, so flipping a flag gets tested.

With `--tiered`, the list follows the scheduled sweep's tiers
(docs/registry-design.md, "Two kinds of status"): every week, entries with
activity in the last RECENT_DAYS days and entries a collection features;
on the first run of each month (day 1-7), everything.

Output is JSON (an array of paths), suitable for a GitHub Actions matrix.

Usage:
    python .github/scripts/list_notebooks.py [--changed-only]
    python .github/scripts/list_notebooks.py --tiered [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import (  # noqa: E402,F401
    ENTRY_FILE, REPO_ROOT, entry_activity, entry_for_path, load_collections, load_entries,
    notebooks_with,
)

RECENT_DAYS = 180


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
        affected = affected_notebooks(changed)
        notebooks = [p for p in notebooks if p in affected]
    print(json.dumps(notebooks))


if __name__ == "__main__":
    main()
