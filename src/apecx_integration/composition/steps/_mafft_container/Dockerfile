# Containerized MAFFT for the sequence-conservation leg — SELF-PROVISIONING, uniform with the
# headless-PyMOL container (`_pymol_container/`). Built on first use by `ensure_docker_image_built`
# (the workflow auto-builds it, or `apecx-setup`), so NO host `brew install mafft` / `conda install
# mafft` is ever required; the conservation leg degrades-loud when Docker is absent.
#
# Determinism: the MAFFT version is PINNED by the Debian base release (bookworm → mafft 7.505), and
# recorded as the aligner_version in every result. MAFFT itself is deterministic for a given version
# + input, so the alignment is reproducible across machines that build this same image.
#
# Run contract: the host writes the unaligned FASTA to a bind-mounted /work and invokes
#   mafft <mode> [--amino] /work/input.fasta
# MAFFT writes the alignment to STDOUT (captured by the host) and progress to stderr. The container
# needs NO network and writes nothing back to /work (so it runs --network none --read-only-friendly).
FROM debian:bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends mafft \
 && rm -rf /var/lib/apt/lists/*

# No ENTRYPOINT: the host passes the full `mafft …` argv to `docker run`, exactly like the binary.
