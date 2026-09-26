"""Registry merge bot: lets entry maintainers merge changes to their own entries.

Maintainers are listed in each entry's `notebook.yaml`; they do not need write
access to the repository. A maintainer comments `/merge` on a pull request and
the bot merges it when every change is one the commenter may approve and all
checks on the PR's head commit have passed (docs/registry-design.md,
"Ownership and the merge bot").

Who may approve what, always read from the **default branch**, never from the
PR (so a PR cannot add its author as a maintainer and then merge itself):

- a change inside an existing entry: that entry's maintainers
- adding an entry to a collection, or creating a new entry: the curators of
  each collection it joins (`general` when it names none)
- anything else (tooling, workflows, collections/, top-level docs), deleting
  an entry: the core team, i.e. accounts with write access

Core-team accounts may also use `/merge`, with the same check requirement.

Subcommands:
    notify --pr N [--dry-run]
        Post (or update) a comment naming who can approve each part of the PR,
        @-mentioning them.
    merge --pr N --actor LOGIN [--comment-id ID] [--dry-run]
        Evaluate a `/merge` request from LOGIN and merge, or reply with why not.
        The comment body is read from $COMMENT_BODY; its first line must be
        exactly `/merge`.

`--dry-run` evaluates and prints without writing anything to GitHub.

Security: the workflow runs this from the default branch with a write token;
it must never check out or execute code from the PR. PR contents are read
only as data through the API (file list, `notebook.yaml` text).

Requires `gh` authenticated as the bot and `pyyaml`.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registry import ENTRY_FILE, load_collections, load_entries  # noqa: E402

REPO = os.environ.get("GITHUB_REPOSITORY", "dandi/example-notebooks")
COMMAND = "/merge"
CORE = "core team"
DEFAULT_COLLECTION = "general"
NOTIFY_MARKER = "<!-- registry-bot:approvers -->"
# Check runs of this bot's own jobs never gate a merge.
OWN_CHECKS = {"registry-bot-notify", "registry-bot-merge"}
OK_CONCLUSIONS = {"success", "neutral", "skipped"}
CORE_PERMISSIONS = {"admin", "maintain", "write"}
MAX_LISTED_PATHS = 10
MAX_MENTIONED_ROWS = 5


# ---------------------------------------------------------------------------
# Decision logic (pure; unit-tested)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str                    # added | modified | removed | renamed | copied | changed
    previous_path: str | None = None


@dataclass
class Requirement:
    """One part of the PR and the accounts that may approve it."""
    description: str
    approvers: frozenset[str]      # GitHub logins; empty with core=True means core only
    core: bool = False             # only the core team may approve

    def satisfied_by(self, actor: str, actor_is_core: bool) -> bool:
        if actor_is_core:
            return True
        return not self.core and actor.lower() in {a.lower() for a in self.approvers}


@dataclass
class Evaluation:
    requirements: list[Requirement] = field(default_factory=list)

    def refusals(self, actor: str, actor_is_core: bool) -> list[str]:
        return [r.description for r in self.requirements
                if not r.satisfied_by(actor, actor_is_core)]


def _innermost(path: str, dirs: list[str]) -> str | None:
    best = None
    for d in dirs:
        if d == "" or path == d or path.startswith(d + "/"):
            if best is None or len(d) > len(best):
                best = d
    return best


def evaluate(changed: list[ChangedFile],
             main_entries: dict[str, dict],
             collections: dict[str, dict],
             head_metadata: dict[str, dict | None]) -> Evaluation:
    """Work out who may approve each part of a PR.

    `main_entries` maps entry directory -> notebook.yaml data on the default
    branch; `head_metadata` maps entry directory -> notebook.yaml data at the
    PR head for every notebook.yaml the PR adds or modifies (None when it
    cannot be parsed).
    """
    ev = Evaluation()

    def curators(name: str) -> Requirement | None:
        c = collections.get(name)
        if c is None:
            return None
        return Requirement("", frozenset(c.get("curators", [])))

    # Entries the PR creates or deletes (by their notebook.yaml).
    new_dirs: set[str] = set()
    deleted_dirs: set[str] = set()
    for f in changed:
        for path, gone in ((f.path, f.status == "removed"),
                           (f.previous_path, f.status == "renamed")):
            if not path or PurePosixPath(path).name != ENTRY_FILE:
                continue
            d = str(PurePosixPath(path).parent)
            d = "" if d == "." else d
            if gone and d in main_entries:
                deleted_dirs.add(d)
            elif not gone and d not in main_entries:
                new_dirs.add(d)

    all_dirs = sorted(set(main_entries) | new_dirs)
    touched: dict[str, None] = {}          # ordered set of existing entries touched
    outside: list[str] = []
    for f in changed:
        for path in filter(None, (f.path, f.previous_path)):
            d = _innermost(path, all_dirs)
            if d is None:
                if path not in outside:
                    outside.append(path)
            elif d not in new_dirs and d not in deleted_dirs:
                touched[d] = None

    for d in sorted(deleted_dirs):
        name = main_entries[d].get("name", d)
        ev.requirements.append(Requirement(f"deleting entry `{name}` (`{d}`)", frozenset(), core=True))

    for d in sorted(new_dirs):
        data = head_metadata.get(d)
        if data is None:
            ev.requirements.append(Requirement(
                f"new entry at `{d}` (its notebook.yaml could not be read)", frozenset(), core=True))
            continue
        name = data.get("name", d)
        for c in data.get("collections") or [DEFAULT_COLLECTION]:
            req = curators(c)
            if req is None:
                ev.requirements.append(Requirement(
                    f"new entry `{name}` joining unknown collection `{c}`", frozenset(), core=True))
            else:
                req.description = f"new entry `{name}` joining collection `{c}`"
                ev.requirements.append(req)

    for d in touched:
        main = main_entries[d]
        name = main.get("name", d)
        maintainers = frozenset(m["github"] for m in main.get("maintainers", []) if "github" in m)
        ev.requirements.append(Requirement(f"changes to entry `{name}` (`{d}`)", maintainers))
        if d in head_metadata:
            head = head_metadata[d]
            if head is None:
                ev.requirements.append(Requirement(
                    f"entry `{name}`: notebook.yaml at the PR head could not be read",
                    frozenset(), core=True))
                continue
            added = [c for c in head.get("collections", []) if c not in main.get("collections", [])]
            for c in added:
                req = curators(c)
                if req is None:
                    ev.requirements.append(Requirement(
                        f"entry `{name}` joining unknown collection `{c}`", frozenset(), core=True))
                else:
                    req.description = f"entry `{name}` joining collection `{c}`"
                    ev.requirements.append(req)

    if outside:
        listed = ", ".join(f"`{p}`" for p in outside[:MAX_LISTED_PATHS])
        more = f" and {len(outside) - MAX_LISTED_PATHS} more" if len(outside) > MAX_LISTED_PATHS else ""
        ev.requirements.append(Requirement(
            f"files outside any entry: {listed}{more}", frozenset(), core=True))
    return ev


def check_problems(check_runs: list[dict], statuses: list[dict]) -> list[str]:
    """Why the head commit's checks do not allow a merge (empty when they do)."""
    problems = []
    for run in check_runs:
        if run.get("name") in OWN_CHECKS:
            continue
        if run.get("status") != "completed":
            problems.append(f"`{run.get('name')}` is still running")
        elif run.get("conclusion") not in OK_CONCLUSIONS:
            problems.append(f"`{run.get('name')}` concluded `{run.get('conclusion')}`")
    for st in statuses:
        if st.get("state") == "pending":
            problems.append(f"`{st.get('context')}` is pending")
        elif st.get("state") in {"failure", "error"}:
            problems.append(f"`{st.get('context')}` reported `{st.get('state')}`")
    return problems


