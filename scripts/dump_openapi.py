"""Dump the Control Plane OpenAPI schema to ``docs/api_contract.yaml``.

Keeping the YAML in the repo makes the TX1 contract discoverable (e.g. for MCP
tool schema generation, reviewers, or code generation in another language). We
do not hand-edit it; it is regenerated from the live FastAPI app whenever the
schemas change.

Usage:
    python scripts/dump_openapi.py

CI can also run:
    python scripts/dump_openapi.py --check
to fail if the checked-in YAML drifts from the schemas.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from apecx_integration.control_plane.app import create_app

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "docs" / "api_contract.yaml"


def render_openapi_yaml() -> str:
    schema = create_app().openapi()
    return yaml.safe_dump(schema, sort_keys=True, default_flow_style=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Dump the OpenAPI contract to YAML.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit nonzero if docs/api_contract.yaml is not up to date.",
    )
    args = parser.parse_args()

    rendered = render_openapi_yaml()

    if args.check:
        on_disk = OUTPUT.read_text() if OUTPUT.exists() else ""
        if on_disk != rendered:
            sys.stderr.write(
                f"{OUTPUT.relative_to(REPO_ROOT)} is out of date — run "
                "`python scripts/dump_openapi.py` to refresh.\n"
            )
            return 1
        return 0

    OUTPUT.write_text(rendered)
    print(f"Wrote {OUTPUT.relative_to(REPO_ROOT)} ({len(rendered)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
