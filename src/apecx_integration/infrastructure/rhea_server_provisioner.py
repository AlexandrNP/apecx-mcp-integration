"""Auto-build the rhea-server Docker image from local rhea source (P4-B autodeploy).

The orchestrator runs rhea-server as a Docker container (``apecx-rhea-server:local``).
Historically the image had to be pre-built out-of-band (``apecx-setup rhea`` /
``docker build``); a missing image surfaced a loud "go build it" message. This module
closes that gap: the orchestrator awaits :func:`ensure_rhea_image_built` (as the
container spec's ``image_builder`` hook) BEFORE ``docker run``, so the image is built
on demand from the operator's local rhea checkout — making rhea zero-config (no
``apecx-setup rhea`` step).

It is the direct analogue of the PyMOL self-provisioning tool: a thin wrapper over
``nanobrain.library.runtime.docker_image_builder.ensure_docker_image_built`` (idempotent +
concurrency-safe: a no-op when the image already exists, a real build when it does not).

FAIL-LOUD: if the rhea source checkout (the docker BUILD CONTEXT) cannot be located,
this raises a clear exception naming the cause — it does NOT silently degrade. A build
against a missing/broken source is exactly the silent-failure shape the orchestrator
exists to refuse.

Deferred / out-of-scope: a PUBLISHED image (e.g. a registry pull) for machines that have
NO local rhea source. Today autodeploy REQUIRES a local rhea checkout to build from; a
pull-if-no-source fallback is a follow-up.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

# The local image tag the orchestrator's rhea container spec runs. Honors the same
# ``APECX_RHEA_IMAGE`` override the orchestrator's ``_make_rhea_container_spec`` reads,
# with the same default, so the tag we BUILD is always the tag docker RUNs (a mismatch
# would be a silent failure: build succeeds, run pulls/fails on a different tag).
_RHEA_IMAGE_ENV = "APECX_RHEA_IMAGE"
_RHEA_IMAGE_DEFAULT = "apecx-rhea-server:local"


def resolve_rhea_image_tag() -> str:
    """The image tag we BUILD and the orchestrator RUNs — single source of truth.

    Both the build (``ensure_rhea_image_built``) and the run (``_make_rhea_container_spec``)
    resolve the tag HERE so the two can never drift (a mismatch would be a silent failure:
    build succeeds on tagA, docker run then pulls/fails on tagB).
    """
    return os.environ.get(_RHEA_IMAGE_ENV, _RHEA_IMAGE_DEFAULT)


def _resolve_rhea_repo() -> Path:
    """Locate the rhea source checkout (the docker build context). FAIL-LOUD if absent.

    Honors ``RHEA_REPO_PATH`` first (operator override + what ``autodiscover_rhea_env``
    sets at apecx-mcp startup), then falls back to the autodiscovery filesystem probe.
    """
    # Imported here (not at module top) to keep this module import-cheap and avoid any
    # import-order coupling with the autodiscovery module.
    from apecx_integration.infrastructure.rhea_env_autodiscovery import _find_rhea_repo

    repo_env = os.environ.get("RHEA_REPO_PATH", "").strip()
    repo = Path(repo_env) if repo_env else _find_rhea_repo()
    if repo is None or not repo.is_dir():
        raise FileNotFoundError(
            "Cannot build the rhea-server image: rhea source checkout not found. "
            "Set RHEA_REPO_PATH to your rhea checkout, or place it where autodiscovery "
            "probes (sibling <workspace>/rhea, ~/src/rhea, ~/code/rhea, "
            "~/Downloads/apecx-cowork/rhea, ...). "
            f"(RHEA_REPO_PATH={repo_env!r}; autodiscovery probe found: {_find_rhea_repo()})"
        )
    return repo


async def ensure_rhea_image_built(*, on_progress: Callable[[str], None] | None = None) -> str:
    """Build (idempotently) the rhea-server image from the local rhea checkout; return its tag.

    Locates the rhea source via :func:`_resolve_rhea_repo`, asserts ``<repo>/Dockerfile``
    exists, and delegates to ``ensure_docker_image_built`` (build context = the repo dir).
    Returns the image tag on success. Raises (FAIL-LOUD) when the source/Dockerfile is
    missing or the build fails — the orchestrator surfaces that as ``ERROR_STARTING``.

    ``on_progress`` (if given) receives each build-log line; the orchestrator wires it to
    ``log.info`` so a slow first-time build is observable, not a silent stall.
    """
    from nanobrain.library.runtime.docker_image_builder import ensure_docker_image_built

    repo = _resolve_rhea_repo()
    dockerfile = repo / "Dockerfile"
    if not dockerfile.is_file():
        raise FileNotFoundError(
            f"rhea source at {repo} has no Dockerfile (expected {dockerfile}); "
            "cannot build the rhea-server image."
        )

    image_tag = resolve_rhea_image_tag()
    log.info(
        "rhea-server image: ensuring %s is built from %s (context %s)",
        image_tag,
        dockerfile,
        repo,
    )
    await ensure_docker_image_built(
        dockerfile_path=str(dockerfile),
        build_context=str(repo),
        image_tag=image_tag,
        on_progress=on_progress,
    )
    return image_tag


__all__ = ["ensure_rhea_image_built", "resolve_rhea_image_tag"]
