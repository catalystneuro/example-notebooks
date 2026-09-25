# Notebook Registry — design proposal

Status: draft. Starting point: `dandi/example-notebooks` (77 notebooks, uv/Colab
lock tooling, per-group Docker images, weekly sweep, PR previews,
`notebooks.json` index).

## Goal

Turn a single-archive example repo into a general **registry of reproducible
notebooks**. It should not be tied to DANDI. A notebook entry can be linked to
papers, datasets in any archive (DANDI, OpenNeuro, Zenodo, Figshare, EMBER,
IBL, ...), journals, courses, or projects. Every entry is tested, locked,
containerized, and runnable in Colab. Every entry has named owners who
maintain it.

## Submission model: follow conda-forge

conda-forge scales to about 25k community-maintained packages with a small
core team. Its process fits this problem well:

| conda-forge | Notebook registry |
|---|---|
| `staged-recipes` repo, one PR per new recipe | `staged-notebooks` repo, one PR per new entry |
| `meta.yaml` with `extra.recipe-maintainers` | `notebook.yaml` with `maintainers` |
| On merge, the bot creates `<pkg>-feedstock` and gives the maintainers commit rights | On merge, the bot creates `<entry>-feedstock` and gives the maintainers commit rights |
| `conda-smithy` rerenders shared CI into each feedstock | `notebook-smithy` rerenders shared CI (these workflows) into each feedstock |
| `regro-cf-autotick-bot` opens version-bump PRs | `relock-bot` opens re-lock PRs (Colab snapshot drift, weekly failures) |
| Maintainers merge their own feedstock PRs | Maintainers merge their own feedstock PRs, with no core review after acceptance |
| `conda-forge.org` package index | Registry website + `registry.json` |

The key property is that **ownership lives in the feedstock**. Maintainers
control their own repo. The core team reviews each entry once, at intake.
Maintenance is spread across owners, and the bot does the routine work.

### Repos (new GitHub org, e.g. `notebook-registry`)

```
staged-notebooks/            # intake: PRs add entries/<name>/
  entries/<name>/
    notebook.yaml            # metadata + maintainers (schema below)
    README.md                # optional; generated from notebook.yaml if absent
    requirements.in          # direct deps (or environment.yml, see Environments)
    *.ipynb
    helpers/…                # optional colocated .py
<name>-feedstock/            # one per accepted entry, maintainers have write
  same layout as above, plus rendered .github/workflows/*
notebook-smithy/             # the tooling: today's .github/scripts, packaged
  lock, run, build-image, lint, render-feedstock, reusable workflows
registry/                    # aggregator: crawls feedstocks → registry.json + site
registry-web/                # submission/dashboard app (below)
```

`dandi/example-notebooks` stays as a DANDI-branded view: a filtered catalog of
entries with `dandi:` resources. It stops being where notebooks are stored.

## Metadata: `notebook.yaml`

```yaml
schema_version: 1
name: peterson21-brunton-ecog          # registry-unique slug → feedstock name
title: Behavioral and neural variability of naturalistic arm movements
description: Reproduces figures 2–4 of Peterson et al. 2021.
maintainers:                           # GitHub handles; ORCID optional
  - github: stepeter
    orcid: 0000-0002-xxxx-xxxx
license: BSD-3-Clause
notebooks:
  - path: figure2.ipynb
    title: Movement event detection
    runtime_minutes: 4                 # CI timeout hint
    colab: true
    test: true                         # replaces notebook-test-exclusions.txt
    test_skip_reason: null
related:                               # typed, prefix-resolved identifiers
  - id: dandi:000055                   # optional version: dandi:000055/0.220127.0436
    relation: uses_data                # uses_data | reproduces | describes | tutorial_for | derived_from
  - id: doi:10.1523/ENEURO.0007-21.2021
    relation: reproduces
  - id: rrid:SCR_017571
    relation: uses_software
keywords: [ecog, naturalistic-behavior]
collections: [dandi, cosyne-2023]      # curated groupings (conferences, journals, courses)
```

