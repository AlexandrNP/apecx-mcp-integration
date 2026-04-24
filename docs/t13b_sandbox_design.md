# T13b — runtime Docker sandbox design

**Status:** scaffold-level, 2026-04-24. Runtime verification is
operator-deferred (see "Verification" below).

## Why this exists

T13 Phase-1 (`src/apecx_integration/composition/sandbox.py`) ships
a **static import-whitelist scanner** that inspects LLM-generated
novel Python before it is loaded. The scanner refuses code whose
imports are off-whitelist, and it blocks `exec` / `eval` / `compile`
/ `importlib.import_module` / `__import__` regardless of whitelist.

The scanner's own docstring explicitly names what it does **not**
catch:

- `getattr(module, 'forbidden_func')` on a whitelisted module.
- Side-effects of whitelisted modules' transitive imports.
- **Any runtime behavior whatsoever** — "if whitelisted code
  decides to shell out via subprocess, it will."

T13b is the runtime isolation layer that backstops the scanner.
Once T13b is the runtime executor for novel Python, an escape from
the scanner is contained to the sandbox and cannot touch the host
filesystem, network, or Tier-2 Python process.

## Threat model

**In scope — must be prevented:**

1. **Filesystem read** outside of the explicitly-mounted read-only
   input directory. The whitelisted module might try to
   `open("/etc/passwd")`, `open("~/.ssh/id_rsa")`, or walk the
   user's home directory.
2. **Filesystem write** of any kind outside of an explicit writable
   scratch tmpfs. No persistent state, no dropping files that might
   be picked up by another process.
