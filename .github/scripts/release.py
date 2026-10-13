"""Cut per-entry releases (docs/registry-design.md, "Releases and DOIs").

A release is cut when a merge changes an entry's `version` in notebook.yaml.
For each such entry this script:

1. tags the commit `<name>/v<version>`
2. records the Software Heritage directory ID of the entry at that commit
   (a `swh:1:dir:` ID is the git tree hash, so it needs no network), and asks
   Software Heritage to archive the repository unless quiet mode is on
3. for versions >= 1.0.0, deposits the entry on Zenodo and publishes it,
   as a new version of the entry's existing record when it has one
4. writes the result to the release state file (`releases.json`, kept on
   gh-pages next to health.json), which registry.json reads

Tagging the entry's container images is done by the workflow, which has
docker; `plan` lists the image groups to tag.

A new entry is not released when it is first added: the first release is the
first time its version changes. Versions below 1.0.0 are tagged but get no
DOI.

Usage:
    python .github/scripts/release.py plan --base REF [--entry NAME]
    python .github/scripts/release.py run --base REF --state releases.json
        [--entry NAME] [--dry-run]

Environment:
    ZENODO_TOKEN   Zenodo access token (deposit:write, deposit:actions).
                   Without it the Zenodo step is skipped and reported.
    ZENODO_API     API root. Defaults to the sandbox,
                   https://sandbox.zenodo.org/api; set it to
                   https://zenodo.org/api to mint real DOIs.
    REGISTRY_QUIET, REGISTRY_SITE_URL   see registry.py / collect_and_render.py

Requires `pyyaml` and `requests`, and a full-history checkout for `--base`.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from identifiers import identifier_url, split  # noqa: E402
from registry import (  # noqa: E402
    ENTRY_FILE, GITHUB_REPO, QUIET, REPO_ROOT, Entry, load_entries,
)

SANDBOX_API = "https://sandbox.zenodo.org/api"
REPO = os.environ.get("GITHUB_REPOSITORY", GITHUB_REPO)

# notebook.yaml relation -> DataCite relation, as Zenodo spells it. The entry
# is the subject: "this entry <relation> that resource".
DATACITE_RELATION = {
    "uses_data": "requires",
    "uses_software": "requires",
    "reproduces": "isSupplementTo",
    "describes": "describes",
    "tutorial_for": "documents",
    "derived_from": "isDerivedFrom",
    "references": "references",
}


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def gets_doi(version: str) -> bool:
    return version_tuple(version) >= (1, 0, 0)


def tag_name(name: str, version: str) -> str:
    return f"{name}/v{version}"


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)


def version_at(ref: str, entry: Entry) -> str | None:
    """The entry's version at a git ref, or None if it did not exist there."""
    r = git("show", f"{ref}:{entry.metadata_path}")
    if r.returncode != 0:
        return None
    try:
        return (yaml.safe_load(r.stdout) or {}).get("version")
    except yaml.YAMLError:
        return None


def swhid(entry: Entry, ref: str = "HEAD") -> str:
    """Software Heritage ID of the entry directory at a commit."""
    r = git("rev-parse", f"{ref}:{entry.directory}" if entry.directory else f"{ref}^{{tree}}")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return f"swh:1:dir:{r.stdout.strip()}"


def image_groups(entry: Entry) -> list[str]:
    """Names of the container image groups built from this entry's notebooks."""
    try:
        from build_notebook_image import collect_groups
    except ImportError:      # nbformat missing: images are simply not listed
        return []
    return [g.name for g in collect_groups() if g.directory == entry.directory]


def releases_to_cut(base: str, only: str | None = None) -> list[dict]:
    """Entries whose version at HEAD differs from their version at `base`."""
    head = git("rev-parse", "HEAD").stdout.strip()
    plans = []
    for entry in load_entries():
        if only and entry.name != only:
            continue
        new = entry.data.get("version")
        old = version_at(base, entry)
        if old is None or new is None or old == new:
            continue
        problem = None
        if version_tuple(new) < version_tuple(old):
            problem = f"version went down from {old} to {new}"
        elif git("rev-parse", "-q", "--verify", f"refs/tags/{tag_name(entry.name, new)}").returncode == 0:
            problem = f"tag {tag_name(entry.name, new)} already exists"
        elif gets_doi(new) and not entry.data.get("authors"):
            problem = "versions >= 1.0.0 must list authors"
        plans.append({
            "name": entry.name,
            "directory": entry.directory,
            "previous_version": old,
            "version": new,
            "tag": tag_name(entry.name, new),
            "commit": head,
            "swhid": swhid(entry),
            "doi": gets_doi(new),
            "images": image_groups(entry),
            "problem": problem,
        })
    return plans


