"""``apecx-lookup`` — CLI wrapper around the synonym-dictionary lookup pipeline.

Resolves a user-supplied surface form to its canonical IRI / label /
synonym set using the Stage 2 lookup pipeline (``apecx_integration.
synonym_dictionary.lookup.lookup_entity``).

Invocation
----------

Three equivalent forms (the first two go through the package's
installed import machinery and Just Work; the third is supported
via a ``sys.path`` self-bootstrap when no install is active):

    apecx-lookup ZIKV                              # installed console script
    .venv/bin/python -m apecx_integration.cli.lookup ZIKV
    .venv/bin/python src/apecx_integration/cli/lookup.py ZIKV

``ModuleNotFoundError`` on ``apecx_integration`` is almost always
wrong-Python — use ``.venv/bin/python``, not ``/opt/anaconda3/bin/python``.
The system Python lacks the editable install (workspace CLAUDE.md
rule #7).

Operator-facing flow:

    apecx-lookup "Eastern equine encephalitis virus"
    apecx-lookup EEEV --type pathogen
    apecx-lookup RSV --json                       # surfaces AMBIGUOUS candidates
    apecx-lookup --dict-path /path/to/dict.sqlite SARS-CoV-2

The tool intentionally adds NO resolution logic of its own — it points
the loader at the configured SQLite, calls ``lookup_entity``, and
renders the existing :class:`LookupResult`. The five paths
(``fast`` / ``ambiguous`` / ``ancestor`` / ``slow`` / ``miss``) are
reported verbatim so a scripted consumer sees exactly what the
in-process API would see.

Exit codes:
  0 = any non-miss result (fast / ambiguous / ancestor / slow)
  1 = miss
  2 = invalid CLI arguments
  3 = dictionary artifact not loadable
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

# Self-bootstrap: when invoked as ``python src/apecx_integration/cli/lookup.py``
# (file path, not ``-m``), Python puts the CLI directory on ``sys.path``
# instead of the project's ``src/``. That makes ``from apecx_integration...``
# fail even on an interpreter that has every other dep installed.
# Inserting ``src/`` here matches what the ``-m`` and console-script paths
# already get for free. Guarded so we only act when running as the main
# script — no effect on import-time use.
if __name__ == "__main__" and __package__ in (None, ""):
    # Path(__file__).resolve().parents[2] is the ``src/`` directory:
    #   parents[0] = .../cli
    #   parents[1] = .../apecx_integration
    #   parents[2] = .../src   <-- what we need on sys.path
    _SRC_DIR = Path(__file__).resolve().parents[2]
    if (_SRC_DIR / "apecx_integration" / "__init__.py").exists():
        sys.path.insert(0, str(_SRC_DIR))

from apecx_integration.synonym_dictionary.enums import EntityType  # noqa: E402
from apecx_integration.synonym_dictionary.loader import (  # noqa: E402
    configure_dictionary_path,
    get_dictionary_index,
)
from apecx_integration.synonym_dictionary.lookup import (  # noqa: E402
    LookupResult,
    lookup_entity,
)

_DEFAULT_DICT_PATH = Path.home() / ".apecx" / "dictionary" / "dictionary.sqlite"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apecx-lookup",
        description=(
            "Resolve a surface form (label, synonym, or IRI) to its "
            "canonical entry via the apecx synonym dictionary. Surfaces "
            "ambiguity (multiple candidate IRIs) instead of silently "
            "picking one."
        ),
    )
    parser.add_argument(
        "query",
        help="The surface form to resolve. May be a label, a synonym, "
        "or a canonical IRI (http://purl.obolibrary.org/obo/...).",
    )
    parser.add_argument(
        "--type",
        "-t",
        dest="entity_type",
        choices=[et.value for et in EntityType],
        default=None,
        help="Restrict the lookup to one entity type. Default: search all types.",
    )
    parser.add_argument(
        "--dict-path",
        type=Path,
        default=None,
        help=(
            "Path to the dictionary SQLite artifact. "
            "Falls back to $APECX_SYNONYM_DICT_PATH, then "
            f"{_DEFAULT_DICT_PATH}."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the LookupResult as JSON instead of human-readable text.",
    )
    return parser


def _resolve_dict_path(arg_path: Path | None) -> Path:
    """Pick the dictionary path: explicit flag > env var > default."""
    if arg_path is not None:
        return arg_path
    env = os.environ.get("APECX_SYNONYM_DICT_PATH")
    if env:
        return Path(env)
    return _DEFAULT_DICT_PATH


def _render_text(result: LookupResult) -> str:
    """Human-readable rendering. Multi-line block; concise."""
    lines: list[str] = [
        f"query        : {result.surface_form}",
        f"path         : {result.path}",
        f"status       : {result.resolution_status.value}",
        f"confidence   : {result.confidence:.2f}",
    ]
    if result.path == "ambiguous":
        lines.append(f"candidates   : {len(result.candidates)} (HITL required)")
        for i, cand in enumerate(result.candidates, 1):
            tail = cand.canonical_iri.rsplit("/", 1)[-1]
            lines.append(
                f"  [{i}] {tail:30s}  conf={cand.confidence:.2f}  label={cand.canonical_label}"
            )
    else:
        lines.append(f"canonical_iri: {result.canonical_iri or '<none>'}")
        lines.append(f"label        : {result.canonical_label or '<none>'}")
        lines.append(f"ontology     : {result.canonical_ontology or '<none>'}")
        if result.synonyms:
            preview = ", ".join(result.synonyms[:10])
            more = f" (+{len(result.synonyms) - 10} more)" if len(result.synonyms) > 10 else ""
            lines.append(f"synonyms ({len(result.synonyms)}): {preview}{more}")
    if result.evidence:
        lines.append(f"evidence     : {result.evidence}")
    return "\n".join(lines)


def _render_json(result: LookupResult) -> str:
    """Machine-readable rendering of the full LookupResult."""
    payload = asdict(result)
    # ``resolution_status`` is an Enum — asdict keeps it as the enum
    # instance, which json.dumps can't serialize. Coerce to its string.
    payload["resolution_status"] = result.resolution_status.value
    return json.dumps(payload, indent=2, default=str)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    dict_path = _resolve_dict_path(args.dict_path)
    if not dict_path.exists():
        print(
            f"apecx-lookup: dictionary artifact not found at {dict_path}\n"
            f"  set --dict-path or APECX_SYNONYM_DICT_PATH, or run "
            f"`apecx-mcp` once to build it lazily.",
            file=sys.stderr,
        )
        return 3

    configure_dictionary_path(dict_path)
    _index, err = get_dictionary_index()
    if err is not None:
        print(f"apecx-lookup: failed to load dictionary: {err}", file=sys.stderr)
        return 3

    entity_type = EntityType(args.entity_type) if args.entity_type else None
    result = lookup_entity(args.query, entity_type=entity_type)

    if args.json:
        print(_render_json(result))
    else:
        print(_render_text(result))

    return 1 if result.path == "miss" else 0


if __name__ == "__main__":
    sys.exit(main())
