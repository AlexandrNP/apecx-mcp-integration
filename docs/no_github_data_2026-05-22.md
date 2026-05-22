# No GitHub data downloads; install-smoke CI (2026-05-22)

## The break

`uv tool install --python 3.12 git+https://github.com/AlexandrNP/apecx-mcp-integration.git`
— the canonical install command in README / QUICKSTART / INSTALL — failed
for every user:

```
process didn't exit successfully: `git reset --hard <sha>` (exit status: 128)
  Downloading data/faiss_indexes/faiss_index.bin (4.1 MB)
  Error downloading object: ... [0] remote missing object 461019...
  fatal: data/faiss_indexes/faiss_index.bin: smudge filter lfs failed
```

`uv tool install git+...` does a full clone + `git reset --hard <sha>`,
which runs the git-LFS **smudge** filter on every LFS-tracked file in the
target tree. `data/faiss_indexes/faiss_index.bin` was a committed LFS
**pointer whose object was missing on GitHub's LFS storage** → smudge aborts
→ install dies before any package is built.

## Why nothing caught it (the testing gap I own)

The published install path was **never exercised** by any test:

- My "clean install verified" runs used `uv pip install -e '.[dev]'` from a
  **local checkout** — no git-URL clone, so no remote LFS smudge.
- CI's `unit` job uses `actions/checkout@v6` (default `lfs: false` → LFS
  pointers stay as text, never smudged) + `pip install -e '.[dev]'` (installs
  `src/`; the `.bin` files are irrelevant to `pytest tests/unit/`).

Both stayed green while the real command was broken. "Clean install verified"
was a claim about a path users don't run. This is the exact "tests pass,
product broken" shape — now closed by the `install-smoke` CI job below.

## Fixes

### 1. Repo ships code only — no data via GitHub

`data/faiss_indexes/` was deleted (`git rm`): ~685 MB of LFS binaries
(`index.faiss` 428 MB, `index.pkl` 256 MB, `faiss_index.bin` 4 MB,
`metadata.json`). It was **orphan** — the runtime default index dir is
`data/apecx_domain_rag/` (`agents/domain_rag/index.py:89`), not this path;
and it isn't in the wheel (`package-data` ships only `**/*.{yml,yaml,md,ini,txt}`
under `src/`). `.gitattributes` now carries **no LFS rules** (the repo is
LFS-free, so a clone has nothing to smudge), and `.gitignore` blocks
re-committing index artifacts (`data/faiss_indexes/`, `data/apecx_domain_rag/`,
`*.faiss`, `*.pkl`). The RAG/FAISS index is built locally via
`apecx-setup rag`, as designed (G81).

Also corrected `data/README.md`, which still claimed a `gh release download`
fallback — that was retired with the Globus migration (G127). Datasets now
come **only** over Globus (`apecx-setup data`).

### 2. `install-smoke` CI job

`.github/workflows/tests.yml` gains a job that runs the **exact** README
command — `uv tool install --python 3.12 git+...@<ref>` — on a fresh runner,
then asserts the three console scripts resolve and respond (`apecx-setup
--help`, `apecx-cp --help`; never `apecx-mcp --help`, which boots the server),
and that the install downloaded **no dataset** (`~/.apecx/data` stays empty).
push-only (a PR's synthetic merge-SHA isn't fetchable by the public git URL).

## Honest scope note — Globus is NOT a like-for-like replacement for `gh`

CI does **no** data download, on purpose. The retired `gh release download`
was **headless and zero-auth** (a plain HTTPS GET of a public asset). Globus's
**default** is the native/web **thick client** (browser device-code) — not
headless. The **thin-client + secret** (M2M) path *is* headless and *is*
implemented, but needs provisioned credentials (+ `apecx-project-all` Group
membership for VIOLIN). So:

- Earlier claim that Globus "completely superseded" the alternatives was an
  **overclaim**. Globus replaced the interactive-user default and (via the
  thin client) covers headless — but it did not preserve gh's zero-auth
  property.
- A CI job that actually *downloads data* would require provisioning M2M
  secrets as GitHub Actions secrets. That is a deliberate, separate decision
  (out of scope here); the `install-smoke` job validates the install only.

## Verification

- Local: clone HEAD + `git reset --hard` produces no LFS smudge (repo is
  LFS-free). [recorded in the commit]
- CI: the `install-smoke` job runs the real `uv tool install git+...@<branch>`
  green on push. [the job is the proof; first green run recorded on the PR]
