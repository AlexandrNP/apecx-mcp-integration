"""LocalMafftAlignStep argv-shape unit test for the containerization-hardening fix
(container-timeout-no-orphan).

The fix pins the MAFFT container name via ``--name <container_name>`` so a subprocess timeout can
``docker kill`` the container by name instead of orphaning it (``--rm`` removes it only AFTER it
stops). ``_docker_run_mafft`` builds the argv THEN calls ``subprocess.run``; to pin the argv WITHOUT
Docker we monkeypatch ``subprocess.run`` in this module's namespace to CAPTURE the argv and return a
fake ``CompletedProcess``.

MOCK JUSTIFICATION (workspace mocks policy): this ``subprocess.run`` mock is a wiring/argv-shape
check only. The SAME real ``docker kill <name>`` kill-by-name path is exercised against a real
container by the integration test
``tests/integration/test_container_timeout_no_orphan.py`` (option ii — direct kill-primitive proof),
satisfying the unit-mock / integration-test parity rule. Mock-only coverage is forbidden.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from apecx_integration.composition.steps.local_mafft_align_step import LocalMafftAlignStep


def _stage(tmp_path: Path, **cfg) -> LocalMafftAlignStep:
    """Construct the step via from_config, mirroring tests/integration/test_local_mafft_align_step.py."""
    p = tmp_path / "mafft.yml"
    lines = ["name: mafft_test"] + [f"{k}: {v}" for k, v in cfg.items()]
    p.write_text("\n".join(lines) + "\n")
    return LocalMafftAlignStep.from_config(str(p))


def test_mafft_docker_argv_names_container(tmp_path, monkeypatch):
    """``_docker_run_mafft`` must build a ``docker run`` argv carrying ``--name <container_name>``
    (immediately after ``--rm``) so a timeout can kill the container by name. Capture the argv via a
    fake ``subprocess.run`` and assert its shape."""
    import apecx_integration.composition.steps.local_mafft_align_step as mod

    captured: dict[str, list[str]] = {}

    def _fake_run(argv, *args, **kwargs):
        captured["argv"] = argv
        # Return a plausible aligned-FASTA CompletedProcess so _run_mafft_container's post-checks
        # would pass if reached; this unit test only inspects the captured argv.
        return subprocess.CompletedProcess(argv, 0, stdout=">x\nAAA\n>y\nAAA\n", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    step = _stage(tmp_path)
    step._docker_run_mafft(tmp_path, "apecx-mafft-deadbeef")

    argv = captured["argv"]
    assert "--name" in argv, argv
    name_idx = argv.index("--name")
    assert argv[name_idx + 1] == "apecx-mafft-deadbeef", argv
    # Inserted immediately after --rm (mirrors the PyMOL container argv shape).
    assert "--rm" in argv, argv
    assert argv.index("--rm") + 1 == name_idx, argv
