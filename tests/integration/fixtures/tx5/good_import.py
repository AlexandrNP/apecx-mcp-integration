"""TX5 AC2 fixture: only stdlib + clearly-resolvable imports.

``scripts/checks/imports_resolve.py`` should accept a directory
containing only this file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class Example:
    data: dict[str, Any]

    def dump(self) -> str:
        return json.dumps(self.data)