# --- Zenodo -------------------------------------------------------------------

def zenodo_metadata(entry: Entry, plan: dict, site_url: str | None = None) -> dict:
    """Deposit metadata for one release, built only from notebook.yaml."""
    d = entry.data
    creators = []
    for a in d.get("authors", []):
        creator = {"name": a["name"]}
        if a.get("orcid"):
            creator["orcid"] = a["orcid"]
        if a.get("affiliation"):
            creator["affiliation"] = a["affiliation"]
        creators.append(creator)

    related = []
    for r in d.get("related", []):
        prefix, local = split(r["id"])
        relation = DATACITE_RELATION.get(r["relation"], "references")
        if prefix == "doi":
            related.append({"identifier": local, "scheme": "doi", "relation": relation})
        else:
            url = identifier_url(r["id"])
            if url:
                related.append({"identifier": url, "scheme": "url", "relation": relation})
    related.append({
        "identifier": f"https://github.com/{REPO}/tree/{plan['tag']}/{entry.directory}".rstrip("/"),
        "scheme": "url", "relation": "isSupplementTo",
    })
    related.append({
        "identifier": f"https://archive.softwareheritage.org/{plan['swhid']}",
        "scheme": "url", "relation": "isIdenticalTo",
    })
    if site_url:
        related.append({"identifier": site_url.rstrip("/") + "/", "scheme": "url",
                        "relation": "isPartOf"})

    metadata = {
        "upload_type": "software",
        "title": d["title"],
        "description": d.get("description") or d["title"],
        "creators": creators,
        "version": plan["version"],
        "license": d["license"].lower(),
        "publication_date": datetime.datetime.now(datetime.timezone.utc).date().isoformat(),
        "related_identifiers": related,
    }
    if d.get("keywords"):
        metadata["keywords"] = list(d["keywords"])
    return metadata


def entry_archive(entry: Entry, ref: str, out: Path) -> Path:
    """Zip of the entry directory exactly as committed at `ref`."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tree = f"{ref}:{entry.directory}" if entry.directory else ref
    r = git("archive", "--format=zip", f"--prefix={entry.name}/", "-o", str(out), tree)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return out


def zenodo_deposit(entry: Entry, plan: dict, archive: Path, previous: dict | None,
                   token: str, api: str) -> dict:
    """Create (or version) the entry's Zenodo record and publish it."""
    import requests

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"

    def call(method: str, url: str, **kw):
        r = session.request(method, url, timeout=300, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"Zenodo {method} {url} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else {}

    if previous and previous.get("zenodo_id"):
        new_version = call("POST", f"{api}/deposit/depositions/{previous['zenodo_id']}/actions/newversion")
        draft = call("GET", new_version["links"]["latest_draft"])
        for f in draft.get("files", []):      # a new version starts with the old files
            call("DELETE", f"{api}/deposit/depositions/{draft['id']}/files/{f['id']}")
    else:
        draft = call("POST", f"{api}/deposit/depositions", json={})

    with archive.open("rb") as fh:
        call("PUT", f"{draft['links']['bucket']}/{archive.name}", data=fh)
    call("PUT", f"{api}/deposit/depositions/{draft['id']}",
         json={"metadata": zenodo_metadata(entry, plan, os.environ.get("REGISTRY_SITE_URL"))})
    published = call("POST", f"{api}/deposit/depositions/{draft['id']}/actions/publish")
    return {
        "doi": published.get("doi"),
        "concept_doi": published.get("conceptdoi"),
        "zenodo_id": published.get("id"),
        "zenodo_url": published.get("links", {}).get("html") or published.get("links", {}).get("record_html"),
        "sandbox": "sandbox" in api,
    }


def request_swh_archival() -> str:
    """Ask Software Heritage to archive the repository. Returns a short status."""
    import requests

    url = f"https://archive.softwareheritage.org/api/1/origin/save/git/url/https://github.com/{REPO}/"
    try:
        r = requests.post(url, timeout=60)
        return f"requested ({r.status_code})"
    except requests.RequestException as e:
        return f"request failed: {e}"


# --- state --------------------------------------------------------------------

def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"schema_version": 1, "entries": {}}


