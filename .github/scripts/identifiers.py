"""Prefixed identifiers used in `related:` (e.g. `dandi:000055`, `doi:10.…`).

Each prefix has a syntax check and a URL template. These are the seed of the
per-prefix resolver plugins described in docs/registry-design.md; adding an
archive means adding a prefix here (and, later, an online resolver).
Prefixes follow Bioregistry where it defines one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote


@dataclass(frozen=True)
class Prefix:
    pattern: str        # regex the local part must fully match
    url: str            # template with {id}
    description: str


PREFIXES: dict[str, Prefix] = {
    "dandi": Prefix(r"\d{6}(/(draft|\d+\.\d+\.\d+))?",
                    "https://dandiarchive.org/dandiset/{id}",
                    "DANDI Archive dandiset, optionally /version"),
    "doi": Prefix(r"10\.\d{4,9}/\S+", "https://doi.org/{id}", "Digital Object Identifier"),
    "openneuro": Prefix(r"ds\d{6}", "https://openneuro.org/datasets/{id}",
                        "OpenNeuro dataset"),
    "zenodo": Prefix(r"\d+", "https://zenodo.org/records/{id}", "Zenodo record"),
    "figshare": Prefix(r"\d+", "https://doi.org/10.6084/m9.figshare.{id}", "Figshare item"),
    "rrid": Prefix(r"[A-Za-z]+_[A-Za-z0-9_:-]+", "https://scicrunch.org/resolver/RRID:{id}",
                   "Research Resource Identifier"),
    "arxiv": Prefix(r"\d{4}\.\d{4,5}(v\d+)?", "https://arxiv.org/abs/{id}", "arXiv preprint"),
    "pmid": Prefix(r"\d+", "https://pubmed.ncbi.nlm.nih.gov/{id}", "PubMed ID"),
    "pmc": Prefix(r"PMC\d+", "https://www.ncbi.nlm.nih.gov/pmc/articles/{id}",
                  "PubMed Central ID"),
    "github": Prefix(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", "https://github.com/{id}",
                     "GitHub repository (owner/repo)"),
    "swh": Prefix(r"1:(cnt|dir|rev|rel|snp):[0-9a-f]{40}(;.*)?",
                  "https://archive.softwareheritage.org/swh:{id}",
                  "Software Heritage persistent identifier"),
}


def split(identifier: str) -> tuple[str, str]:
    prefix, _, local = identifier.partition(":")
    return prefix, local


def validate(identifier: str) -> str | None:
    """None if `identifier` is well formed, otherwise the problem."""
    prefix, local = split(identifier)
    if prefix not in PREFIXES:
        known = ", ".join(sorted(PREFIXES))
        return f"unknown identifier prefix {prefix!r} (known: {known})"
    if not re.fullmatch(PREFIXES[prefix].pattern, local):
        return f"{identifier!r} is not a valid {prefix}: id ({PREFIXES[prefix].description})"
    return None


def identifier_url(identifier: str) -> str | None:
    prefix, local = split(identifier)
    if prefix not in PREFIXES or validate(identifier):
        return None
    return PREFIXES[prefix].url.format(id=quote(local, safe="/:.;_-"))
