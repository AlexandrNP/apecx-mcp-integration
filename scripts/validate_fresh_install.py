#!/usr/bin/env python3
"""Fresh-install (packaging / delivery) validation for the self-refining loop.

WHY THIS EXISTS
---------------
The boundary loop (``scripts/validate_workflow_boundary.py``) runs ``PYTHONPATH=src`` — the
EDITABLE source tree — so every packaged-resource path resolves and a wheel-delivery gap is
INVISIBLE. It validates the product's behavior, never its delivery. That blind spot shipped a real
bug: the PyMOL build context (``docker/pymol/_pymol_job.py``) lived outside ``src/`` and was absent
from the wheel, so a ``uv tool install`` user hit ``FileNotFoundError`` while every dev test passed.

This harness validates the DELIVERED package: it builds the wheel and asserts the load-bearing,
module-relative resources are actually inside it AND resolve in the install layout — the exact
class of bug the editable-dev loop cannot see. In ``--full`` mode it does a real ``uv tool install``
into an isolated dir, runs ``apecx-setup --non-interactive``, and drives the C1-C6 boundary contract
from the DELIVERED venv (not ``PYTHONPATH=src``).

Tiers:
  default (deterministic, no network — CI-safe):
    1. build the wheel (``pip wheel --no-deps``)
    2. DELIVERY MANIFEST: assert each load-bearing resource is inside the wheel
    3. INSTALL-LAYOUT: extract the wheel; assert every module-relative resource resolves on disk
       (the PyMOL-class runtime check, done structurally so it needs no deps)
    4. ENTRY POINTS: assert the console scripts (apecx-mcp / apecx-setup / …) are declared
    5. PACKAGE-DATA GLOBS: assert each pyproject package-data glob matches >=1 file in the wheel
  --full (env-gated, heavy: deps + ~735MB dict + live APIs):
    6. ``uv tool install`` the local checkout into an isolated UV_TOOL_DIR
    7. ``apecx-mcp --help`` / ``apecx-setup --help`` resolve from it
    8. ``apecx-setup --non-interactive`` exits cleanly (optionals = honest skips)
    9. run ``validate_workflow_boundary.py`` with the DELIVERED venv's python

Usage:
  .venv/bin/python scripts/validate_fresh_install.py            # deterministic tiers (1-5)
  .venv/bin/python scripts/validate_fresh_install.py --full     # + real install + e2e (6-9)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from fnmatch import fnmatch
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PKG = "apecx_integration"
_E2E = False  # set by --e2e: run the heavy boundary workflow from the delivered venv

# Load-bearing resources resolved at runtime via ``Path(__file__).parent / <rel>`` (or an absolute
# install path). Each MUST be inside the wheel; a missing one is a runtime FileNotFoundError on a
# non-editable install. Paths are relative to the package root (``apecx_integration/``). This is the
# generalization of the PyMOL fix from "one file" to "the delivery set".
RESOURCE_MANIFEST: list[tuple[str, str]] = [
    ("PyMOL job script (SASA container)", "composition/steps/_pymol_container/_pymol_job.py"),
    ("PyMOL Dockerfile (build context)", "composition/steps/_pymol_container/Dockerfile"),
    ("PyMOL SASA helper (copied into container)", "composition/steps/_pymol_sasa.py"),
    ("composer config", "composition/composer_config.yml"),
]
# Resource TREES that must ship at least one file (globbed; a missing tree => a runtime load error).
RESOURCE_TREES: list[tuple[str, str]] = [
    ("alembic migration versions", "_alembic/migrations/versions/"),
    ("viral_epitope_analysis workflow", "composition/workflows/viral_epitope_analysis/"),
]
# Console scripts the README + operators rely on (pyproject [project.scripts]).
REQUIRED_ENTRY_POINTS = {"apecx-mcp", "apecx-cp", "apecx-setup", "apecx-globus-setup"}

# Files under the package that intentionally do NOT ship (dev/build-time only). The DEFAULT is "every
# source resource ships" (so a new runtime resource is caught prospectively); this small, EXPLICIT,
# justified set is the only exception list. Add here ONLY a file that is never loaded at runtime.
_DEV_ONLY_PATTERNS = {
    "*.example",  # config templates (e.g. .env.example) — documentation, never read at runtime
}


def _ok(msg: str) -> None:
    print(f"  ✅ {msg}", flush=True)


def _fail(msg: str) -> None:
    print(f"  ❌ {msg}", flush=True)


def build_wheel(workdir: Path) -> Path:
    print("\n== build the wheel (pip wheel --no-deps) ==", flush=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            str(_REPO_ROOT),
            "-w",
            str(workdir),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
    )
    wheels = sorted(workdir.glob("apecx_integration-*.whl"))
    if not wheels:
        raise SystemExit("FATAL: pip wheel produced no apecx_integration-*.whl")
    print(f"  built {wheels[0].name}", flush=True)
    return wheels[0]


def _package_data_globs() -> list[str]:
    raw = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    return list(raw["tool"]["setuptools"]["package-data"][_PKG])


def check_delivery(wheel: Path) -> list[str]:
    """Tiers 2 + 5: the manifest + every package-data glob are present in the wheel."""
    print("\n== delivery manifest (resources inside the wheel) ==", flush=True)
    names = zipfile.ZipFile(wheel).namelist()
    pkg_names = [n for n in names if n.startswith(f"{_PKG}/")]
    rel = {n[len(_PKG) + 1 :] for n in pkg_names}
    failures: list[str] = []

    for label, path in RESOURCE_MANIFEST:
        if path in rel:
            _ok(f"{label}: {path}")
        else:
            _fail(f"MISSING from wheel: {label} ({path})")
            failures.append(f"manifest:{path}")

    for label, tree in RESOURCE_TREES:
        if any(r.startswith(tree) and not r.endswith("/") for r in rel):
            _ok(f"{label}: {tree}* present")
        else:
            _fail(f"MISSING tree from wheel: {label} ({tree})")
            failures.append(f"tree:{tree}")

    # A glob that matches >=1 file confirms it is doing its job; a glob matching NOTHING is only a
    # WARNING (it may be intentional future-proofing — e.g. ``**/*.yaml`` while the repo uses ``.yml``
    # exclusively — so it is not a delivery failure). The manifest + layout checks are the hard gates.
    print("\n== package-data globs each match >=1 wheel file ==", flush=True)
    for glob in _package_data_globs():
        if any(_glob_match(r, glob) for r in rel):
            _ok(f"glob {glob!r}: matched")
        else:
            print(
                f"  ⚠️  glob {glob!r}: matched nothing (dead or future-proofing — not a failure)",
                flush=True,
            )
    return failures


def _glob_match(rel_path: str, glob: str) -> bool:
    parts = Path(rel_path).parts
    gparts = Path(glob).parts

    def _m(g: tuple[str, ...], p: tuple[str, ...]) -> bool:
        if not g:
            return not p
        if g[0] == "**":
            return _m(g[1:], p) or (bool(p) and _m(g, p[1:]))
        if not p or not fnmatch(p[0], g[0]):
            return False
        return _m(g[1:], p[1:])

    return _m(gparts, parts)


def check_all_resources_ship(wheel: Path) -> list[str]:
    """DERIVED delivery gate — the real fix for false confidence. Every non-.py resource under the
    package MUST be in the wheel, unless explicitly denylisted as dev-only. This catches ANY
    un-globbed runtime resource PROSPECTIVELY (seqtest.fasta, a future .json fixture, …) — not just
    the handful someone thought to hand-list. A hand-maintained manifest can only catch a REGRESSION
    of a resource already listed; this derives the contract from the source tree itself.

    Two known boundaries (both SAFE — they fail loud / never give false confidence):
      - it scans the LIVE source tree, so a runtime-written artifact left under ``src/`` would
        FALSE-FAIL (demand it ship). Keep ``src/`` free of generated files; extend the denylist if a
        generated-in-tree path ever becomes legitimate.
      - ``.py`` resources are skipped (setuptools auto-ships package ``.py`` as modules); a ``.py``
        used as DATA (e.g. ``_pymol_job.py``) is covered instead by the manifest spotlight + the
        install-layout check below, not by this derived gate."""
    print("\n== DERIVED delivery: every source resource ships in the wheel ==", flush=True)
    src_pkg = _REPO_ROOT / "src" / _PKG
    names = set(zipfile.ZipFile(wheel).namelist())
    failures: list[str] = []
    n_ok = 0
    for f in sorted(src_pkg.rglob("*")):
        if not f.is_file() or f.suffix == ".py" or "__pycache__" in f.parts:
            continue
        rel = f.relative_to(src_pkg).as_posix()
        if any(fnmatch(rel, p) or fnmatch(f.name, p) for p in _DEV_ONLY_PATTERNS):
            continue
        if f"{_PKG}/{rel}" in names:
            n_ok += 1
        else:
            _fail(f"source resource ABSENT from wheel (add a package-data glob): {rel}")
            failures.append(f"unshipped:{rel}")
    if not failures:
        _ok(
            f"all {n_ok} non-.py source resources ship (dev-only excepted: {sorted(_DEV_ONLY_PATTERNS)})"
        )
    return failures


def check_install_layout(wheel: Path, workdir: Path) -> list[str]:
    """Tier 3: extract the wheel and assert each module-relative resource resolves on disk.

    Reproduces ``Path(<module>).parent / <rel>`` resolution structurally (no import / no deps): a
    resource present in the wheel but at the WRONG relative location would still fail here."""
    print("\n== install-layout resolution (extracted wheel) ==", flush=True)
    dest = workdir / "extracted"
    if dest.exists():
        shutil.rmtree(dest)
    zipfile.ZipFile(wheel).extractall(dest)
    pkg_dir = dest / _PKG
    failures: list[str] = []
    for label, path in RESOURCE_MANIFEST:
        if (pkg_dir / path).is_file():
            _ok(f"resolves: {path}")
        else:
            _fail(f"does NOT resolve in install layout: {label} ({path})")
            failures.append(f"layout:{path}")
    return failures


def check_entry_points(wheel: Path) -> list[str]:
    """Tier 4: the console scripts are declared in the wheel's entry_points.txt."""
    print("\n== console-script entry points ==", flush=True)
    zf = zipfile.ZipFile(wheel)
    ep_files = [n for n in zf.namelist() if n.endswith("entry_points.txt")]
    declared: set[str] = set()
    for ep in ep_files:
        for line in zf.read(ep).decode().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("["):
                declared.add(line.split("=", 1)[0].strip())
    failures: list[str] = []
    for name in sorted(REQUIRED_ENTRY_POINTS):
        if name in declared:
            _ok(f"entry point: {name}")
        else:
            _fail(f"MISSING entry point: {name}")
            failures.append(f"entrypoint:{name}")
    return failures


