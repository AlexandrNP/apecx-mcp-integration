"""build_env_manifest — multi-repo provenance stamp for a composed workflow.

STAMP ONLY. Records, per contributing repo, the git SHA + dirty flag; key package
versions; the LLM model/base_url; python + platform. A clean SHA over a DIRTY tree is
DECEPTIVE provenance, so ``reproducible`` is False unless EVERY contributing repo has a
clean SHA. This does NOT verify or reproduce — that is Project B (the consumer of this
stamp). Behavior lives in the editable-installed Python (apecx_integration + nanobrain),
so the manifest pins THOSE repos, not just the composition tree.
"""

from __future__ import annotations

import importlib.metadata as _md
import json
import os
import platform
import subprocess
from pathlib import Path

# Editable-installed packages whose source repos' code determines run behavior.
_REPO_PACKAGES = ("apecx-integration", "nanobrain")
# Versions worth recording for the provenance audit (3rd-party + the framework).
_KEY_PACKAGES = ("pydantic", "nanobrain", "faiss-cpu", "sentence-transformers")


def _editable_src(pkg: str) -> Path | None:
    """The on-disk source dir of an editable install, via PEP 610 direct_url.json.

    Iterates ALL distributions (not ``distribution(pkg)``, which returns the FIRST
    dist-info match — nondeterministic when a stale duplicate dist-info shadows the
    editable one, e.g. a leftover ``apecx_integration-*.dist-info`` with no direct_url).
    Returns the first matching name that carries a resolvable ``file://`` direct_url.
    """
    target = pkg.replace("_", "-").lower()
    for dist in _md.distributions():
        try:
            name = (dist.metadata["Name"] or "").replace("_", "-").lower()
        except Exception:
            continue
        if name != target:
            continue
        try:
            raw = dist.read_text("direct_url.json")
        except Exception:
            raw = None
        if not raw:
            continue
        try:
            url = json.loads(raw).get("url", "")
        except Exception:
            continue
        prefix = "file://"
        if url.startswith(prefix):
            return Path(url[len(prefix) :])
    return None


def _git_state(src: Path) -> dict:
    """{sha, dirty, src, vcs} for a checkout. vcs=False when src is not a git repo."""

    def _git(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", "-C", str(src), *args],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return out.stdout.strip() if out.returncode == 0 else None
        except Exception:
            return None

    sha = _git("rev-parse", "HEAD")
    if sha is None:
        return {"sha": None, "dirty": None, "src": str(src), "vcs": False}
    porcelain = _git("status", "--porcelain")
    return {"sha": sha, "dirty": bool(porcelain), "src": str(src), "vcs": True}


def _compute_reproducible(repos: dict) -> bool:
    """Reproducible iff every repo has a clean SHA. A missing SHA (no VCS) or ANY dirty
    tree makes a SHA-based reproduction claim a lie."""
    if not repos:
        return False
    have_all_shas = all(r.get("sha") for r in repos.values())
    any_dirty = any(r.get("dirty") for r in repos.values() if r.get("vcs"))
    return have_all_shas and not any_dirty


def _pkg_version(pkg: str) -> str | None:
    try:
        return _md.version(pkg)
    except Exception:
        return None


def build_env_manifest(*, llm_model: str | None = None, llm_base_url: str | None = None) -> dict:
    """Build the provenance manifest. ``llm_*`` default to the ``APECX_LLM_*`` env vars."""
    repos: dict[str, dict] = {}
    for pkg in _REPO_PACKAGES:
        src = _editable_src(pkg)
        repos[pkg] = (
            _git_state(src)
            if src is not None
            else {"sha": None, "dirty": None, "src": None, "vcs": False}
        )
    return {
        "repos": repos,
        "key_packages": {p: _pkg_version(p) for p in _KEY_PACKAGES},
        "llm": {
            "model": llm_model if llm_model is not None else os.environ.get("APECX_LLM_MODEL"),
            "base_url": llm_base_url
            if llm_base_url is not None
            else os.environ.get("APECX_LLM_BASE_URL"),
        },
        "python": platform.python_version(),
        "platform": platform.platform(),
        "reproducible": _compute_reproducible(repos),
    }


__all__ = ["build_env_manifest"]