def approvers_comment(ev: Evaluation) -> str:
    """The sticky comment listing who can approve each part of the PR.

    People are @-mentioned (and so notified) only when they can actually act:
    the PR needs no core-team row and touches at most MAX_MENTIONED_ROWS parts.
    Sweeping PRs (re-locks, tooling) list names without pinging anyone.
    """
    core_only = any(r.core or not r.approvers for r in ev.requirements)
    mention = not core_only and len(ev.requirements) <= MAX_MENTIONED_ROWS
    fmt = (lambda a: f"@{a}") if mention else (lambda a: f"`{a}`")
    rows = []
    for r in ev.requirements:
        who = CORE if r.core else (", ".join(fmt(a) for a in sorted(r.approvers, key=str.lower))
                                   or f"{CORE} (no one listed)")
        rows.append(f"| {r.description} | {who} |")
    if not rows:
        rows.append(f"| (no files changed) | {CORE} |")
    table = ["| Change | Can approve |", "|---|---|", *rows]
    if len(rows) > MAX_MENTIONED_ROWS:
        table = [f"<details><summary>{len(rows)} parts</summary>", "", *table, "", "</details>"]
    lines = [NOTIFY_MARKER, "### Who can merge this PR", "", *table, ""]
    if core_only:
        lines.append("Part of this PR needs the core team, so a core maintainer has to merge it.")
    else:
        lines.append("Once all checks pass, someone who can approve **every** row can merge by "
                     f"commenting `{COMMAND}`.")
    lines.append("Maintainers and curators are read from the default branch, so changes this "
                 "PR makes to them take effect only after it merges.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------

def gh_api(*args: str, input_json: dict | None = None) -> subprocess.CompletedProcess:
    cmd = ["gh", "api", *args]
    if input_json is not None:
        cmd += ["--input", "-"]
    return subprocess.run(cmd, capture_output=True, text=True,
                          input=json.dumps(input_json) if input_json is not None else None)


def gh_json(*args: str):
    r = gh_api(*args)
    if r.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args)} failed: {r.stderr.strip()}")
    return json.loads(r.stdout) if r.stdout.strip() else None


