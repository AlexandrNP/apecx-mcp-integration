"""TX5 AC2 fixture: hallucinated / nonexistent top-level package.

``scripts/checks/imports_resolve.py`` should reject a directory
containing this file, identifying ``definitely_not_a_real_package``
as the unresolvable module.
"""

from __future__ import annotations

import definitely_not_a_real_package  # noqa: F401  — intentional
from also_fake_pkg.submodule import something  # noqa: F401  — intentional
