"""Split a Colab-snapshot refresh into one PR per entry plus one for the snapshot.

Run after `refresh_colab_snapshot.py --relock` has rewritten
`.github/colab-preinstalled.txt` and re-locked notebooks in the working tree.
The changes are split so each entry's maintainers can review and `/merge`
their own re-lock (docs/registry-design.md, "Environments"):

- `bot/refresh-colab-snapshot`: the snapshot file only (core review)
- `bot/relock/<entry name>`: that entry's notebooks and lock files only

The PRs are independent: an entry's re-lock does not need the new snapshot to
be merged first. Each branch is recreated from the default branch and
force-pushed, so an open PR is updated in place when the next refresh moves
the pins again.

Usage:
    python .github/scripts/open_relock_prs.py --base master [--dry-run]

Requires git with push access and `gh` authenticated with pull-requests:write.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_notebook_image import real_pins  # noqa: E402
from lock_notebook import CONSTRAINT, read_lock  # noqa: E402
from refresh_colab_snapshot import describe_changes, split_snapshot  # noqa: E402
from registry import REPO_ROOT, entry_for_path  # noqa: E402

SNAPSHOT = str(CONSTRAINT.relative_to(REPO_ROOT))
SNAPSHOT_BRANCH = "bot/refresh-colab-snapshot"
RELOCK_PREFIX = "bot/relock/"


def git(*args: str, check: bool = True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
                          check=check, **kw)


def changed_paths() -> list[str]:
    out = git("status", "--porcelain", "--untracked-files=all").stdout
    return sorted(line[3:].strip().strip('"') for line in out.splitlines() if line.strip())


def old_text(path: str) -> str:
    r = git("show", f"HEAD:{path}", check=False)
    return r.stdout if r.returncode == 0 else ""


def lock_changes(paths: list[str]) -> list[str]:
    """Pin changes across the lock files among `paths` (old HEAD vs working tree)."""
    lines = []
    for p in paths:
        if not p.endswith(".lock.txt"):
            continue
        old = [ln.strip() for ln in old_text(p).splitlines() if ln.strip() and not ln.startswith("#")]
        new = read_lock(REPO_ROOT / p)
        old_kept, _ = real_pins(old)
        new_kept, _ = real_pins(new)
        changes = describe_changes([x for x in old_kept if "==" in x], [x for x in new_kept if "==" in x])
        if changes:
            lines.append(f"`{p}`:")
            lines += [f"  {c}" for c in changes]
    return lines


def entry_body(name: str, directory: str, paths: list[str]) -> str:
    changes = lock_changes(paths)
    return "\n".join([
        f"Colab's preinstalled packages moved, so this re-locks entry `{name}` (`{directory}`) "
        "against the refreshed snapshot. Packages Colab does not ship keep their current pins. "
        "CI tests the entry's notebooks with the new pins.",
        "",
        "Once checks pass, a maintainer of this entry can merge it by commenting `/merge`.",
        "",
        "<details><summary>Pin changes</summary>",
        "",
        "```",
        *(changes or ["(install cells reformatted; no pin changes)"]),
        "```",
        "",
        "</details>",
    ])


def snapshot_body() -> str:
    _, old = split_snapshot(old_text(SNAPSHOT))
    _, new = split_snapshot((REPO_ROOT / SNAPSHOT).read_text())
    changes = describe_changes(old, new)
    return "\n".join([
        "Colab's preinstalled package versions have moved past "
        f"`{SNAPSHOT}`. This refreshes the snapshot from "
        "[googlecolab/backend-info](https://github.com/googlecolab/backend-info). The "
        f"notebooks' re-locks are in separate `{RELOCK_PREFIX}*` PRs, one per entry, which "
        "don't depend on this one.",
        "",
        "<details><summary>Snapshot changes</summary>",
        "",
        "```",
        *changes,
        "```",
        "",
        "</details>",
    ])


def open_pr(branch: str, base: str, title: str, body: str, paths: list[str],
            patch: str, dry_run: bool) -> None:
    if dry_run:
        print(f"[dry run] {branch}: {len(paths)} file(s) -> PR {title!r}")
        return
    git("checkout", "-B", branch, f"origin/{base}")
    git("apply", "--index", "--binary", "-", input=patch)
    git("commit", "-m", title)
    git("push", "--force", "origin", branch)
    existing = subprocess.run(
        ["gh", "pr", "list", "--head", branch, "--state", "open", "--json", "number",
         "--jq", ".[].number"], capture_output=True, text=True).stdout.strip()
    if existing:
        subprocess.run(["gh", "pr", "edit", existing, "--body", body], check=True)
        print(f"updated PR #{existing} ({branch})")
    else:
        subprocess.run(["gh", "pr", "create", "--head", branch, "--base", base,
                        "--title", title, "--body", body], check=True)
        print(f"opened PR for {branch}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True, help="default branch the PRs target")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    paths = changed_paths()
    if not paths:
        print("nothing changed")
        return 0
    # New files (e.g. a first lock file) must be visible to `git diff HEAD`.
    untracked = git("ls-files", "--others", "--exclude-standard").stdout.split("\n")
    untracked = [p for p in untracked if p in paths]
    if untracked:
        git("add", "--intent-to-add", "--", *untracked)

    groups: dict[str, list[str]] = defaultdict(list)
    entry_names: dict[str, str] = {}
    stray = []
    for p in paths:
        if p == SNAPSHOT:
            groups[SNAPSHOT_BRANCH].append(p)
            continue
        entry = entry_for_path(p)
        if entry is None:
            stray.append(p)
            continue
        branch = RELOCK_PREFIX + entry.name
        groups[branch].append(p)
        entry_names[branch] = entry.name
    if stray:
        print(f"warning: changes outside any entry are left out: {stray}", file=sys.stderr)

    # Capture every PR's patch and body before the working tree is reset.
    planned = []
    for branch, group in sorted(groups.items()):
        patch = git("diff", "--binary", "HEAD", "--", *group).stdout
        if branch == SNAPSHOT_BRANCH:
            title, body = "Refresh the Colab snapshot", snapshot_body()
        else:
            name = entry_names[branch]
            directory = entry_for_path(group[0]).directory
            title = f"Re-lock {name} for Colab's current runtime"
            body = entry_body(name, directory, group)
        planned.append((branch, title, body, group, patch))

    if not args.dry_run:
        git("reset", "--hard", "HEAD")
        if untracked:
            git("clean", "-f", "--", *untracked)
        git("fetch", "origin", args.base)
    for branch, title, body, group, patch in planned:
        open_pr(branch, args.base, title, body, group, patch, args.dry_run)
    if not args.dry_run:
        git("checkout", args.base)
    n_entries = sum(1 for b in groups if b != SNAPSHOT_BRANCH)
    print(f"{n_entries} entry PR(s)" + (" + the snapshot PR" if SNAPSHOT_BRANCH in groups else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
