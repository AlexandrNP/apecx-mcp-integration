"""Content-addressed artifact store (T11).

Every LLM-generated YAML / novel Python (and any other persisted blob)
lands here. The store writes the content to disk under a
content-addressed path, inserts an ``Artifact`` row, and — for
generated kinds — an accompanying ``GeneratedArtifact`` row that
pins the generation provenance (prompt, LLM model, library version,
composition summary). A WORKFLOW_GENERATED provenance event is
emitted under the owning run's hash chain.

## Invariants (T11 AP §5.11)

- **Append-only.** There is no ``delete(artifact_id)`` method on this
  class, and no HTTP route removes artifact rows. Regenerating the
  same workflow creates a new ``GeneratedArtifact`` row with a new UUID
  — it does not replace the previous one. UX should name the action
  "Regenerate (creates a new artifact; does not replace existing)".
- **Content-addressed for verification, not dedup.** ``sha256(content)``
  is stored on every Artifact so tampering is detectable. Two stores
  of identical bytes create two separate Artifact rows (AC1) — the
  audit trail wants distinct rows per generation event, not a shared
  row. On-disk file naming uses the Artifact UUID, not the content
  hash, so each row owns its file.
- **Optional git integration.** If ``GENERATED_ARTIFACTS_REPO_PATH``
  is set to a writable git checkout, the store also writes the content
  to that repo and commits with a meaningful message. Failures in the
  git step raise — they are not silent, because a half-commit is
  worse than no commit.

## API shape

The composer (Phase 2, not yet landed) calls::

    store.store(
        content=yaml_bytes,
        kind=ArtifactKind.GENERATED_WORKFLOW,
        run_id=run_id,
        mime_type="application/yaml",
        # Generated-only metadata (required when kind starts with GENERATED_):
        source_prompt=prompt_text,
        library_version=lib_ver,
        llm_model="claude-opus-4-7",
        llm_model_version_hash=model_hash,
        composition_summary={"steps_reused": 3, "steps_generated": 1},
        parent_artifact_id=None,
    )

Non-generated kinds (INPUT / INTERMEDIATE / OUTPUT) skip the
GeneratedArtifact-specific metadata.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from apecx_integration.control_plane.models.entities import (
    Artifact as ArtifactORM,
)
from apecx_integration.control_plane.models.entities import (
    GeneratedArtifact as GeneratedArtifactORM,
)
from apecx_integration.control_plane.provenance.recorder import ProvenanceRecorder
from apecx_integration.control_plane.schemas.enums import (
    ArtifactKind,
    ProvenanceEventType,
)

log = logging.getLogger(__name__)

GENERATED_KINDS = frozenset(
    {ArtifactKind.GENERATED_WORKFLOW, ArtifactKind.GENERATED_PYTHON}
)


class ArtifactNotFound(LookupError):
    """Raised by :meth:`ArtifactStore.load_content` when the artifact
    row exists but its on-disk file has been externally removed.

    AP §5.11 says artifacts are append-only — regenerate creates a new
    artifact, it does not replace or delete. If the file is missing,
    someone bypassed the API and manually deleted it; failing loudly is
    the right move.
    """


@dataclass(frozen=True, kw_only=True)
class GenerationMetadata:
    source_prompt: str
    library_version: str
    llm_model: str
    llm_model_version_hash: str
    composition_summary: dict[str, Any] = field(default_factory=dict)
    parent_artifact_id: UUID | None = None


class ArtifactStore:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        recorder: ProvenanceRecorder,
        *,
        root: Path | None = None,
        git_repo_env: str = "GENERATED_ARTIFACTS_REPO_PATH",
    ) -> None:
        self._session_factory = session_factory
        self._recorder = recorder
        self._root = root or Path.home() / ".apecx_cp" / "artifacts"
        self._git_repo_env = git_repo_env
        self._root.mkdir(parents=True, exist_ok=True)

    def store(
        self,
        *,
        content: bytes,
        kind: ArtifactKind,
        run_id: UUID,
        mime_type: str,
        step_id: UUID | None = None,
        generated_metadata: GenerationMetadata | None = None,
    ) -> ArtifactORM:
        if kind in GENERATED_KINDS and generated_metadata is None:
            raise ValueError(
                f"kind={kind.value} requires generated_metadata (prompt, "
                "library_version, llm_model, llm_model_version_hash)"
            )
        if kind not in GENERATED_KINDS and generated_metadata is not None:
            raise ValueError(
                f"kind={kind.value} does not accept generated_metadata; "
                "that field is only meaningful for GENERATED_* kinds"
            )

        content_hash = hashlib.sha256(content).hexdigest()
        artifact_id = uuid4()
        on_disk = self._root / str(artifact_id)
        on_disk.write_bytes(content)

        with self._session_factory() as session:
            artifact = ArtifactORM(
                id=artifact_id,
                run_id=run_id,
                step_id=step_id,
                kind=kind,
                location=str(on_disk),
                content_hash=content_hash,
                size_bytes=len(content),
                mime_type=mime_type,
                created_at=datetime.now(timezone.utc),
            )
            session.add(artifact)

            if generated_metadata is not None:
                session.add(
                    GeneratedArtifactORM(
                        artifact_id=artifact_id,
                        source_prompt=generated_metadata.source_prompt,
                        library_version=generated_metadata.library_version,
                        llm_model=generated_metadata.llm_model,
                        llm_model_version_hash=generated_metadata.llm_model_version_hash,
                        composition_summary=generated_metadata.composition_summary,
                        parent_artifact_id=generated_metadata.parent_artifact_id,
                    )
                )

            session.commit()
            session.refresh(artifact)

        if kind in GENERATED_KINDS:
            self._recorder.record(
                run_id=run_id,
                event_type=ProvenanceEventType.WORKFLOW_GENERATED,
                actor="composer",
                payload={
                    "artifact_id": str(artifact_id),
                    "content_hash": content_hash,
                    "kind": kind.value,
                    "llm_model": (
                        generated_metadata.llm_model
                        if generated_metadata
                        else "unknown"
                    ),
                    "size_bytes": len(content),
                },
            )
            self._maybe_git_commit(artifact_id, kind, content, generated_metadata)

        return artifact

    def load_content(self, artifact_id: UUID) -> bytes:
        """Read the on-disk bytes for an artifact. Verifies the SHA-256
        matches what the row claims — raises on tamper.
        """
        with self._session_factory() as session:
            artifact = session.get(ArtifactORM, artifact_id)
            if artifact is None:
                raise ArtifactNotFound(
                    f"No Artifact row for id={artifact_id}; either never "
                    "created or someone bypassed the API"
                )
            path = Path(artifact.location)
            if not path.is_file():
                raise ArtifactNotFound(
                    f"Artifact {artifact_id} row exists but content file "
                    f"{path} is gone — artifacts are append-only; "
                    "regenerate to create a new one"
                )
            content = path.read_bytes()
            expected = artifact.content_hash
        actual = hashlib.sha256(content).hexdigest()
        if actual != expected:
            raise ValueError(
                f"Artifact {artifact_id} content_hash mismatch: row claims "
                f"{expected[:16]}..., file hashes {actual[:16]}..."
            )
        return content

    # ---- Optional git integration ----------------------------------------

    def _maybe_git_commit(
        self,
        artifact_id: UUID,
        kind: ArtifactKind,
        content: bytes,
        metadata: GenerationMetadata | None,
    ) -> None:
        repo_path_str = os.environ.get(self._git_repo_env)
        if not repo_path_str:
            return
        repo_path = Path(repo_path_str).resolve()
        if not (repo_path / ".git").is_dir():
            raise RuntimeError(
                f"{self._git_repo_env}={repo_path} exists but is not a git "
                "working tree (no .git directory). Set to a real checkout "
                "or unset the env var."
            )

        suffix = ".yml" if kind is ArtifactKind.GENERATED_WORKFLOW else ".py"
        target = repo_path / f"{artifact_id}{suffix}"
        target.write_bytes(content)

        llm = metadata.llm_model if metadata else "unknown"
        summary = (
            metadata.composition_summary.get("summary_sentence")
            if metadata and isinstance(metadata.composition_summary, dict)
            else None
        )
        msg = (
            f"apecx: {kind.value} {artifact_id}\n\n"
            f"llm_model: {llm}\n"
            + (f"summary: {summary}\n" if summary else "")
            + f"content_hash: {hashlib.sha256(content).hexdigest()}\n"
        )
        try:
            subprocess.run(
                ["git", "-C", str(repo_path), "add", target.name],
                check=True, capture_output=True, text=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_path), "commit", "-m", msg],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"git commit to {self._git_repo_env}={repo_path} failed: "
                f"{e.stderr.strip() or e.stdout.strip()}"
            ) from e
        log.info("artifact %s git-committed to %s", artifact_id, repo_path)
