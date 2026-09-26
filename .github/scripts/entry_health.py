"""Track each entry's health across scheduled sweeps and file per-entry issues.

The scheduled sweep (test-all-notebooks-weekly.yml) runs the notebooks and
leaves one `*.result.json` per notebook. This script folds those results into
`health.json`, the running record of every entry's current status (published
on gh-pages and read into registry.json), and keeps one GitHub issue per
failing entry (docs/registry-design.md, "Two kinds of status"):

- passing       every notebook tested in the entry's latest run passed
- failing       failed FAILING_AFTER or more runs in a row
- unmaintained  failing, first failed more than UNMAINTAINED_DAYS ago, and
                no commit to the entry since then

An entry's first failure opens an issue that @-mentions its maintainers.
Later failures add a comment (without mentions), and the first all-green run
closes it. Entries not tested in a run keep their previous state.

Usage:
    python .github/scripts/entry_health.py --results DIR --state health.json
        [--output health.json] [--run-url URL] [--no-issues] [--dry-run]

Requires `pyyaml`, and `gh` authenticated with issues:write unless --no-issues.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from merge_bot import gh_api, gh_paginated  # noqa: E402
from registry import entry_activity, load_entries  # noqa: E402

REPO = os.environ.get("GITHUB_REPOSITORY", "dandi/example-notebooks")
FAILING_AFTER = 2
UNMAINTAINED_DAYS = 56
LABEL = "notebook-failure"
LABEL_COLOR = "d73a4a"
MARKER = "<!-- registry-entry:{name} -->"


def _iso(t: datetime.datetime) -> str:
    return t.astimezone(datetime.timezone.utc).isoformat(timespec="seconds")


def last_error_line(result: dict) -> str:
    lines = (result.get("error") or "").strip().splitlines()
    return next((line.strip() for line in reversed(lines) if line.strip()), "")[:300]


@dataclass
class Outcome:
    """What happened to one entry in this run."""
    name: str
    directory: str
    maintainers: list[str]
    failed: list[dict]              # failing notebook results
    passed: list[dict]
    status: str
    consecutive_failures: int


def update_state(state: dict, results: list[dict], entries: list, activity: dict,
                 now: datetime.datetime, run_url: str | None) -> tuple[dict, list[Outcome]]:
    """Fold one run's results into `state`. Pure apart from its arguments."""
    innermost_first = sorted(entries, key=lambda e: -len(e.directory))
    by_entry: dict[str, list[dict]] = {}
    dir_of = {}
    for r in results:
        entry = next((e for e in innermost_first
                      if e.directory == "" or r["notebook"].startswith(e.directory + "/")), None)
        if entry is None:
            continue
        by_entry.setdefault(entry.name, []).append(r)
        dir_of[entry.name] = entry

    known = {e.name for e in entries}
    old = {k: v for k, v in state.get("entries", {}).items() if k in known}
    new_entries = dict(old)
    outcomes = []
    for name, rs in sorted(by_entry.items()):
        entry = dir_of[name]
        prev = old.get(name, {})
        failed = [r for r in rs if not r.get("ok")]
        passed = [r for r in rs if r.get("ok")]
        rec = {
            "last_run": _iso(now),
            "last_run_url": run_url,
            "notebooks": {
                r["notebook"]: {
                    "ok": bool(r.get("ok")),
                    "stage": r.get("stage"),
                    "duration_s": r.get("duration_s"),
                    **({} if r.get("ok") else {"error": last_error_line(r)}),
                } for r in rs
            },
        }
        if failed:
            consecutive = prev.get("consecutive_failures", 0) + 1
            first_failure = prev.get("first_failure") or _iso(now)
            status = "failing" if consecutive >= FAILING_AFTER else prev.get("status", "passing")
            if status == "failing":
                since = datetime.datetime.fromisoformat(first_failure)
                touched = activity.get(entry.directory)
                quiet = touched is None or touched < since
                if quiet and now - since > datetime.timedelta(days=UNMAINTAINED_DAYS):
                    status = "unmaintained"
            rec.update(status=status, consecutive_failures=consecutive,
                       first_failure=first_failure, last_pass=prev.get("last_pass"))
        else:
            rec.update(status="passing", consecutive_failures=0, first_failure=None,
                       last_pass=_iso(now))
        new_entries[name] = rec
        outcomes.append(Outcome(name, entry.directory, entry.maintainers, failed, passed,
                                rec["status"], rec["consecutive_failures"]))
    return {"schema_version": 1, "updated": _iso(now), "entries": new_entries}, outcomes


