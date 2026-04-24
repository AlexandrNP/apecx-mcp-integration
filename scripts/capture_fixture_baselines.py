"""Capture baseline_hash.txt + baseline_content.yml for T12 fixtures.

For each fixture under ``tests/reproducibility/fixtures/``:

1. If ``canned_response.txt`` exists, extract the first ``yaml`` fenced
   block using the SAME regex the composer uses in ``_parse_response``
   (so the hash matches what ``Composer.compose`` would produce).
2. Write the extracted body to ``baseline_content.yml``.
3. Write ``sha256(body.encode("utf-8")).hexdigest()`` to
   ``baseline_hash.txt``.

Fixtures without ``canned_response.txt`` are skipped with a log line —
those are the live-LLM fixtures whose baselines must be captured by an
operator against a pinned model, not regenerated offline.

Pass ``--only <name>`` to restrict to one fixture (useful during
iterative authoring).
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

# Same regex as composer._FENCE_RE — keep these in sync if the composer
# ever changes its fence-extraction rule.
_FENCE_RE = re.compile(
    r"```\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\n"
    r"(.*?)"
    r"\n```",
    re.DOTALL,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "reproducibility" / "fixtures"


def _extract_yaml_body(canned: str) -> str:
    for match in _FENCE_RE.finditer(canned):
        if match.group(1).lower() == "yaml":
            return match.group(2)
    raise ValueError("canned_response.txt has no ```yaml fenced block")


def _process_fixture(fixture_dir: Path) -> bool:
    canned = fixture_dir / "canned_response.txt"
    if not canned.is_file():
        print(f"  skip {fixture_dir.name}: no canned_response.txt (live-LLM?)")
        return False
    body = _extract_yaml_body(canned.read_text(encoding="utf-8"))
    body_bytes = body.encode("utf-8")
    digest = hashlib.sha256(body_bytes).hexdigest()
    (fixture_dir / "baseline_content.yml").write_bytes(body_bytes)
    (fixture_dir / "baseline_hash.txt").write_text(digest + "\n", encoding="utf-8")
    print(f"  wrote {fixture_dir.name}: sha256={digest[:16]}... ({len(body_bytes)} bytes)")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--only",
        help="Restrict to one fixture by name (matches the subdir name).",
    )
    args = parser.parse_args()

    if not FIXTURES_DIR.is_dir():
        print(f"no fixtures dir at {FIXTURES_DIR}", file=sys.stderr)
        return 2

    processed = 0
    for entry in sorted(FIXTURES_DIR.iterdir()):
        if not entry.is_dir():
            continue
        if args.only and entry.name != args.only:
            continue
        if _process_fixture(entry):
            processed += 1
    print(f"captured {processed} baseline(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
