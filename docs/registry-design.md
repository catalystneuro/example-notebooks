# Notebook Registry: design proposal

Status: draft. Starting point: `dandi/example-notebooks`. It has 77 notebooks,
uv/Colab lock tooling, per-group Docker images, a weekly test sweep, PR
previews, and a `notebooks.json` index.

## Goal

Turn a single-archive example repo into a general **registry of reproducible
notebooks**. The registry is not tied to DANDI. An entry can be linked to
papers, to datasets in any archive (DANDI, OpenNeuro, Zenodo, Figshare, EMBER,
IBL, ...), and to journals, courses, or projects. Every entry is tested, locked,
containerized, and runnable in Colab. Every entry has named maintainers who own
it.

People submit either through a pull request or through a web app that needs no
code. The web app takes one or more notebooks, plus an optional README and an
optional `requirements.in`. Each released version of an entry gets a DOI, so
papers and datasets can cite it.

### Positioning

Other services overlap with this one. [NeuroLibre](https://neurolibre.org)
publishes reproducible preprints (Jupyter Book, Docker, DOIs).
[Code Ocean](https://codeocean.com) sells "compute capsules" to journals.
[Binder](https://mybinder.org) runs repos on demand. The registry differs in
these ways:

- **Linked to data archives.** Entries point to the datasets they use, and
  archives can show "notebooks that use this dataset".
- **Colab first.** Every entry runs in free Colab with one click. A pinned
  Docker image is kept as the permanent fallback.
- **Tested continuously.** Entries are checked on a schedule, and their current
  status is public.
- **Free, open, and community-maintained.** Entry authors own their entries.
  Curators run collections. Everything lives in public git.

The registry should work well alongside these services. Where practical, its
environment files follow the formats repo2docker understands, so an entry can
also be launched on Binder.

## Model: one registry repo, with maintainer ownership

The registry is **one repository**. It follows [bioconda](https://github.com/bioconda/bioconda-recipes)
(about 10k recipes in one repo, run by a small core team) and nixpkgs (about
100k packages, where each package's maintainers merge their own changes through
a bot). It also borrows one idea from the [Julia General registry](https://github.com/JuliaRegistries/General):
an entry can point to code that lives in the author's own repository.

Ideas taken from these projects:

- **Core review happens once, at intake.** A new entry needs one core-team
  approval. After that, the entry's maintainers merge their own changes.
- **Ownership lives in metadata.** Each entry's `notebook.yaml` lists its
  maintainers. A merge bot enforces it, so maintainers need no write access to
  the repo.
- **A bot does the routine upkeep.** It re-locks environments when Colab's
  runtime drifts and reports weekly test failures to the entry's maintainers.

### Why not a repo per entry (conda-forge feedstocks)

conda-forge gives every package its own repo because packages change all the
time: new upstream releases, rebuilds when dependencies change, many platforms,
and active maintainers. Notebooks are mostly published once and fixed now and
then. For notebooks, a repo per entry would mean:

- tens to hundreds of repos, each with CI that has to be kept in sync
- per-repo permissions and container packages
- a crawler just to rebuild the catalog
- breaking the DANDI JupyterHub clone, which expects one repo
- many abandoned repos, because authors are often not software maintainers

If the registry reaches thousands of entries, or a community wants its own
governance, entries can be split out into their own repos later. The metadata
and tooling do not depend on the layout.

## Two kinds of entries

An **entry** is any directory that contains a `notebook.yaml`.

### Hosted entries

The notebooks live in the registry repo. This is what the web app produces,
and it is how every existing notebook works today.

```
entries/<slug>/
  notebook.yaml          # metadata + maintainers (schema below)
  README.md              # optional; rendered from notebook.yaml if absent
  requirements.in        # direct dependencies
  requirements.lock.txt  # generated full pin set (image input, reviewable diff)
  *.ipynb                # with generated Colab bootstrap cells
  helpers/…              # optional colocated .py files
```

Existing directories (`000055/BruntonLab/peterson21/`, `tutorials/…`) **stay
where they are** and get a `notebook.yaml`. That way no GitHub, Colab, or
paper link breaks. Only new entries go under `entries/`.

### External entries

The code stays in the author's repository. The registry keeps only metadata
plus the environment lock:

```yaml
source:
  repo: https://github.com/somelab/paper-code
  ref: 3f2a9c1e…            # full commit SHA; tags are resolved to SHAs at intake
  notebooks: [analysis/fig2.ipynb, analysis/fig3.ipynb]
```

```
entries/<slug>/
  notebook.yaml
  requirements.in        # only if upstream has none; otherwise read from source
  requirements.lock.txt
```

- CI checks out `repo@ref` and tests the listed notebooks with the same
  harness used for hosted entries.
- The registry can't add a Colab install cell to someone else's notebook.
  Instead, CI publishes a **prepared copy**: the notebook plus a Colab badge
  and install cell, written to a `dist` branch. Colab and the catalog link to
  that copy, and the catalog also links to the upstream source.
- To publish a new version, a maintainer bumps `ref` in a PR, which the merge
  bot can merge. An optional GitHub Action in the upstream repo can open that
  PR automatically, similar to Julia's Registrator.
- The lint step checks that the upstream license allows redistribution. The
  prepared copy and the Docker image both redistribute the author's code.

## Metadata: `notebook.yaml`

```yaml
schema_version: 1
name: peterson21-brunton-ecog          # registry-unique slug
version: 1.2.0                         # bumped to cut a release (see Releases and DOIs)
title: Behavioral and neural variability of naturalistic arm movements
description: Reproduces figures 2–4 of Peterson et al. 2021.
authors:                               # credited as DOI creators, in order
  - name: Steven M. Peterson
    orcid: 0000-0002-xxxx-xxxx
    affiliation: University of Washington
maintainers:                           # GitHub handles with merge rights; may differ from authors
  - github: stepeter
license: BSD-3-Clause
notebooks:
  - path: figure2.ipynb
    title: Movement event detection
    runtime_minutes: 4                 # CI timeout hint
    test: true                         # replaces notebook-test-exclusions.txt
    colab: true                        # replaces notebook-colab-exclusions.txt
    image: true                        # replaces notebook-image-inclusions.txt
    skip_reason: null                  # required when test or colab is false
related:
  - id: dandi:000055                   # optionally versioned: dandi:000055/0.220127.0436
    relation: uses_data
  - id: doi:10.1523/ENEURO.0007-21.2021
    relation: reproduces
  - id: rrid:SCR_017571
    relation: uses_software
keywords: [ecog, naturalistic-behavior]
collections: [dandi, cosyne-2023]      # curated groupings (see Collections and curation)
```

- **Authors and maintainers are separate lists.** Authors are the people who
  get credit. Maintainers are the people who can merge changes. A postdoc may
  author an entry and a lab's research software engineer may maintain it.
  Authors don't need GitHub accounts.

- **Identifiers** use [Bioregistry](https://bioregistry.io)/identifiers.org
  prefixes, such as `dandi:`, `doi:`, `openneuro:`, `zenodo:`, `rrid:`,
  `arxiv:`, and `pmid:`. Each prefix has a small **resolver plugin** that
  returns a title, URL, and license for the catalog, and checks that the ID
  exists at lint time. The DANDI plugin reuses `get_dandiset_metadata()` from
  `collect_and_render.py`. Supporting a new archive means writing a plugin;
  the schema does not change.
- **Relations** (`uses_data`, `reproduces`, `describes`, `tutorial_for`,
  `derived_from`, `uses_software`) map onto DataCite `relationType`. That
  keeps the option open to mint a DOI for each entry version later, with
  correct links to papers and datasets.
- **Per-notebook flags replace the three `.github/*.txt` lists.** Each entry
  carries its own settings and the reasons for them.
- The JSON Schema is shared by the linter, the web form, and the catalog.

## Ownership and the merge bot

GitHub's native `CODEOWNERS` doesn't fit here. It only honors users who have
write access to the repo. Giving hundreds of notebook authors write access to
a shared repo would also let them approve changes to each other's entries. So
ownership is enforced by a **merge bot**, modeled on the
[nixpkgs merge bot](https://github.com/NixOS/nixpkgs-merge-bot).

A maintainer comments `@registry-bot merge` on a PR. The bot merges it when all
of these hold:

1. Every changed file is inside entries where the commenter is a maintainer.
   The bot reads the maintainer list from **`main`**, not from the PR, so a PR
   can't add its own author as a maintainer and then merge itself.
2. The PR does not create a new entry, and it does not add the entry to a
   collection. Both need a curator's approval (see below).
3. It does not touch tooling, workflows, or shared config. Those also need core
   review.
4. All required checks pass: lint, test of the changed notebooks, and the
   image build dry run.

Other rules:

- **Adding a maintainer** needs approval from an existing maintainer. A
  maintainer can remove themselves at any time.
- **Notifications.** When a PR touches an entry, the bot @-mentions that
  entry's maintainers and requests their review. This covers the notification
  part that CODEOWNERS would otherwise provide.
- **Relock PRs** opened by the bot on an entry can be merged by any of its
  maintainers the same way. The core team can also batch-merge relock PRs that
  pass CI.

## Collections and curation

A single core team can't review new entries for every archive, journal, and
course. Intake review is therefore delegated to **collections**. Each
collection has a file in `collections/<name>.yaml`:

```yaml
name: dandi
title: DANDI Archive
description: Notebooks that use data from the DANDI Archive.
curators: [yarikoptic, bendichter, ...]   # GitHub handles
intake:                                   # shown to submitters and used by lint
  requires_related_prefix: [dandi]        # entry must reference at least one dandiset
  guidelines: https://…
```

- **Joining a collection.** A new entry, or an existing entry being added to a
  collection, needs approval from one curator of that collection. The merge bot
  checks this the same way it checks maintainers, against the curator list on
  `main`.
- **Entries with no collection** go to a small `general` collection, which the
  core team curates.
- **Creating a collection** is a PR to `collections/`, reviewed by the core
  team. Examples: a journal's collection, a course, a conference tutorial
  series, a lab's collection.
- **Curators own intake quality for their collection:** scope, data
  availability, and documentation. Technical checks (tests, lint, license)
  are automated and the same for every collection.
- Each collection gets a filtered catalog view and a feed. It can optionally
  have its own Zenodo community.

## Environments

The current pipeline stays the default, because it is what makes the Colab
button work:

`requirements.in` → `uv pip compile` (constrained by the Colab snapshot) →
pinned install cell + `requirements.lock.txt` → Docker image.

- `requirements.lock.txt` is new. It is committed next to the notebooks, so
  lock diffs can be reviewed and the image build has a single input. The
  install cell is still generated from it.
- The relock bot runs `refresh_colab_snapshot.py --relock` and opens one PR
  per entry. Today it opens one PR for the whole repo. Splitting it up lets
  each entry's maintainers merge their own relock.
- **Later:** optional `environment.yml`, locked with pixi or conda-lock, for
  notebooks that need non-PyPI dependencies (R, CUDA, compiled tools). Those
  entries get a Docker image, but a Colab button only when a pip lock also
  resolves.
- Images are published as `ghcr.io/<org>/notebooks/<slug>` with the current
  tag scheme (`latest`, `YYYY-MM-DD`, `sha-…`, `hash-…`). Released versions
  also get a `vX.Y.Z` tag (see below).
- **Binder compatibility.** `requirements.txt` (the lock) and
  `environment.yml` are both formats repo2docker reads. The catalog can
  therefore offer a "Launch on Binder" link for each entry release, with no
  extra files. The custom Dockerfile stays as the verified build, because it
  pins the base image and separates the JupyterLab environment from the
  kernel environment.
- **Notebook outputs are stripped on commit.** Lint rejects notebooks with
  outputs above a size limit. Executed copies are published to the catalog
  and PR previews, as PR previews do today. This keeps the repo, and the
  JupyterHub clone, from growing with every plot.

## Releases and DOIs

An entry changes on `main` over time, but papers and datasets need to cite a
fixed version. So each entry has **its own versioned releases**.

- **To cut a release**, a maintainer bumps `version` in `notebook.yaml` in a
  PR. The merge bot can merge it like any other change to the entry. A
  release is permanent, so bumping `version` has to be deliberate. Other
  merges don't create a release.
- **On merge,** a deploy job:
  1. tags the registry commit `<slug>/v<version>`
  2. tags the verified image `ghcr.io/<org>/notebooks/<slug>:v<version>`
  3. records the Software Heritage directory ID (`swh:1:dir:…`) of the
     entry at that commit, after requesting archival
  4. deposits the entry on Zenodo and publishes it
- **Zenodo deposit** (through the REST API, not Zenodo's GitHub integration,
  which archives whole repos):
  - **Files:** a zip of the entry directory (notebooks, README,
    `notebook.yaml`, `requirements.in`, `requirements.lock.txt`, helpers),
    plus the executed notebooks as HTML.
  - **Creators** come from `authors`, not from maintainers or git history.
  - **Related identifiers** come from `related` (mapped to DataCite relation
    types), plus the registry commit, the SWHID, the image digest, and the
    catalog page.
  - **Versioning.** The first release creates a record whose *concept DOI*
    identifies the entry across all versions. Each later release adds a new
    version, with its own DOI, to that record. `registry.json` stores both
    DOIs.
  - **Communities.** Every deposit goes into the registry's Zenodo community,
    and into each collection's community if the collection has one.
- **External entries.** If the upstream repo already has a DOI (for example
  from Zenodo's GitHub integration), the entry links to it with `IsIdenticalTo`
  and the registry does not mint a new one. Otherwise the registry deposits
  the prepared copy, if the upstream license allows it.
- **Credentials.** The Zenodo token exists only in the deploy job. That job
  runs on `main` after merge, never in PR context. Integration testing uses
  Zenodo's sandbox.
- **Later:** once volume justifies it, the registry could mint DataCite DOIs
  directly (as DANDI does), with landing pages on the catalog. Zenodo would
  remain the file archive.

## CI

Almost everything carries over. The changes:

| Existing | Change |
|---|---|
| `lock_notebook.py`, `run_notebook.py`, `build_notebook_image.py`, `pr_preview.py` | Read settings from `notebook.yaml`; write `requirements.lock.txt`; understand external entries (check out `repo@ref`) |
| `list_notebooks.py` + exclusion `.txt` files | Discover entries by `notebook.yaml`; use the per-notebook flags |
| `test-changed-notebooks.yml`, PR preview | Also triggered by `notebook.yaml` changes, including a bumped `ref` on an external entry |
| `test-all-notebooks-weekly.yml` | Split into shards to stay under the 256-job matrix limit. Open **one issue per failing entry**, labeled with the entry and mentioning its maintainers, instead of one issue for everything |
| `index_workflow.yml` + `collect_and_render.py` | Generate `registry.json` from all `notebook.yaml` files. DANDI-specific code becomes the `dandi:` resolver plugin. Keep publishing `notebooks.json` for DANDI as a filtered view |
| new: `lint.yml` | Schema validation, identifier resolution, maintainer handles exist, license present, headless-gotcha checks, output size limits |
| new: merge bot | See above |
| new: `dist` branch publish | Prepared copies of external-entry notebooks for Colab |

**Two kinds of status.** They answer different questions, so they are shown
separately:

- **Verified at release.** Every notebook in the release ran successfully
  inside the released image. This is recorded once and never changes. It
  shows that the notebook reproduced, and the image and Zenodo deposit keep
  that result reproducible.
- **Current status.** Does the entry still run today, on current Colab and
  against live data? A scheduled sweep sets it to `passing`, `failing` (failed
  N runs in a row), or `unmaintained` (failing, with no maintainer response
  for M weeks). It changes over time, and it costs compute on every run.

The scheduled sweep is **tiered** so its cost doesn't grow with the size of
the registry:

- **Weekly:** entries changed or released in the last 6 months, entries in
  the top N by views, and entries a collection marks as featured.
- **Monthly:** everything else.
- **Immediately:** entries that use a dataset or package that just changed,
  once dependency and dataset links are indexed.

`unmaintained` entries are hidden from the default catalog view but never
deleted. Their released versions stay citable and runnable from the image.

**Security.** Uploaded notebooks are untrusted code. Tests run with a read-only
token and no secrets. Deploys (previews, images) run in separate
`workflow_run` jobs, as `deploy-executed-notebooks.yml` already does. The web
app opens PRs **from a bot-owned fork**, so its PRs are treated like any other
fork PR and never run with the main repo's secrets.

## Catalog

`registry.json` is built from a single repo checkout, so no crawler is needed.
It feeds:

- a static catalog site (GitHub Pages), faceted by related resource, archive,
  collection, keyword, and current status. Each entry page shows:
  - both kinds of status
  - its releases, each with its DOI and image tag
  - how to cite it
  - launch links: Colab, Binder, and `docker run`
  - its related papers and datasets
- the DANDI view (`notebooks.dandiarchive.org/notebooks.json`, same shape as
  today)
- **a per-resource lookup for archives.** A static JSON file is generated for
  each referenced resource, such as `api/related/dandi/000055.json` or
  `api/related/openneuro/ds000246.json`. It lists the entries that reference
  the resource, with status, launch links, and DOIs. An archive shows
  "notebooks that use this dataset" on its dataset pages with a single
  fetch: no API server, no auth, and caching through a CDN.
- an embeddable widget (one `<script>` tag) that renders that list, for
  archives that don't want to build their own UI

## Web app

The web app is a thin front end over GitHub. **GitHub stays the only source
of truth.** Every action in the app becomes a PR or a bot command. The app
keeps no ownership records of its own; at most it caches data from
`registry.json`.

- **Accounts:** sign in with GitHub. A maintainer is anyone whose GitHub
  handle is listed in an entry's `notebook.yaml`. Users can link an ORCID.
  Only maintainers need GitHub. **Authors** are listed by name and ORCID, so
  a PI or a co-author can be credited on the DOI without an account.
- **Submit:**
  1. The user uploads one or more `.ipynb` files, plus an optional README and
     an optional `requirements.in`, or pastes the URL of an external repo and
     picks notebooks from it.
  2. A form, generated from the JSON Schema, collects:
     - title, description, and license
     - authors (looked up by ORCID) and co-maintainers
     - collections to submit to
     - related resources: pasting a DOI or DANDI URL resolves it through the
       resolver plugins
  3. With no `requirements.in`, the app infers direct dependencies from the
     imports (reusing `backfill_requirements.py`) and asks the user to confirm
     them.
  4. The app runs lint checks up front (headless gotchas, local paths,
     notebook size) so obvious problems show up before CI does.
  5. The app opens a PR from the bot fork, with a `Co-authored-by` credit for
     the user. The PR preview checklist and CI status are shown in the app.
- **Dashboard:** the user's entries, with both kinds of status, releases and
  DOIs, image tags, Colab links, and open PRs (including relock PRs). A
  **Merge** button posts the merge-bot command. A **Release** button opens the
  `version` bump PR.
- **Curator queue:** for curators, the entries waiting to join their
  collections, each with its CI results and preview.
- **Update:** upload a new notebook version, edit metadata, or bump an
  external `ref`. Each opens a PR that the maintainer merges from the
  dashboard.

**Stack (suggested):** a FastAPI backend that acts as a GitHub App (OAuth,
opening PRs, reading check status) and stages uploads in object storage. The
front end is a small SPA or server-rendered UI, deployed on its own. Browse
and search can use the static catalog, so the app only handles the signed-in
flows.

## Migration of the existing notebooks

1. Generate a `notebook.yaml` for each current directory with a script:
   - **Maintainers:** the original PR authors and git history.
   - **`related`:** the dandiset ID from the path, plus DOIs found in the
     README.
   - **Authors:** from the README and the linked paper.
   - **Flags:** from the three `.txt` lists.
   - **Collections:** `dandi` for everything, plus `cosyne-2023`,
     `bcm-2024`, and so on for `tutorials/`.

   A person reviews the output. Where no author can be identified, the DANDI
   core team is the maintainer. Existing entries start at `version: 0.1.0`
   and get no DOI until a maintainer cuts a `1.0.0` release. That way nobody
   is credited on a DOI without having reviewed the metadata.
2. Directories stay in place, so no Colab badge, JupyterHub clone, or cited
   link breaks.
3. Retire the `.txt` lists once the flags are in place.
4. If the registry moves to a neutral org, **transfer** the repo rather than
   creating a new one. GitHub redirects old repo URLs after a transfer. Check
   that Colab badge links and the JupyterHub clone follow the redirect before
   relying on it.

## Phasing

1. **Metadata:**
   - the `notebook.yaml` schema (including `version`, `authors`, and
     `collections`) and the `collections/` files
   - lint, per-notebook flags, and output stripping
   - generated metadata for the existing notebooks
   - `requirements.lock.txt` and `registry.json`

   The current `notebooks.json` keeps working.
2. **Ownership and curation:** merge bot (maintainer and curator rules),
   maintainer notifications, per-entry failure issues, per-entry relock PRs,
   and both kinds of status with the tiered sweep.
3. **Releases:** version tags, image `vX.Y.Z` tags, Software Heritage
   archival, Zenodo deposits and DOIs (sandbox first), and a citation block
   on each entry.
4. **Catalog:** resolver plugins (dandi, doi, openneuro, zenodo, rrid), the
   faceted static site, collection views, the DANDI view, and Binder links.
5. **External entries:** `source:` support in the harness, the `dist` branch,
   the optional upstream Action for bumping `ref`, and linking to existing
   upstream DOIs.
6. **Web app:** GitHub App, upload → PR, dashboard, curator queue, release
   button, and metadata editing.
7. **Later:** conda/pixi environments, direct DataCite minting, GPU and
   long-running runners, and journal workflows.

Phases 4 and 5 are independent of each other, and phase 6 can start as soon
as phase 2 lands.

**Year-one audience: DANDI and other data archives.** That sets these
priorities:

- **Archive integration comes first.** The per-resource lookup and the widget
  are part of phase 4, not "later". Resolver plugins are built in the order
  archives are onboarded: dandi, then openneuro, then ember, zenodo, and
  figshare. doi and rrid are needed from the start for paper and software
  links.
- **Each archive gets a collection** whose curators are that archive's staff.
  DANDI is the pilot. Onboarding a second archive is the milestone that shows
  the registry works beyond DANDI.
- **External entries (phase 5) come before the web app (phase 6).** Many
  dataset authors already keep analysis code in their own repos.
- **Journal-specific needs are deferred:** ORCID-only maintainers, journal
  submission workflows, and review-time access for editors.

## Open questions

- **Name and home:** a neutral org, or start under `dandi` and transfer later.
- **Second archive:** which archive to onboard after DANDI. It should be
  one whose staff will curate a collection and put the widget or lookup on
  their dataset pages.
- **Compute:** whether GitHub-hosted runners can handle the weekly sweep once
  non-DANDI entries arrive, or whether self-hosted or sponsored runners are
  needed. Also a policy on `runtime_minutes` limits.
- **Intake criteria:** what curators check, what the `general` collection
  accepts, and who is on the core team.
- **Zenodo account:** which organization owns the Zenodo community and token.
  Zenodo records outlive projects, so it should be an institution, not a
  person.
