"""``apecx-regression-metrics`` — CLI wrapper around
``apecx_integration.regression_metrics.compute_regression_metrics``.

Reads the control-plane DB (per the ``APECX_CP_DB_URL`` env var, or
the default-resolved SQLite path), computes the three regression
signals introduced this session (compose_retries, runtime_violations,
retrieval_gap), and prints them as a table OR JSON.

Operator-facing flow:

    apecx-regression-metrics                  # table to stdout
    apecx-regression-metrics --json           # JSON to stdout
    apecx-regression-metrics --since 2026-05-01

Exit codes:
  0 = report computed successfully
  1 = DB read failed (URL wrong / migrations not run / FK violation)
  2 = invalid CLI args
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from apecx_integration.control_plane.db import (
    get_db_url,
    make_engine,
    make_session_factory,
)
from apecx_integration.regression_metrics import (
    compute_regression_metrics,
)


def _parse_since(arg: str | None) -> datetime | None:
    if arg is None:
        return None
    # Accept ISO-format. Be permissive on the time component — we
    # care about the date for operator usage; finer granularity is
    # for programmatic callers.
    try:
        if "T" in arg:
            return datetime.fromisoformat(arg)
        return datetime.fromisoformat(arg + "T00:00:00")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--since must be an ISO date (e.g. 2026-05-11) "
            f"or full datetime (2026-05-11T12:34:56); got {arg!r}: {exc}"
        ) from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apecx-regression-metrics",
        description=(
            "Report on the three regression signals introduced for "
            "automated workflow generation reliability: C1 compose "
            "retries, C2 runtime violations, A2 retrieval-gap rate."
        ),
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help=(
            "Control-plane DB URL (overrides APECX_CP_DB_URL / the "
            "default SQLite path). Use for ad-hoc reads against a "
            "snapshot."
        ),
    )
    parser.add_argument(
        "--since",
        type=_parse_since,
        default=None,
        help=(
            "Only include artifacts created on/after this UTC "
            "datetime (ISO format). Default: all-time."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of the human-readable table.",
    )
    parser.add_argument(
        "--rule-id",
        default=None,
        help=(
            "Filter the JSON / table output to a single rule_id "
            "(e.g. ``step_inline_config_forbidden``). The total "
            "counts still reflect ALL violations; only the by_rule_id "
            "section is filtered."
        ),
    )
    return parser


def _filter_rule_id(report_dict: dict, rule_id: str) -> dict:
    """Narrow the by_rule_id breakdown in-place."""
    rv = report_dict.get("runtime_violations", {})
    by_rule = rv.get("by_rule_id", {})
    rv["by_rule_id"] = {k: v for k, v in by_rule.items() if k == rule_id}
    return report_dict


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    db_url = args.db_url or get_db_url()
    try:
        engine = make_engine(db_url)
        session_factory = make_session_factory(engine)
        report = compute_regression_metrics(session_factory, since=args.since)
    except Exception as exc:
        print(
            f"apecx-regression-metrics: failed to read DB at {db_url}: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    if args.json:
        out = report.to_dict()
        if args.rule_id:
            out = _filter_rule_id(out, args.rule_id)
        print(json.dumps(out, indent=2, default=str))
    else:
        if args.rule_id:
            # Replace just the by_rule_id dict so the rest of the
            # table is unchanged. Operators reading the table want
            # the same headline counts; only the rule-breakdown is
            # filtered.
            text = report.to_table()
            # Best-effort string filter: drop lines that don't
            # mention the rule_id under "By rule_id:".
            lines = text.splitlines()
            kept: list[str] = []
            in_rule_block = False
            for line in lines:
                if line.strip() == "By rule_id:":
                    in_rule_block = True
                    kept.append(line)
                    continue
                if in_rule_block and line.startswith("    "):
                    if args.rule_id in line:
                        kept.append(line)
                    continue
                if in_rule_block and (
                    line.strip().startswith("By failure_class:")
                    or not line.strip()
                    or not line.startswith("    ")
                ):
                    in_rule_block = False
                kept.append(line)
            text = "\n".join(kept)
            print(text)
        else:
            print(report.to_table())
    return 0


if __name__ == "__main__":
    sys.exit(main())