3. **Network access** of any kind. No DNS, no HTTP, no raw sockets.
4. **Process escape** via privilege escalation (`setuid` binaries,
   capability abuse, kernel exploits reachable via syscalls that
   aren't strictly necessary).
5. **Resource exhaustion** — CPU burn, memory balloon, fork-bomb,
   PID exhaustion, disk fill. All must be bounded.
6. **Lateral movement** — the container must not be able to see or
   signal other containers on the same host (includes the
   postgres control-plane container).

**Out of scope — accepted residual risk:**

1. **CPU timing side-channels.** A malicious novel artifact that
   tries to read secrets via Spectre/Meltdown-class attacks is not
   the adversary we're modeling. The adversary is an LLM that
   produces obviously-bad Python, not a targeted APT.
2. **Rowhammer / hardware attacks.** Same reason.
3. **Container escape via a Docker 0-day.** A CVE in the container
   runtime itself is treated as an infrastructure problem, not a
   sandbox problem. Deploy-side mitigation: keep Docker patched.

## Isolation primitives (`docker run` flags)

Each flag and why it's load-bearing:

| Flag | Effect | Threat mitigated |
|---|---|---|
| `--network=none` | No network namespace peer, no interfaces except loopback. No DNS, no IP. | (3) |
| `--read-only` | Root filesystem mounted read-only. | (2) |
| `--tmpfs /tmp:size=256m,mode=1777` | Writable scratch on tmpfs, bounded, evaporates on exit. | (2), (5) |
| `--user 65534:65534` | Runs as `nobody:nogroup`. No root inside container. | (4) |
| `--cap-drop=ALL` | Drops every Linux capability. No `CAP_SYS_ADMIN`, no `CAP_NET_RAW`, nothing. | (4) |
| `--security-opt=no-new-privileges:true` | `setuid` binaries cannot gain privileges via exec. | (4) |
| `--security-opt=seccomp=default` | Docker's default seccomp profile (blocks `ptrace`, `mount`, `unshare`, etc.). | (4) |
| `--memory=512m`, `--memory-swap=512m` | Cgroup memory cap. `--memory-swap=--memory` disables swap so the cap is real. | (5) |
| `--cpus=1.0` | CPU quota. | (5) |
| `--pids-limit=256` | Fork-bomb cap. | (5) |
| `--mount type=bind,source=…,target=/work,readonly` | Input code mounted read-only. No writable bind. | (1), (2) |
| `--rm` | Container removed on exit. No lingering layer diff. | (2), (6) |
| `--workdir /work` | Starts in the read-only input dir. Any write attempt there fails. | (2) |

### What is NOT mounted

- No host `/` bind, no `/home`, no `/etc`, no `/var/run/docker.sock`
  (the sock would mean container-from-container, full escape).
- No host-uid/gid mapping — `--user 65534:65534` is the only identity
  inside the container.
- No secrets: not via `--env`, not via `--env-file`. If novel Python
  needs secret material (it shouldn't, for a compute step), wrap the
  secret delivery as an encrypted input artifact and unwrap inside
  the container.

## Input / output contract

The sandbox is a **pure compute** runtime. In, out, done.

**Input** — the caller provides a **host directory** with the novel
Python source file plus any data artifacts the code will read. That
directory is bind-mounted read-only at `/work` inside the container.

**Output** — the container writes to `/tmp` (writable tmpfs) during
execution. Before the container exits, it serializes its output to
`/tmp/result.json` (or similar); the sandbox runner reads that
serialized form off the container's filesystem **before** the
container is removed.

**Caveat (honest):** reading `/tmp` from a `--rm` container after it
has exited is not directly possible — the tmpfs has evaporated. Two
options, and the scaffold does not commit to one yet:

- **(a)** Capture the container's stdout/stderr and use that as the
  output channel. Simple; limits output to what fits in a pipe.
- **(b)** Bind-mount a second writable host directory (not tmpfs) at
  `/out`, and have the container write results there. Keeps tmpfs
  for ephemeral scratch, separates "result" from "scratch." More
  complex, more correct.

Phase-3 will commit to one; for the scaffold, the runner exposes
`stdout` / `stderr` on `SandboxResult` (option a) and leaves a
TODO for the `/out` path (option b).

## API surface (scaffold)

```python
from apecx_integration.composition.docker_sandbox import (
    DockerSandboxRunner,
    SandboxConfig,
    SandboxResult,
    SandboxNotAvailableError,
    build_docker_sandbox_command,
)
```

- `SandboxConfig` — frozen dataclass of the tunable isolation
  parameters (image, memory, cpus, pids limit, timeout, tmpfs size,
  user, workdir).
- `build_docker_sandbox_command(image, command, *, input_host_path,
  container_name, config) -> list[str]` — pure function, no I/O.
  Returns the argv. This is the unit-testable surface.
- `DockerSandboxRunner` — wraps `build_docker_sandbox_command` with
  an `async run(command, *, input_host_path) -> SandboxResult`
  method that `subprocess.run`s the argv with a timeout. Refuses to
  run unless `APECX_T13B_SANDBOX_EXECUTE=1` is set, to prevent
  accidental Docker invocation in CI.
- `SandboxResult` — exit_code, stdout, stderr, duration_seconds,
  killed_by_timeout.
- `SandboxNotAvailableError` — raised when Docker isn't on PATH or
  the execute-gate env var is not set.

## Composer integration (Phase 3, NOT this scaffold)

Today the composer's static scanner is wired in
`src/apecx_integration/composition/composer.py`. The planned
integration:

1. Composer emits novel Python source + inputs bundle.
2. Static scanner (T13) runs; violations reject the composition.
3. If accepted, the source + inputs bundle is written to a host
   directory.
4. `DockerSandboxRunner.run(...)` executes the compute step inside
   the container.
5. The serialized result is returned up the composer's call chain.

Step 4 is what T13b **enables**; it is not activated in this
commit. Wiring the composer to actually USE the runner is
Phase-3 work with its own AC set.

## Verification

**What's in this commit:**

- `tests/unit/test_docker_sandbox_command.py` — asserts every
  hardening flag is present in the constructed argv. Runs on any
  machine, no Docker required. This is how the threat-model table
  above is pinned — if someone drops `--cap-drop=ALL`, the unit
  test goes red.
- `tests/integration/test_docker_sandbox_runtime.py` — opt-in via
  `APECX_T13B_SANDBOX_EXECUTE=1`. Runs a trivial python command
  inside the container and asserts exit-code + captured stdout.

**What's NOT in this commit (operator-deferred):**

- A **runtime escape-attempt** test: a test that tries to open a
  TCP socket inside the container and asserts it fails, tries to
  `open("/etc/passwd")` and asserts it fails, etc. These are
  valuable but require a live Docker daemon on the test host and
  escape policy that may differ across Docker versions. Ship
  when Phase-3 wires the composer.
- **Image pin by digest.** The default image name in the scaffold
  is a tag (`python:3.12-slim`); before production use, switch to
  a SHA256 digest pin so builds are reproducible.
- **Container-name UUID minting and kill-on-cancel.** The scaffold
  uses `--rm` but doesn't currently name containers uniquely or
  implement cancellation via `docker kill`. Phase-3 concern.

## Open design questions (for Phase 3)

- Should the sandbox use `gVisor` (`--runtime=runsc`) instead of /
  in addition to the default `runc`? `gVisor` provides a user-space
  kernel that meaningfully raises the bar on kernel-surface attacks,
  at ~10% CPU overhead. Tradeoff is operational (requires gVisor
  installed on the host).
- Should we require signed images? If so, how are we distributing
  the key?
- What's the image-refresh policy? Pinned digests are reproducible
  but accumulate CVEs. A scheduled re-pin + CI security scan is
  the standard answer.

These do not block the scaffold; they block Phase-3 rollout.