# ---------------------------------------------------------------------------
# Issues
# ---------------------------------------------------------------------------

def failure_lines(o: Outcome) -> list[str]:
    return [f"- `{r['notebook']}` failed at stage `{r.get('stage')}`: `{last_error_line(r)}`"
            for r in o.failed]


def new_issue_body(o: Outcome, run_url: str | None) -> str:
    mentions = " ".join(f"@{m}" for m in o.maintainers) or "(no maintainers listed)"
    return "\n".join([
        MARKER.format(name=o.name),
        f"The scheduled notebook sweep found failures in entry `{o.name}` (`{o.directory}`).",
        "",
        *failure_lines(o),
        "",
        f"Run: {run_url}" if run_url else "",
        "",
        f"Maintainers: {mentions}",
        "",
        "Fix the notebook (or its pins in `requirements.in`) in a PR and merge it with "
        "`/merge`. If it can't run in CI, set `test: false` with a `test_skip_reason` in "
        "the entry's `notebook.yaml`. This issue closes by itself once the entry passes "
        "a scheduled run. See docs/adding-notebooks.md.",
    ])


def followup_body(o: Outcome, run_url: str | None) -> str:
    return "\n".join([
        f"Still failing (run {o.consecutive_failures} in a row; status `{o.status}`).",
        "",
        *failure_lines(o),
        *(["", f"Run: {run_url}"] if run_url else []),
    ])


def open_issues() -> dict[str, int]:
    """Entry name -> number of its open failure issue."""
    issues = gh_paginated(f"repos/{REPO}/issues?state=open&labels={LABEL}&per_page=100")
    out = {}
    for issue in issues:
        body = issue.get("body") or ""
        start = body.find("<!-- registry-entry:")
        if start >= 0:
            name = body[start + len("<!-- registry-entry:"):body.find(" -->", start)]
            out.setdefault(name, issue["number"])
    return out


def sync_issues(outcomes: list[Outcome], run_url: str | None, dry_run: bool) -> None:
    def call(*args, **kw):
        if dry_run:
            print(f"[dry run] gh api {' '.join(args)} {json.dumps(kw.get('input_json', ''))[:200]}")
            return None
        r = gh_api(*args, **kw)
        if r.returncode != 0:
            print(f"warning: gh api {' '.join(args)} failed: {r.stderr.strip()}", file=sys.stderr)
        return r

    call("-X", "POST", f"repos/{REPO}/labels",
         input_json={"name": LABEL, "color": LABEL_COLOR,
                     "description": "A registry entry failed the scheduled notebook sweep"})
    existing = open_issues()
    for o in outcomes:
        number = existing.get(o.name)
        if o.failed and number is None:
            call("-X", "POST", f"repos/{REPO}/issues", input_json={
                "title": f"Notebooks failing in entry {o.name}",
                "body": new_issue_body(o, run_url),
                "labels": [LABEL],
            })
        elif o.failed:
            call("-X", "POST", f"repos/{REPO}/issues/{number}/comments",
                 input_json={"body": followup_body(o, run_url)})
        elif number is not None:
            call("-X", "POST", f"repos/{REPO}/issues/{number}/comments", input_json={
                "body": "All notebooks in this entry pass again"
                        + (f" ({run_url})" if run_url else "") + ". Closing."})
            call("-X", "PATCH", f"repos/{REPO}/issues/{number}", input_json={"state": "closed"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", type=Path, required=True,
                        help="directory holding the sweep's *.result.json files")
    parser.add_argument("--state", type=Path, required=True,
                        help="previous health.json (missing file = no history)")
    parser.add_argument("--output", type=Path, help="where to write health.json (default: --state)")
    parser.add_argument("--run-url")
    parser.add_argument("--no-issues", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="print issue changes, don't make them")
    args = parser.parse_args()

    state = json.loads(args.state.read_text()) if args.state.exists() else {}
    results = [json.loads(p.read_text()) for p in sorted(args.results.glob("*.result.json"))]
    now = datetime.datetime.now(datetime.timezone.utc)
    new_state, outcomes = update_state(state, results, list(load_entries()), entry_activity(),
                                       now, args.run_url)
    out = args.output or args.state
    out.write_text(json.dumps(new_state, indent=2) + "\n")
    for o in outcomes:
        print(f"{o.status:12} {o.name}: {len(o.passed)} passed, {len(o.failed)} failed")
    if not args.no_issues:
        sync_issues(outcomes, args.run_url, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