def gh_paginated(path: str, key: str | None = None) -> list:
    """All items of a paginated endpoint (optionally under `key` in each page)."""
    r = gh_api(path, "--paginate", "--slurp")
    if r.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {r.stderr.strip()}")
    pages = json.loads(r.stdout)
    return [item for page in pages for item in (page[key] if key else page)]


def changed_files(pr: int) -> list[ChangedFile]:
    return [ChangedFile(f["filename"], f["status"], f.get("previous_filename"))
            for f in gh_paginated(f"repos/{REPO}/pulls/{pr}/files?per_page=100")]


def head_notebook_metadata(pr_data: dict, changed: list[ChangedFile]) -> dict[str, dict | None]:
    """notebook.yaml contents at the PR head, parsed as data only."""
    head_repo = (pr_data.get("head", {}).get("repo") or {}).get("full_name") or REPO
    sha = pr_data["head"]["sha"]
    out: dict[str, dict | None] = {}
    for f in changed:
        if PurePosixPath(f.path).name != ENTRY_FILE or f.status == "removed":
            continue
        d = str(PurePosixPath(f.path).parent)
        d = "" if d == "." else d
        try:
            blob = gh_json(f"repos/{head_repo}/contents/{f.path}?ref={sha}")
            data = yaml.safe_load(base64.b64decode(blob["content"]))
            out[d] = data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001 - unreadable metadata is reported, not fatal
            out[d] = None
    return out


def main_branch_state() -> tuple[dict[str, dict], dict[str, dict]]:
    """Entries and collections from the checked-out default branch."""
    entries = {e.directory: e.data for e in load_entries()}
    return entries, load_collections()


def actor_is_core(actor: str) -> bool:
    r = gh_api(f"repos/{REPO}/collaborators/{actor}/permission", "-q", ".permission")
    return r.returncode == 0 and r.stdout.strip() in CORE_PERMISSIONS


DRY_RUN = False


def comment(pr: int, body: str) -> None:
    if DRY_RUN:
        print(f"[dry run] would comment on #{pr}:\n{body}")
        return
    gh_api("-X", "POST", f"repos/{REPO}/issues/{pr}/comments", input_json={"body": body})


def react(comment_id: str | None, content: str) -> None:
    if comment_id and not DRY_RUN:
        gh_api("-X", "POST", f"repos/{REPO}/issues/comments/{comment_id}/reactions",
               input_json={"content": content})


