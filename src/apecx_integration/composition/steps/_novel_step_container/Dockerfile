# Sandbox image for executing composer "novel Python" steps (#1c). Self-provisioned like the PyMOL
# image. The harness (_novel_step_job.py) is baked in at /app; a run mounts job.json read-only at
# /work and an empty writable /out, then (under docker_sandbox's hardened flags) runs:
#   python /app/_novel_step_job.py /work/job.json /out/result.json
#
# Contents are the MINIMUM a novel BaseStep may import per composition/import_whitelist.txt: nanobrain
# (BaseStep/StepConfig live in nanobrain.core.step) + numpy + pandas. Pinned to the SAME nanobrain ref
# as pyproject.toml so the sandbox runs the framework the host composed against.
FROM python:3.12-slim

# git is only needed to pip-install nanobrain from its git ref; purged afterwards to keep the image
# lean + reduce attack surface.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && pip install --no-cache-dir \
      "nanobrain @ git+https://github.com/AlexandrNP/nanobrain.git@academy-integration" \
      numpy \
      pandas \
 && apt-get purge -y git \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/* /root/.cache/pip

COPY _novel_step_job.py /app/_novel_step_job.py

# Run unprivileged (matches docker_sandbox.SandboxConfig.user default 65534:65534). site-packages is
# root-owned + world-readable; /tmp (tmpfs) and /out (bind mount) are the only writable paths at run
# time (the rest of the fs is --read-only via the sandbox flags).
USER 65534:65534
