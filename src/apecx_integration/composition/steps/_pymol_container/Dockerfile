# Containerized, headless, open-source PyMOL for the structural-reasoning stage (E2-P).
#
# Open-source PyMOL (pymol-open-source, conda-forge) run FULLY HEADLESS / in-process
# (pymol2.PyMOL()) — no GUI, no X server, no socket. This is the determinism +
# reproducibility anchor for the structural-reasoning stage: the PyMOL version is
# PINNED here (3.1.0) and recorded in every result; the stage shells out to this
# image with `docker run`, the host pre-fetches the immutable RCSB structure and
# mounts it in, so the container itself needs NO network.
#
# Build (one-time, ~5 min on first build; the conda solve dominates) — normally the
# image AUTO-BUILDS on first structural use (`pymol_sasa_tool.run_sasa` -> `ensure_image`);
# to build it manually from this packaged build context:
#   CTX=src/apecx_integration/composition/steps/_pymol_container
#   docker build -t apecx-pymol:3.1.0 -f $CTX/Dockerfile $CTX
#
# Verify headless PyMOL works (with the same hardening flags the stage uses):
#   docker run --rm --network none --cap-drop ALL \
#     --security-opt no-new-privileges:true --user $(id -u):$(id -g) \
#     apecx-pymol:3.1.0 python -c \
#     "import pymol2; p=pymol2.PyMOL(); p.start(); print(p.cmd.get_version()[0]); p.stop()"
#
# Provenance note: the per-residue SASA idiom (load -> remove solvent/hetatm -> pin
# dot_solvent=1/dot_density=3 -> cmd.get_area per selection) is standard open-source
# PyMOL practice; the agentic-pymol project
# (https://github.com/Arcadia-Science/agentic-pymol, MIT) uses the same get_area
# primitive. Its GUI-socket delivery model is deliberately NOT used — we run PyMOL
# headless/in-process inside this container instead.
FROM mambaorg/micromamba:1.5.8

# PIN the PyMOL version (determinism contract — recorded in every result.json).
ARG PYMOL_VER=3.1.0
RUN micromamba install -y -n base -c conda-forge \
      pymol-open-source=${PYMOL_VER} numpy \
    && micromamba clean -a -y

# Activate the base env for `docker run <image> python ...` invocations.
ENV MAMBA_DOCKERFILE_ACTIVATE=1
