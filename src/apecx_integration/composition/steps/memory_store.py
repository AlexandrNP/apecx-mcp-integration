"""Git-tracked file-based memory store for code-writing workflows.

Implements the Reflexion-style verbal-lesson memory shape (Shinn et
al., NeurIPS 2023, arXiv:2303.11366). Each entry is a small JSON
file on disk; the directory IS the index. No SQL, no vector DB —
just files, atomic writes, and keyword-Jaccard retrieval.

Why "git-tracked": the memory grows by accumulating reviewable diffs.
A reviewer reading a PR sees exactly what new lessons the agent
recorded. The atomic-write contract (``os.replace(tmp, final)``)
means a crashed write can never leave a half-file in the working
tree.

Pure-Python; no BaseStep subclass. Composers instantiate this via
``from_config`` (it inherits the apecx pattern but is small enough
that it's just a dataclass plus methods). The ``MemoryReadStep`` /
``MemoryWriteStep`` wrappers in sibling modules embed it as their
state.

Schema (memory_schema_version: 1) — see ``memory/code_writing/README.md``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger(__name__)

MEMORY_SCHEMA_VERSION = 1

Status = Literal["pass", "fail", "partial"]


@dataclass(frozen=True)
class MemoryEntry:
    """One reflexion-style memory entry.

    Fields mirror ``memory/code_writing/README.md`` exactly so a
    reviewer reading a PR diff sees the same shape as the docs.
    """

    spec_id: str
    attempt_n: int
    status: Status
    lesson: str
    failure_keywords: tuple[str, ...] = ()
    spec_keywords: tuple[str, ...] = ()
    created_at: str = ""
    source_commit: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = ""
    memory_schema_version: int = MEMORY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Tuples → lists for JSON.
        d["failure_keywords"] = list(self.failure_keywords)
        d["spec_keywords"] = list(self.spec_keywords)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> MemoryEntry:
        return cls(
            spec_id=str(d["spec_id"]),
            attempt_n=int(d.get("attempt_n", 1)),
            status=str(d.get("status", "fail")),  # type: ignore[arg-type]
            lesson=str(d.get("lesson", "")),
            failure_keywords=tuple(d.get("failure_keywords") or ()),
            spec_keywords=tuple(d.get("spec_keywords") or ()),
            created_at=str(d.get("created_at", "")),
            source_commit=d.get("source_commit"),
            metadata=dict(d.get("metadata") or {}),
            id=str(d.get("id", "")),
            memory_schema_version=int(d.get("memory_schema_version", MEMORY_SCHEMA_VERSION)),
        )


_SLUG_PATTERN = re.compile(r"[^a-z0-9_.-]+")


def _slugify(s: str) -> str:
    """Filesystem-safe slug for spec_id directory naming."""
    s = (s or "").strip().lower().replace(" ", "_")
    s = _SLUG_PATTERN.sub("", s)
    return s or "anonymous"


def _now_iso() -> str:
    # UTC, second-precision, with offset suffix.
    t = time.gmtime()
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", t)


def _timestamp_token() -> str:
    """Filename-safe + chronologically sortable + collision-resistant.

    Two writes within the same second from the same process would
    otherwise produce identical filenames and the second would
    silently overwrite the first via ``os.replace``. We include
    microsecond precision + a process-local counter so collisions
    are impossible in practice.
    """
    now = time.time()
    secs = int(now)
    micro = int((now - secs) * 1_000_000)
    t = time.gmtime(secs)
    base = time.strftime("%Y-%m-%dT%H-%M-%S", t)
    counter = _next_counter()
    return f"{base}.{micro:06d}Z-{os.getpid()}-{counter:03d}"


_counter_lock = __import__("threading").Lock()
_counter_value = 0


def _next_counter() -> int:
    global _counter_value
    with _counter_lock:
        _counter_value = (_counter_value + 1) % 1000
        return _counter_value


def _git_short_commit(repo_dir: Path) -> str | None:
    """Best-effort short commit hash for the repo containing ``repo_dir``.
    Returns None when not in a git context or git is unavailable —
    NOT an error path; tests run outside a clone."""
    import subprocess

    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--short=7", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


class MemoryStore:
    """File-based, git-tracked memory store for verbal lessons.

    Construction is bound to a directory. All entries live under
    ``<root>/reflexions/<slug(spec_id)>/<id>.json``. Operators can
    point multiple workflows at the SAME root (memory is shared) or
    at separate roots (memory is per-workflow). Tests use ``tmp_path``.

    Thread-safety: read methods are safe under concurrent writers
    because Python's stat + open are atomic at the filesystem layer.
    Write methods use ``os.replace(tmp, final)`` so half-written
    files never appear on disk.
    """

    def __init__(self, root: Path):
        self._root: Path = Path(root)
        self._reflexions_dir: Path = self._root / "reflexions"
        # Create on first write — don't litter empty directories.

    @property
    def root(self) -> Path:
        return self._root

    @property
    def reflexions_dir(self) -> Path:
        return self._reflexions_dir

    def write(
        self,
        *,
        spec_id: str,
        attempt_n: int,
        status: Status,
        lesson: str,
        failure_keywords: Iterable[str] = (),
        spec_keywords: Iterable[str] = (),
        metadata: dict[str, Any] | None = None,
        skip_if_restatement: bool = True,
        min_lesson_chars: int = 40,
    ) -> Path | None:
        """Write a new memory entry, subject to gates.

        Gates (return None when triggered; do NOT raise):
          - ``lesson`` is shorter than ``min_lesson_chars`` (low-signal).
          - ``skip_if_restatement`` AND keyword-Jaccard with the newest
            entry for the same ``spec_id`` exceeds 0.7 (restatement).

        On commit: write to ``reflexions/<slug>/<id>.json`` atomically.
        Returns the final file path on success, None when skipped.
        """
        if not isinstance(lesson, str) or len(lesson.strip()) < int(min_lesson_chars):
            log.info(
                "MemoryStore.write skip: lesson too short for spec_id=%r (<%d chars after strip)",
                spec_id,
                min_lesson_chars,
            )
            return None

        spec_keywords_t = tuple(sorted(set(self._normalize_keywords(spec_keywords))))
        failure_keywords_t = tuple(sorted(set(self._normalize_keywords(failure_keywords))))

        if skip_if_restatement:
            newest = self._read_newest_for_spec(spec_id)
            if newest is not None:
                lesson_jacc = self._lesson_jaccard(newest.lesson, lesson)
                new_kws = newest.failure_keywords + newest.spec_keywords
                cur_kws = failure_keywords_t + spec_keywords_t
                # Lesson-only restatement: identical / near-identical
                # text triggers a skip regardless of keyword presence.
                if lesson_jacc > 0.7:
                    log.info(
                        "MemoryStore.write skip: lesson restates newest "
                        "entry for spec_id=%r (lesson Jaccard %.2f > 0.7)",
                        spec_id,
                        lesson_jacc,
                    )
                    return None
                # When both sides DO have keywords, require both
                # keyword + lesson similarity for a softer match.
                if new_kws and cur_kws:
                    kw_jacc = self._keyword_jaccard(new_kws, cur_kws)
                    if kw_jacc > 0.7 and lesson_jacc > 0.5:
                        log.info(
                            "MemoryStore.write skip: keywords+lesson both "
                            "highly similar to newest for spec_id=%r "
                            "(kw=%.2f, lesson=%.2f)",
                            spec_id,
                            kw_jacc,
                            lesson_jacc,
                        )
                        return None

        entry_id = _timestamp_token()
        entry = MemoryEntry(
            spec_id=spec_id,
            attempt_n=int(attempt_n),
            status=status,
            lesson=lesson.strip(),
            failure_keywords=failure_keywords_t,
            spec_keywords=spec_keywords_t,
            created_at=_now_iso(),
            source_commit=_git_short_commit(self._root),
            metadata=dict(metadata or {}),
            id=entry_id,
        )

        spec_dir = self._reflexions_dir / _slugify(spec_id)
        spec_dir.mkdir(parents=True, exist_ok=True)
        final = spec_dir / f"{entry_id}.json"
        # Atomic write via mkstemp + os.replace.
        fd, tmp_path = tempfile.mkstemp(
            prefix=f"{entry_id}.", suffix=".json.tmp", dir=str(spec_dir)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(entry.to_dict(), fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp_path, final)
        except Exception:
            # Try to clean up the tmp file; don't mask the real exception.
            import contextlib

            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

        log.info(
            "MemoryStore.write: %s (spec_id=%r, status=%s, lesson=%d chars)",
            final,
            spec_id,
            status,
            len(entry.lesson),
        )
        return final

    def read_for_spec(
        self,
        spec_id: str,
        *,
        limit: int = 3,
    ) -> list[MemoryEntry]:
        """Return the most recent ``limit`` entries for ``spec_id``,
        newest first. Empty list when none found."""
        spec_dir = self._reflexions_dir / _slugify(spec_id)
        if not spec_dir.is_dir():
            return []
        entries = []
        for p in sorted(spec_dir.glob("*.json"), reverse=True):
            try:
                entries.append(self._load(p))
            except Exception as e:
                log.warning(
                    "MemoryStore.read_for_spec: skipping malformed entry %s: %s",
                    p,
                    e,
                )
            if len(entries) >= limit:
                break
        return entries

    def read_by_keywords(
        self,
        *,
        spec_keywords: Iterable[str],
        limit: int = 3,
    ) -> list[MemoryEntry]:
        """Fall-back retrieval when no spec_id match: return entries
        whose ``spec_keywords`` intersect ``spec_keywords`` (Jaccard
        ranked, ties broken by recency)."""
        target = set(self._normalize_keywords(spec_keywords))
        if not target:
            return []
        if not self._reflexions_dir.is_dir():
            return []
        candidates: list[tuple[float, str, MemoryEntry]] = []
        for spec_dir in self._reflexions_dir.iterdir():
            if not spec_dir.is_dir():
                continue
            for p in spec_dir.glob("*.json"):
                try:
                    entry = self._load(p)
                except Exception:
                    continue
                jacc = self._keyword_jaccard(tuple(target), entry.spec_keywords)
                if jacc > 0:
                    candidates.append((jacc, entry.created_at, entry))
        candidates.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [c[2] for c in candidates[:limit]]

    def all_entries(self) -> list[MemoryEntry]:
        """Return every entry in the store. Useful for tests / audits."""
        out: list[MemoryEntry] = []
        if not self._reflexions_dir.is_dir():
            return out
        for spec_dir in self._reflexions_dir.iterdir():
            if not spec_dir.is_dir():
                continue
            for p in spec_dir.glob("*.json"):
                try:
                    out.append(self._load(p))
                except Exception:
                    continue
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load(self, path: Path) -> MemoryEntry:
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            raise ValueError(f"MemoryStore: {path} does not contain a JSON object")
        schema = int(raw.get("memory_schema_version", MEMORY_SCHEMA_VERSION))
        if schema > MEMORY_SCHEMA_VERSION:
            raise ValueError(
                f"MemoryStore: {path} has memory_schema_version={schema}, "
                f"newer than this code understands ({MEMORY_SCHEMA_VERSION}). "
                f"Update the code or downgrade the entry."
            )
        return MemoryEntry.from_dict(raw)

    def _read_newest_for_spec(self, spec_id: str) -> MemoryEntry | None:
        entries = self.read_for_spec(spec_id, limit=1)
        return entries[0] if entries else None

    @staticmethod
    def _normalize_keywords(keywords: Iterable[str]) -> list[str]:
        out: list[str] = []
        for kw in keywords or ():
            if not isinstance(kw, str):
                continue
            normalized = kw.strip().lower().replace(" ", "_")
            normalized = re.sub(r"[^a-z0-9_]+", "", normalized)
            if normalized:
                out.append(normalized)
        return out

    @staticmethod
    def _keyword_jaccard(a: Iterable[str], b: Iterable[str]) -> float:
        sa, sb = set(a), set(b)
        if not sa and not sb:
            return 0.0
        union = sa | sb
        return len(sa & sb) / len(union) if union else 0.0

    @staticmethod
    def _lesson_jaccard(a: str, b: str) -> float:
        """Token-Jaccard on alphanumeric tokens of two lessons."""
        ta = set(re.findall(r"[a-z0-9]+", a.lower()))
        tb = set(re.findall(r"[a-z0-9]+", b.lower()))
        if not ta and not tb:
            return 0.0
        return len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0


__all__ = [
    "MemoryStore",
    "MemoryEntry",
    "MEMORY_SCHEMA_VERSION",
]