def run_full(workdir: Path) -> list[str]:
    """Tiers 6-9: real uv tool install + apecx-setup --non-interactive + boundary e2e.

    Heavy + env-dependent (deps over the network, ~735MB dict, live APIs). Each sub-step is a
    finding, not a hard crash, so the loop records WHAT broke in a real install."""
    print("\n== FULL: real fresh install (uv tool) ==", flush=True)
    findings: list[str] = []
    if shutil.which("uv") is None:
        findings.append("full:uv-not-on-PATH (cannot reproduce the README `uv tool install`)")
        _fail("uv not found — install it to run the full fresh-install tier")
        return findings
    tool_dir = workdir / "uv_tool"
    # Strip ALL APECX_* from the child env so the "fresh install" does NOT inherit the dev shell's
    # APECX_DATA_ROOT / APECX_SYNONYM_DICT_PATH / APECX_LLM_BASE_URL — otherwise a developer with
    # local data set would silently mask the very no-local-data clean-install scenario this validates.
    base = {k: v for k, v in os.environ.items() if not k.startswith("APECX_")}
    env = {**base, "UV_TOOL_DIR": str(tool_dir), "UV_TOOL_BIN_DIR": str(workdir / "bin")}
    proc = subprocess.run(
        [
            "uv",
            "tool",
            "install",
            "--python",
            "3.12",
            "--from",
            str(_REPO_ROOT),
            _PKG.replace("_", "-"),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
    )
    if proc.returncode != 0:
        findings.append("full:uv-tool-install-failed")
        _fail(f"uv tool install failed:\n{proc.stderr[-2000:]}")
        return findings
    _ok("uv tool install succeeded")
    bindir = workdir / "bin"
    for script in ("apecx-mcp", "apecx-setup"):
        exe = bindir / script
        r = subprocess.run([str(exe), "--help"], env=env, capture_output=True, text=True)
        (_ok if r.returncode == 0 else _fail)(f"{script} --help (rc={r.returncode})")
        if r.returncode != 0:
            findings.append(f"full:{script}-help-rc{r.returncode}")
    # Headless setup — skip the 735MB dict download; optionals should honest-skip, not crash.
    setup_env = {**env, "APECX_SKIP_DICT_BUILD": "1"}
    r = subprocess.run(
        [str(bindir / "apecx-setup"), "--non-interactive"],
        env=setup_env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    (_ok if r.returncode == 0 else _fail)(f"apecx-setup --non-interactive (rc={r.returncode})")
    if r.returncode != 0:
        findings.append(f"full:apecx-setup-rc{r.returncode}")
    print(r.stdout[-2000:], flush=True)

    # Step 9 — END-TO-END: run the C1-C6 boundary contract from the DELIVERED venv (NOT PYTHONPATH=src),
    # so the real INSTALLED package serves a real query. This is where a runtime delivery/tooling gap
    # surfaces (e.g. a bio tool that isn't self-provisioning). Heavy (dict + live APIs); recorded as a
    # finding, not a hard gate. Gated on --e2e (and a present dict) since it needs network + ~735MB dict.
    if not _E2E:
        print(
            "  (skipping the boundary e2e — pass --e2e to run the workflow from the delivered venv)",
            flush=True,
        )
        return findings
    delivered = sorted(tool_dir.glob("*/bin/python"))
    boundary = _REPO_ROOT / "scripts" / "validate_workflow_boundary.py"
    if not delivered or not boundary.is_file():
        findings.append("full:e2e-skipped (no delivered python / boundary script)")
        return findings
    print(
        f"\n== FULL e2e: boundary contract from the delivered venv ({delivered[0]}) ==", flush=True
    )
    e2e = subprocess.run(
        [str(delivered[0]), str(boundary), "influenza/HA"],
        env={**env},
        capture_output=True,
        text=True,
        timeout=1200,
    )
    print(e2e.stdout[-4000:], flush=True)
    if e2e.returncode != 0:
        findings.append(f"full:boundary-e2e-rc{e2e.returncode}")
        _fail(f"boundary e2e from delivered venv (rc={e2e.returncode})\n{e2e.stderr[-1500:]}")
    else:
        _ok("boundary e2e ran from the delivered venv (inspect the scorecard above for leg health)")
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else None)
    ap.add_argument(
        "--full",
        action="store_true",
        help="also run the real uv-tool-install + apecx-setup tier (heavy: deps over network).",
    )
    ap.add_argument(
        "--e2e",
        action="store_true",
        help="within --full, also run the boundary workflow from the delivered venv "
        "(very heavy: ~735MB dict + live APIs).",
    )
    args = ap.parse_args()
    global _E2E
    _E2E = args.e2e

    workdir = Path(tempfile.mkdtemp(prefix="apecx-fresh-install-"))
    print(f"workdir: {workdir}", flush=True)
    failures: list[str] = []
    try:
        wheel = build_wheel(workdir)
        failures += check_all_resources_ship(
            wheel
        )  # DERIVED gate (catches the class prospectively)
        failures += check_delivery(wheel)  # curated spotlight on load-bearing files + glob warnings
        failures += check_install_layout(wheel, workdir)  # load-bearing paths RESOLVE in the layout
        failures += check_entry_points(wheel)
        if args.full:
            failures += run_full(workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n===== FRESH-INSTALL SCORECARD =====", flush=True)
    if failures:
        print(f"  {len(failures)} delivery/packaging failure(s):", flush=True)
        for f in failures:
            print(f"    - {f}", flush=True)
        return 1
    print(
        "  ALL CLEAR — every non-.py source resource ships in the wheel, the load-bearing paths "
        "resolve in the install layout, and every entry point is declared.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