def record(state: dict, plan: dict, result: dict) -> None:
    """Add one release to the state. Releases of an entry are kept oldest first."""
    entry = state["entries"].setdefault(plan["name"], {"releases": []})
    if result.get("concept_doi"):
        entry["concept_doi"] = result["concept_doi"]
    entry["releases"] = [r for r in entry["releases"] if r["version"] != plan["version"]]
    entry["releases"].append({
        "version": plan["version"],
        "date": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "tag": plan["tag"],
        "commit": plan["commit"],
        "swhid": plan["swhid"],
        "images": plan["images"],
        **{k: result[k] for k in ("doi", "zenodo_id", "zenodo_url", "sandbox") if result.get(k) is not None},
    })
    state["updated"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def latest_release(state: dict, name: str) -> dict | None:
    releases = state.get("entries", {}).get(name, {}).get("releases", [])
    with_record = [r for r in releases if r.get("zenodo_id")]
    return with_record[-1] if with_record else None


# --- commands -----------------------------------------------------------------

def cmd_plan(args) -> int:
    print(json.dumps(releases_to_cut(args.base, args.entry), indent=2))
    return 0


def cmd_run(args) -> int:
    plans = releases_to_cut(args.base, args.entry)
    state_path = Path(args.state)
    state = load_state(state_path)
    entries = {e.name: e for e in load_entries()}
    token = os.environ.get("ZENODO_TOKEN", "")
    api = (os.environ.get("ZENODO_API") or SANDBOX_API).rstrip("/")
    failed = False
    done = []

    for plan in plans:
        label = f"{plan['name']} {plan['previous_version']} -> {plan['version']}"
        if plan["problem"]:
            print(f"::error::{label}: {plan['problem']}; not released")
            failed = True
            continue
        if args.dry_run:
            doi = "Zenodo deposit" if plan["doi"] and token else (
                "no DOI (no ZENODO_TOKEN)" if plan["doi"] else "no DOI (below 1.0.0)")
            print(f"[dry run] {label}: would tag {plan['tag']}, record {plan['swhid']}, "
                  f"tag images {plan['images'] or 'none'}; {doi}")
            continue

        entry = entries[plan["name"]]
        r = git("tag", plan["tag"], plan["commit"])
        if r.returncode == 0:
            r = git("push", "origin", f"refs/tags/{plan['tag']}")
        if r.returncode != 0:
            print(f"::error::{label}: could not push tag {plan['tag']}: {r.stderr.strip()}")
            failed = True
            continue

        result: dict = {}
        if QUIET:
            print(f"{label}: quiet mode, Software Heritage archival not requested")
        else:
            print(f"{label}: Software Heritage archival {request_swh_archival()}")
        if plan["doi"] and token:
            try:
                archive = entry_archive(entry, plan["commit"],
                                        state_path.parent / f"{plan['name']}-v{plan['version']}.zip")
                result = zenodo_deposit(entry, plan, archive, latest_release(state, plan["name"]),
                                        token, api)
                archive.unlink()
                print(f"{label}: published {result['doi']}")
            except Exception as e:       # the tag exists; report and keep the record
                print(f"::error::{label}: Zenodo deposit failed: {e}")
                failed = True
        elif plan["doi"]:
            print(f"::warning::{label}: no ZENODO_TOKEN, so no DOI was minted")
        record(state, plan, result)
        done.append(plan)
        print(f"{label}: released as {plan['tag']}")

    if not args.dry_run and done:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, indent=2) + "\n")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"released={json.dumps(done)}\n")
            f.write(f"any_released={'true' if done else 'false'}\n")
    if not plans:
        print("No entry changed its version; nothing to release.")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("plan", cmd_plan), ("run", cmd_run)):
        p = sub.add_parser(name)
        p.add_argument("--base", required=True, help="git ref the versions are compared against")
        p.add_argument("--entry", help="only this entry (its `name`)")
        p.set_defaults(func=func)
        if name == "run":
            p.add_argument("--state", required=True, help="path of releases.json")
            p.add_argument("--dry-run", action="store_true",
                           help="report what would be released; change nothing")
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