- **Identifiers**: use [identifiers.org](https://identifiers.org)/Bioregistry
  prefixes: `dandi:`, `doi:`, `openneuro:`, `zenodo:`, `rrid:`, `arxiv:`,
  `pmid:`, and others. One resolver plugin per prefix supplies the title,
  URL, and license for the index. DANDI's plugin reuses
  `get_dandiset_metadata()` from `collect_and_render.py`. Supporting a new
  archive then only needs a small plugin, with no schema change.
- **Relations** map to DataCite `relationType` (`References`, `IsSupplementTo`,
  …). This allows a later mint of a DOI per entry version (for example via
  Zenodo), with correct links back to papers and datasets.
- **Per-notebook flags replace the three `.github/*.txt` lists**
  (`test`/`colab`/image inclusion). Each notebook's flags live in its own
  entry.
- A JSON Schema lives in `notebook-smithy`. It is used by the linter, the web
  form, and the index.

## Environments

The current pipeline is: `requirements.in` → `uv pip compile` constrained by
the Colab snapshot → pinned install cell → Docker image. Keep that as the
default, because it is what makes the Colab button work. Add:

- **`environment.yml` (conda) as an option**, locked with `pixi`/`conda-lock`
  to `conda-lock.yml`. It is for notebooks that need non-PyPI dependencies
  (MATLAB runtime, CUDA, R, compiled neuro tools). The Docker image builds from
  the conda lock. The Colab button is only shown when a pip-only lock also
  resolves, or it goes through `condacolab`.
- **Locks live next to the notebook, not only inside the install cell.** Add
  `requirements.lock.txt`, which is the image input and makes diffs readable.
  The install cell is still regenerated from it.
- The relock-bot runs `refresh_colab_snapshot.py --relock` for each feedstock
  and opens PRs. Maintainers merge them after CI passes.
- Images publish to `ghcr.io/notebook-registry/<name>:{latest,YYYY-MM-DD,sha-,hash-}`
  (same tag scheme as today).

## Web interface (`registry-web`)

The web app is a front end over GitHub. It is not a second source of truth.
Everything it does becomes a commit or a PR, so review, history, CI, and
ownership keep working the same way.

**Accounts**: sign in with GitHub, which is required because ownership means
GitHub commit rights. Users can link an ORCID for attribution and for
"my notebooks" search. The app is a **GitHub App** installed on the org, and
it opens PRs on behalf of users.

**Flows**

1. *Submit* (no code): drag in one or more `.ipynb`, plus an optional
   `README.md` and `requirements.in`/`environment.yml`. A form collects
   title, description, related resources, co-maintainers, and license.
   Related resources autocomplete from the resolver plugins: paste a DOI or
   a DANDI URL and the app resolves it.
   - If there is no `requirements.in`, the app infers direct dependencies
     from the imports (`backfill_requirements.py` already does this) and
     shows them for confirmation.
   - Before the PR, the app runs static checks in the browser or backend:
     nbformat, headless gotchas (`fig.show()`, `input()`, local paths),
     notebook size, and output stripping.
   - The app opens a PR to `staged-notebooks` authored by the GitHub App, with
     `Co-authored-by` set to the user. The existing CI runs, and the
     PR-preview checklist shows up on the user's dashboard as well as on
     GitHub.
2. *Dashboard*: entries where the user is a maintainer (read from
   `notebook.yaml` across feedstocks), with CI status, weekly-sweep status,
   image tags, Colab link, open bot PRs, and a one-click "merge relock PR".
3. *Update*: upload a new notebook version or edit metadata. The app opens a
   PR on the feedstock, which the maintainer can merge from the dashboard.
4. *Browse/search*: faceted by related resource, archive, collection,
   keyword, and status. It is backed by `registry.json`, so the site can be
   static plus a small API.

**Stack** (suggested): FastAPI backend (GitHub App + OAuth, static checks,
upload staging in S3/R2), a small Postgres cache of `registry.json` for
search and dashboards (rebuildable from GitHub at any time), and a
React/Next or SvelteKit front end. The catalog pages can also be pre-rendered
statically.

## CI (moved into `notebook-smithy`, largely unchanged)

| Existing | Becomes |
|---|---|
| `lock_notebook.py`, `run_notebook.py`, `build_notebook_image.py`, `pr_preview.py` | `notebook-smithy` package (`pip install notebook-smithy`) |
| `test-changed-notebooks.yml`, `pr_preview.yml`, `deploy-executed-notebooks.yml` | reusable workflows called from staged-notebooks + each feedstock |
| `build-notebook-images.yml` | reusable workflow; one image per feedstock (or per pin-group within it) |
| `test-all-notebooks-weekly.yml` | central sweep across all feedstocks. It opens issues **on the feedstock** and pings maintainers, and marks the entry `failing` in the index after N weeks |
| `index_workflow.yml` + `collect_and_render.py` | `registry` crawler; DANDI-specific parts become the `dandi:` resolver plugin |
| exclusion `.txt` files | per-notebook flags in `notebook.yaml` |
| new | `lint`: schema validation, identifier resolution, headless checks, license present, maintainers exist |
| new | `render-feedstock`: create repo, set team permissions, render workflows (the conda-smithy role) |

## Migration of the existing 77 notebooks

1. Write `notebook.yaml` for each current directory by script. Maintainers
   come from git blame / PR authors, `related` from the dandiset ID in the
   path plus README DOIs, and flags from the three `.txt` lists. Humans review
   the result.
2. Create feedstocks in bulk with `render-feedstock`. Invite the original
   authors as maintainers. Until they accept, DANDI core stays as a fallback
   maintainer.
3. Keep `dandi/example-notebooks` working: it becomes a generated view
   (for JupyterHub clone compatibility). `notebooks.dandiarchive.org/notebooks.json`
   keeps its shape, populated from `registry.json` filtered to `dandi:`.

## Phasing

1. **Schema + smithy**: `notebook.yaml` schema, extract scripts into
   `notebook-smithy`, per-notebook flags, lint. Still runs in this monorepo.
2. **Staged + feedstocks**: `staged-notebooks`, `render-feedstock`, relock-bot,
   central weekly sweep, and migration of existing notebooks.
3. **Registry index + site**: crawler, resolver plugins (dandi, doi,
   openneuro, zenodo), faceted catalog, DANDI filtered view.
4. **Web app**: GitHub App, upload → PR, dashboard, metadata editing.
5. **Later**: conda-lock envs, DOI minting per release, embeddable widgets
   for archives and journals ("notebooks using this dataset"), and GPU and
   long-running runners.

## Open questions

- Org and name, and whether DANDI hosts it or a neutral org does.
- GitHub-only accounts, or also ORCID-only submitters? ORCID-only would need
  bot-owned feedstocks with ownership recorded in the app rather than in git
  permissions.
- Feedstock-per-entry vs. a single monorepo with CODEOWNERS. Feedstocks
  scale ownership better. A monorepo is simpler below ~200 entries and keeps
  the JupyterHub clone trivial.
- Compute budget for the sweep once non-DANDI entries arrive (GitHub-hosted
  runners vs. self-hosted).
