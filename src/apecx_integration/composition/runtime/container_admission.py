"""Host-side admission control for per-run code-exec ``docker run`` spawns.

The apecx-mcp server is deployed as an open, unauthenticated endpoint (an explicit
product decision). That makes per-request container spawns a resource-exhaustion
vector: N concurrent anonymous callers each driving a workflow that spawns a PyMOL
(``structural_reasoning_step``, ~2 GB) or composition-sandbox (``docker_sandbox``)
container can pin the host's memory with no cap. The compose-service ``mem_limit`` /
``pids_limit`` / ``cpus`` in ``deploy/docker-compose.server.yml`` do NOT reach these
ad-hoc ``docker run`` containers — they are not compose services — so the cap has to
live here, in the process that issues the spawns.

This module bounds the number of code-exec containers running SIMULTANEOUSLY across
the whole process, via a single shared semaphore. Hold a slot for the container's
whole lifetime (spawn → ``communicate()`` returns / is killed), not just the spawn,
because the host RAM is consumed while the container runs::

    from apecx_integration.composition.runtime.container_admission import (
        acquire_container_slot,
    )

    async with acquire_container_slot():
        proc = await asyncio.create_subprocess_exec(*argv, ...)
        await proc.communicate()

The cap is count-based (not memory-aware): size ``APECX_MAX_CONCURRENT_DOCKER_RUNS``
so ``largest_container_MB * N`` stays within the host's RAM headroom (default 4 →
4 * 2 GB = 8 GB worst case). It is deliberately simple — the goal is to convert
"host OOM" into "container waits its turn", matching the deployment policy's P2
(minimal blast radius), not to perfectly account for heterogeneous container sizes.
"""

from __future__ import annotations

import asyncio
import os

#: Env var that overrides the default simultaneous-container cap.
ENV_VAR = "APECX_MAX_CONCURRENT_DOCKER_RUNS"
_DEFAULT_MAX = 4

# ``asyncio.Semaphore`` binds to the event loop that is running when it is created;
# a module-import-time instance would bind to the wrong loop (or none). So create it
# lazily on first use and rebind if the running loop changes (mirrors nanobrain's
# ``Workflow._get_run_lock`` per-loop-lazy pattern).
#
# ASSUMPTION: the cap binds across spawns that share ONE event loop — which is the
# deployment topology (a single ``apecx-mcp`` process with one persistent loop). Spawns
# driven on *separate* loops (e.g. a per-thread ``asyncio.run``) would each get their own
# semaphore and the cap would not bind across them. Not a concern for the single-loop
# server; called out so a future multi-loop driver does not silently lose the cap.
_semaphore: asyncio.Semaphore | None = None
_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _max_slots() -> int:
    """Resolve the cap from the env var (fail-loud on garbage / non-positive)."""
    raw = os.environ.get(ENV_VAR)
    if raw is None:
        return _DEFAULT_MAX
    value = int(raw)  # ValueError on non-int — fail loud, do not silently default
    if value < 1:
        raise ValueError(f"{ENV_VAR} must be >= 1, got {value}")
    return value


def acquire_container_slot() -> asyncio.Semaphore:
    """Return the process-wide code-exec-container semaphore for ``async with``.

    ``async with acquire_container_slot():`` blocks until a slot is free, holds it
    for the body, and releases on exit (including on exception). Must be called from
    within a running event loop.
    """
    global _semaphore, _semaphore_loop
    loop = asyncio.get_running_loop()
    if _semaphore is None or _semaphore_loop is not loop:
        _semaphore = asyncio.Semaphore(_max_slots())
        _semaphore_loop = loop
    return _semaphore


def _reset_for_test() -> None:
    """Drop the cached semaphore so the next acquire re-reads ``ENV_VAR``. Tests only."""
    global _semaphore, _semaphore_loop
    _semaphore = None
    _semaphore_loop = None