def upsert_comment(pr: int, body: str) -> None:
    if DRY_RUN:
        print(f"[dry run] would post/update the approvers comment on #{pr}")
        return
    r = gh_api(f"repos/{REPO}/issues/{pr}/comments", "--paginate",
               "-q", f'.[] | select(.body | contains("{NOTIFY_MARKER}")) | .id')
    ids = r.stdout.split()
    if ids:
        gh_api("-X", "PATCH", f"repos/{REPO}/issues/comments/{ids[0]}", input_json={"body": body})
    else:
        comment(pr, body)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_notify(args: argparse.Namespace) -> int:
    pr_data = gh_json(f"repos/{REPO}/pulls/{args.pr}")
    changed = changed_files(args.pr)
    entries, collections = main_branch_state()
    ev = evaluate(changed, entries, collections, head_notebook_metadata(pr_data, changed))
    upsert_comment(args.pr, approvers_comment(ev))
    print(approvers_comment(ev))
    return 0


def refuse(pr: int, comment_id: str | None, actor: str, reasons: list[str]) -> int:
    react(comment_id, "-1")
    body = [f"@{actor}, I can't merge this PR:", ""] + [f"- {r}" for r in reasons]
    comment(pr, "\n".join(body))
    print("refused:\n  " + "\n  ".join(reasons))
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    actor = args.actor
    body = os.environ.get("COMMENT_BODY", COMMAND)
    if (body.strip().splitlines() or [""])[0].strip() != COMMAND:
        print(f"comment does not start with a bare {COMMAND}; ignoring")
        return 0
    react(args.comment_id, "eyes")
    pr_data = gh_json(f"repos/{REPO}/pulls/{args.pr}")
    default_branch = pr_data["base"]["repo"]["default_branch"]
    if pr_data["state"] != "open":
        return refuse(args.pr, args.comment_id, actor, ["the PR is not open"])
    if pr_data.get("draft"):
        return refuse(args.pr, args.comment_id, actor, ["the PR is a draft"])
    if pr_data["base"]["ref"] != default_branch:
        return refuse(args.pr, args.comment_id, actor,
                      [f"the bot only merges into `{default_branch}`"])

    changed = changed_files(args.pr)
    entries, collections = main_branch_state()
    ev = evaluate(changed, entries, collections, head_notebook_metadata(pr_data, changed))
    is_core = actor_is_core(actor)
    not_allowed = ev.refusals(actor, is_core)
    if not_allowed:
        return refuse(args.pr, args.comment_id, actor,
                      [f"you can't approve {d}" for d in not_allowed])

    sha = pr_data["head"]["sha"]
    runs = gh_paginated(f"repos/{REPO}/commits/{sha}/check-runs?per_page=100", key="check_runs")
    statuses = (gh_json(f"repos/{REPO}/commits/{sha}/status") or {}).get("statuses", [])
    problems = check_problems(runs, statuses)
    if problems:
        return refuse(args.pr, args.comment_id, actor,
                      ["checks on the head commit have not all passed: " + "; ".join(problems)])
    if pr_data.get("mergeable") is False:
        return refuse(args.pr, args.comment_id, actor, ["the PR has merge conflicts"])

    if DRY_RUN:
        print(f"[dry run] would merge #{args.pr} at {sha} for @{actor}")
        return 0
    # `sha` makes GitHub reject the merge if new commits arrived after the checks.
    r = gh_api("-X", "PUT", f"repos/{REPO}/pulls/{args.pr}/merge", input_json={
        "sha": sha,
        "merge_method": "merge",
        "commit_title": f"Merge pull request #{args.pr} from {pr_data['head']['label']}",
        "commit_message": f"{pr_data['title']}\n\nMerged by @{actor} via {COMMAND}.",
    })
    if r.returncode != 0:
        detail = r.stdout.strip() or r.stderr.strip()
        return refuse(args.pr, args.comment_id, actor, [f"GitHub refused the merge: {detail}"])
    react(args.comment_id, "rocket")
    comment(args.pr, f"Merged at @{actor}'s request.")
    print("merged")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p_notify = sub.add_parser("notify")
    p_notify.add_argument("--pr", type=int, required=True)
    p_notify.set_defaults(func=cmd_notify)
    p_merge = sub.add_parser("merge")
    p_merge.add_argument("--pr", type=int, required=True)
    p_merge.add_argument("--actor", required=True)
    p_merge.add_argument("--comment-id")
    p_merge.set_defaults(func=cmd_merge)
    for p in (p_notify, p_merge):
        p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    global DRY_RUN
    DRY_RUN = args.dry_run
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
