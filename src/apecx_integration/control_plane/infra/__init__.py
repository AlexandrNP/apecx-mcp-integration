"""Infrastructure governance for the Control Plane.

On a scientist laptop, the Control Plane is supposed to "just work" —
no manual ``docker compose up`` before the first ``apecx-cp serve``.
This package contains the machinery that decides whether the app
should provision its own Postgres, which container runtime to use
(Docker preferred, Apptainer fallback for HPC), and how to bring
the container up/down.

Deployment modes, matrix:

    | DB URL target               | Action on startup            |
    | --------------------------- | ---------------------------- |
    | sqlite:///...               | nothing — SQLite is fileborne |
    | postgres on localhost:5433  | bring up local container      |
    | postgres elsewhere (BYO)    | nothing — user manages it     |

See ``docs/infra_governance.md`` for the full design narrative.
"""
