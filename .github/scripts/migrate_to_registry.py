"""One-off migration: write a `notebook.yaml` for every existing notebook directory.

Derives each entry's metadata from what the repo already records:

- entry directory: the notebook's directory (or its parent, when the notebooks
  sit in a `notebooks/` subdirectory next to the README)
- flags: `test`/`colab`/`image` from the three `.github/*.txt` lists, with the
  comment above each pattern as the skip reason
- maintainers: the entry's main contributors in the commit history (see
  `maintainers_from_history`), falling back to `--default-maintainer`
- related: `dandi:<id>` from the path and from dandiset links in the README,
  DOIs found in the README
- title/description: the README's first heading and paragraph, else the first
  notebook's first heading
- lock files: the install cell's pins, written as `requirements.lock.txt`

Everything it writes is a starting point for a human to review. Existing
`notebook.yaml` files are left alone unless `--force` is given.

Usage:
    python .github/scripts/migrate_to_registry.py [--dry-run] [--force]
        [--no-github] [--default-maintainer HANDLE]
    python .github/scripts/migrate_to_registry.py --update-maintainers [--dry-run]
        Recompute only the `maintainers` of existing notebook.yaml files.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

import nbformat
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_notebook_image import real_pins  # noqa: E402
from lock_notebook import lock_path_for, requirements_for, write_lock  # noqa: E402
from registry import ENTRY_FILE, REPO_ROOT, entry_files, repo_notebooks  # noqa: E402
from run_notebook import find_install_cell  # noqa: E402

LISTS = {
    "test": REPO_ROOT / ".github" / "notebook-test-exclusions.txt",
    "colab": REPO_ROOT / ".github" / "notebook-colab-exclusions.txt",
    "image": REPO_ROOT / ".github" / "notebook-image-inclusions.txt",
}
REPO_LICENSE = "Apache-2.0"
DANDI_RRID = "rrid:SCR_017571"   # DANDI Archive
GITHUB_REPO = "dandi/example-notebooks"
BOT_SUFFIXES = ("[bot]",)
NOT_PEOPLE = {"dandibot", "yarikoptic-gitmate", "nbgitpuller"}
# A commit touching this many entries is repo-wide maintenance (re-locks,
# codespell runs, reorganizations), not authorship of any one of them.
SWEEP_ENTRIES = 3
# Contributors below this share of an entry's added lines are not maintainers
# (typo fixes, a re-lock), except the person who first added the entry.
MIN_SHARE = 0.2
MAX_MAINTAINERS = 4
NOREPLY_RE = re.compile(r"^(?:\d+\+)?([A-Za-z0-9-]+)@users\.noreply\.github\.com$")

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>()\[\]{}]+[^\s\"'<>()\[\]{}.,;:])")
BIORXIV_VERSION_RE = re.compile(r"^(10\.1101/.+?)v\d+$")
DANDISET_RE = re.compile(r"dandiarchive\.org/dandiset/(\d{6})")
HEADING_RE = re.compile(r"^\s{0,3}(#{1,3})\s+(.+?)\s*#*\s*$", re.M)
# Headings that say nothing about what a notebook or entry is.
GENERIC_HEADINGS = {
    "installing requirements", "introduction", "overview", "reference", "references",
    "setup", "set up", "imports", "set parameters", "define utility functions",
    "background", "part i", "part ii", "contents", "table of contents",
}


# ---------------------------------------------------------------------------
# Exclusion lists
# ---------------------------------------------------------------------------

def parse_list(path: Path) -> list[tuple[str, str]]:
    """(pattern, reason) pairs; the reason is the comment block above the pattern.

    Section banners (text between two `# ====` lines) supply the reason for
    patterns that have no comment of their own.
    """
    if not path.exists():
        return []
    out: list[tuple[str, str]] = []
    reason: list[str] = []
    section: list[str] = []
    in_banner = False
    after_pattern = False
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line.startswith("# ===="):
            in_banner = not in_banner
            if in_banner:
                section = []
            reason, after_pattern = [], False
            continue
        if line.startswith("#"):
            text = line.lstrip("#").strip()
            if in_banner:
                section.append(text)
            else:
                if after_pattern:
                    reason, after_pattern = [], False
                reason.append(text)
            continue
        if not line:
            if after_pattern:
                reason, after_pattern = [], False
            continue
        text = " ".join(t for t in (reason or section) if t)
        out.append((line, text.rstrip(".") + "." if text else ""))
        after_pattern = True
    return out


def match(path: str, pattern: str) -> bool:
    if fnmatch.fnmatch(path, pattern):
        return True
    return pattern.endswith("/**") and (path == pattern[:-3] or path.startswith(pattern[:-2]))


def lookup(path: str, rules: list[tuple[str, str]]) -> str | None:
    """The reason for the first rule matching `path`, or None when none match."""
    for pattern, reason in rules:
        if match(path, pattern):
            return reason or "Listed in the legacy exclusion file."
    return None


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def strip_markdown(text: str) -> str:
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # images
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)       # links
    text = re.sub(r"[*_`]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def readme_title_and_description(readme: Path) -> tuple[str | None, str | None]:
    if not readme.exists():
        return None, None
    text = readme.read_text(errors="replace")
    m = HEADING_RE.search(text)
    title = clean_heading(m.group(2)) if m else None
    description = None
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block or block.startswith(("#", "[![", "![", "```", "|", "<", "-", "*", ">")):
            continue
        description = strip_markdown(block)
        if description.endswith(":"):
            # Lead-in to a list ("... the following papers:"); not a summary.
            description = None
            break
        if len(description) > 600:
            description = description[:597].rsplit(" ", 1)[0] + "..."
        break
    return title or None, description or None


def clean_heading(text: str) -> str:
    return strip_markdown(text.lstrip("#").strip()).rstrip(".")


def notebook_title(nb) -> str | None:
    """The top-level heading of the first markdown cell that has a real heading."""
    for cell in nb.cells:
        if cell.cell_type != "markdown":
            continue
        headings = sorted(
            (len(m.group(1)), i, clean_heading(m.group(2)))
            for i, m in enumerate(HEADING_RE.finditer(cell.source))
        )
        for _, _, title in headings:
            if title and title.lower() not in GENERIC_HEADINGS:
                return title
    return None


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ---------------------------------------------------------------------------
# GitHub attribution
# ---------------------------------------------------------------------------

def _gh(*args: str) -> str:
    r = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def _person(login: str | None) -> str | None:
    if not login or login.endswith(BOT_SUFFIXES) or login in NOT_PEOPLE:
        return None
    return login


_pr_author_cache: dict[str, str | None] = {}


def pr_author(sha: str) -> str | None:
    """Author of the merged PR that brought in `sha`."""
    if sha not in _pr_author_cache:
        _pr_author_cache[sha] = _person(_gh(
            f"repos/{GITHUB_REPO}/commits/{sha}/pulls",
            "--jq", "[.[] | select(.merged_at != null)][0].user.login // empty"))
    return _pr_author_cache[sha]


_email_login_cache: dict[str, str | None] = {}


def commit_login(sha: str, email: str) -> str | None:
    """GitHub login for a commit: its author's account, else the PR author.

    Accounts are resolved once per email. Commits whose email maps to no
    account (e.g. nbgitpuller commits made on the JupyterHub) are credited to
    the author of the PR that merged them.
    """
    m = NOREPLY_RE.match(email)
    if m:
        return _person(m.group(1))
    if email not in _email_login_cache:
        _email_login_cache[email] = _gh(
            f"repos/{GITHUB_REPO}/commits/{sha}", "--jq", ".author.login // empty") or None
    login = _person(_email_login_cache[email])
    return login or pr_author(sha)


# Files the registry tooling generates; editing them is not authorship.
GENERATED = (ENTRY_FILE, ".lock.txt")


def commit_entry_counts() -> dict[str, int]:
    """sha -> number of current entries whose existing files the commit modified.

    Used to recognize repo-wide sweeps. Only modifications count: a commit
    that adds several new entries at once is authorship, not a sweep.
    """
    out = subprocess.run(
        ["git", "log", "--no-merges", "--diff-filter=M", "--name-only", "--format=@@%H"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    entry_dirs = sorted(
        ("" if str(p.parent.relative_to(REPO_ROOT)) == "." else str(p.parent.relative_to(REPO_ROOT))
         for p in entry_files()),
        key=len, reverse=True,
    )
    counts: dict[str, set[str]] = defaultdict(set)
    sha = None
    for line in out.splitlines():
        if line.startswith("@@"):
            sha = line[2:]
        elif line.strip() and sha:
            for d in entry_dirs:
                if d and line.startswith(d + "/"):
                    counts[sha].add(d)
                    break
    return {k: len(v) for k, v in counts.items()}


def file_history(repo_path: str) -> list[tuple[str, str, int, bool]]:
    """(sha, email, lines added, created) for every commit in a file's history.

    Follows renames and copies (`--follow`), so a notebook moved by a
    reorganization is still credited to the commits that wrote it.
    """
    out = subprocess.run(
        ["git", "log", "--follow", "--no-merges", "--numstat", "--summary",
         "--format=@@%H %ae", "--", repo_path],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    rows: list[list] = []
    for line in out.splitlines():
        if line.startswith("@@"):
            sha, email = line[2:].split(" ", 1)
            rows.append([sha, email, 0, False])
        elif not rows or not line.strip():
            continue
        elif line.startswith(" create mode"):
            rows[-1][3] = True
        elif "\t" in line:
            added = line.split("\t", 1)[0]
            rows[-1][2] += int(added) if added.isdigit() else 0
    return [tuple(r) for r in rows]


def maintainers_from_history(entry_dirs: list[str]) -> dict[str, list[tuple[str, float]]]:
    """Per entry directory: [(login, share of added lines)], strongest first.

    Lines added to the entry's files are summed per GitHub account across the
    files' full history (following moves). Commits that modify files in
    SWEEP_ENTRIES or more entries (re-locks, codespell runs) are skipped, as
    are the registry's generated files. A contributor qualifies with at least
    MIN_SHARE of the lines; whoever created each notebook always qualifies.
    """
    sweeps = {sha for sha, n in commit_entry_counts().items() if n >= SWEEP_ENTRIES}
    tracked = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, capture_output=True,
                             text=True, check=True).stdout.splitlines()
    by_length = sorted(entry_dirs, key=len, reverse=True)

    def entry_of(path: str) -> str | None:
        for d in by_length:
            if d == "" or path.startswith(d + "/"):
                return d
        return None

    lines: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    creators: dict[str, list[str]] = defaultdict(list)
    for path in tracked:
        d = entry_of(path)
        if d is None or path.endswith(GENERATED):
            continue
        for sha, email, added, created in file_history(path):
            if sha in sweeps and not created:
                continue
            login = commit_login(sha, email)
            if login is None:
                continue
            lines[d][login] += added
            if created and path.endswith(".ipynb") and login not in creators[d]:
                creators[d].append(login)

    result = {}
    for d in entry_dirs:
        total = sum(lines[d].values())
        share = {u: (n / total if total else 0.0) for u, n in lines[d].items()}
        chosen = [u for u, _ in sorted(share.items(), key=lambda kv: -kv[1])
                  if share[u] >= MIN_SHARE]
        chosen += [u for u in creators[d] if u not in chosen]
        result[d] = [(u, share.get(u, 0.0)) for u in chosen][:MAX_MAINTAINERS]
    return result


# ---------------------------------------------------------------------------
# Entry construction
# ---------------------------------------------------------------------------

def entry_dir_for(repo_path: str) -> str:
    parent = Path(repo_path).parent
    if parent.name == "notebooks" and parent.parent != Path("."):
        parent = parent.parent
    return "" if str(parent) == "." else str(parent)


def doi_resolves(doi: str) -> bool | None:
    """True/False when doi.org answers; None when the lookup itself fails."""
    try:
        req = urllib.request.Request(f"https://doi.org/api/handles/{doi}", method="HEAD")
        with urllib.request.urlopen(req, timeout=15):
            return True
    except urllib.error.HTTPError as e:
        return False if e.code == 404 else None
    except Exception:  # noqa: BLE001
        return None


def related_for(directory: str, readme: Path, check_dois: bool) -> list[dict]:
    related: list[dict] = []
    seen: set[str] = set()

    def add(identifier: str, relation: str) -> None:
        if identifier.lower() not in seen:
            seen.add(identifier.lower())
            related.append({"id": identifier, "relation": relation})

    top = directory.split("/", 1)[0]
    if re.fullmatch(r"\d{6}", top):
        add(f"dandi:{top}", "uses_data")
    else:
        # Archive-level tutorials and tooling demos: about DANDI itself.
        add(DANDI_RRID, "tutorial_for")
    if readme.exists():
        text = readme.read_text(errors="replace")
        for ds in DANDISET_RE.findall(text):
            add(f"dandi:{ds}", "uses_data")
        for doi in DOI_RE.findall(text):
            if doi.startswith("10.48324/dandi."):   # DANDI's own DOIs; covered by dandi:
                continue
            doi = BIORXIV_VERSION_RE.sub(r"\1", doi)   # bioRxiv URLs carry a version suffix
            if check_dois and doi_resolves(doi) is False:
                print(f"note  {directory}: dropping unresolvable DOI {doi}")
                continue
            add(f"doi:{doi}", "references")
    return related


def keywords_for(directory: str) -> list[str]:
    parts = directory.split("/")
    if parts[0] == "tutorials":
        return ["tutorial"] + ([slug(parts[1])] if len(parts) > 1 else [])
    if parts[0] == "dandi":
        return ["dandi-archive"]
    return []


def build_entry(directory: str, notebooks: list[str], rules: dict, use_github: bool,
                maintainers: list[str]) -> dict:
    entry_path = REPO_ROOT / directory
    readme = entry_path / "README.md"
    title, description = readme_title_and_description(readme)

    nb_specs = []
    for repo_path in notebooks:
        nb = nbformat.read(REPO_ROOT / repo_path, as_version=4)
        rel = str(Path(repo_path).relative_to(directory)) if directory else repo_path
        spec: dict = {"path": rel}
        nb_title = notebook_title(nb)
        if nb_title:
            spec["title"] = nb_title
        test_reason = lookup(repo_path, rules["test"])
        colab_reason = lookup(repo_path, rules["colab"])
        image_forced = lookup(repo_path, rules["image"]) is not None
        if test_reason is not None:
            spec["test"] = False
            spec["test_skip_reason"] = test_reason
        if colab_reason is not None:
            spec["colab"] = False
            spec["colab_skip_reason"] = colab_reason
        if image_forced and test_reason is not None:
            spec["image"] = True
        nb_specs.append(spec)
        if title is None:
            title = nb_title

    if title is None:
        title = Path(directory).name.replace("_", " ") if directory else "Notebooks"

    data: dict = {
        "schema_version": 1,
        "name": slug(directory) or "root",
        "version": "0.1.0",
        "title": title or directory,
    }
    if description:
        data["description"] = description
    data["maintainers"] = [{"github": m} for m in maintainers]
    data["license"] = REPO_LICENSE
    data["notebooks"] = nb_specs
    related = related_for(directory, readme, check_dois=use_github)
    if related:
        data["related"] = related
    keywords = keywords_for(directory)
    if keywords:
        data["keywords"] = keywords
    data["collections"] = ["dandi"]
    return data


HEADER = (
    "# Registry entry metadata. Schema: .github/schemas/notebook.schema.json\n"
    "# Generated by .github/scripts/migrate_to_registry.py from the repo's history\n"
    "# and the legacy exclusion lists; review before cutting a 1.0.0 release.\n"
)


def write_locks(notebooks: list[str], dry_run: bool) -> list[str]:
    """Write lock files from the install cells of `notebooks`. Returns conflicts.

    Only CI-tested (or image-built) notebooks are passed in: those are the ones
    re-locked by the Colab-snapshot refresh, so the lock tracks them. Skipped
    notebooks may legitimately sit on an older pin set.
    """
    by_lock: dict[Path, dict[tuple[str, ...], list[str]]] = defaultdict(lambda: defaultdict(list))
    requirements_of: dict[Path, Path] = {}
    for repo_path in notebooks:
        nb_path = REPO_ROOT / repo_path
        try:
            pins, _, _ = find_install_cell(nbformat.read(nb_path, as_version=4))
            requirements = requirements_for(nb_path)
        except (RuntimeError, FileNotFoundError):
            continue
        kept, _ = real_pins(pins)
        lock = lock_path_for(requirements)
        requirements_of[lock] = requirements
        by_lock[lock][tuple(kept)].append(repo_path)
    conflicts = []
    for lock, variants in sorted(by_lock.items()):
        if len(variants) > 1:
            detail = "; ".join(f"{len(v)} notebook(s) e.g. {v[0]}" for v in variants.values())
            conflicts.append(f"{lock.relative_to(REPO_ROOT)}: notebooks disagree ({detail})")
            continue
        (pins,) = variants
        if not dry_run:
            write_lock(requirements_of[lock], list(pins))
        print(f"lock  {lock.relative_to(REPO_ROOT)} ({len(pins)} pins)")
    return conflicts


def update_maintainers(dry_run: bool, default_maintainer: str) -> int:
    """Rewrite only the `maintainers` field of every existing notebook.yaml."""
    targets = entry_files()
    dirs = [str(p.parent.relative_to(REPO_ROOT)) for p in targets]
    dirs = ["" if d == "." else d for d in dirs]
    history = maintainers_from_history(dirs)
    for path, d in zip(targets, dirs):
        ranked = history.get(d, [])
        logins = [u for u, _ in ranked] or [default_maintainer]
        text = path.read_text()
        data = yaml.safe_load(text)
        old = [m["github"] for m in data.get("maintainers", [])]
        shares = ", ".join(f"{u} {share:.0%}" for u, share in ranked) or f"(none; {default_maintainer})"
        print(f"{'same' if old == logins else 'set '}  {d}: {shares}"
              + ("" if old == logins else f"   [was: {', '.join(old)}]"))
        if old == logins or dry_run:
            continue
        # Replace the block in place so comments and layout elsewhere survive.
        block = "maintainers:\n" + "".join(f"- github: {u}\n" for u in logins)
        new, n = re.subn(r"^maintainers:\n(?:[ -].*\n)+", block, text, count=1, flags=re.M)
        if n != 1:
            print(f"error: could not locate maintainers block in {path}", file=sys.stderr)
            return 1
        path.write_text(new)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="overwrite existing notebook.yaml")
    parser.add_argument("--no-github", action="store_true",
                        help="skip `gh api` maintainer lookup")
    parser.add_argument("--default-maintainer", default="bendichter")
    parser.add_argument("--update-maintainers", action="store_true",
                        help="only recompute `maintainers` in existing notebook.yaml files")
    args = parser.parse_args()

    if args.update_maintainers:
        return update_maintainers(args.dry_run, args.default_maintainer)

    rules = {k: parse_list(p) for k, p in LISTS.items()}
    notebooks = repo_notebooks()
    groups: dict[str, list[str]] = defaultdict(list)
    for repo_path in notebooks:
        groups[entry_dir_for(repo_path)].append(repo_path)
    history = maintainers_from_history(list(groups)) if not args.no_github else {}

    for directory, nbs in sorted(groups.items()):
        target = REPO_ROOT / directory / ENTRY_FILE
        if target.exists() and not args.force:
            print(f"skip  {target.relative_to(REPO_ROOT)} (exists)")
            continue
        maintainers = [u for u, _ in history.get(directory, [])] or [args.default_maintainer]
        data = build_entry(directory, sorted(nbs), rules, not args.no_github, maintainers)
        text = HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=88)
        if args.dry_run:
            print(f"--- {target.relative_to(REPO_ROOT)}\n{text}")
        else:
            target.write_text(text)
            print(f"wrote {target.relative_to(REPO_ROOT)} "
                  f"({len(nbs)} notebook(s), maintainers: "
                  f"{', '.join(m['github'] for m in data['maintainers'])})")

    locked = [
        p for p in notebooks
        if lookup(p, rules["test"]) is None or lookup(p, rules["image"]) is not None
    ]
    conflicts = write_locks(locked, args.dry_run)
    for c in conflicts:
        print(f"warning: {c}", file=sys.stderr)
    print(json.dumps({"entries": len(groups), "lock_conflicts": len(conflicts)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
