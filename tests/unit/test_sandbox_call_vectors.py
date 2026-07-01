"""#1b (2026-07-01) — ImportScanner flags deserialization / arbitrary-code CALLS on whitelisted
libraries (np.load(allow_pickle=True), pd.read_pickle, pickle/dill/joblib.load), alias-aware, WITHOUT
banning numpy/pandas imports. Benign DataFrame/array ops (incl. np.load safe default) still pass.

The import-name whitelist can't see these: numpy/pandas are legitimately whitelisted for array /
DataFrame work, but a few of their functions unpickle untrusted input = RCE. These pin the narrow
deny-list of dangerous CALLS + the alias resolution that makes it precise (no false positives on an
unrelated local named ``np``).
"""

from __future__ import annotations

import pytest

from apecx_integration.composition.sandbox import ImportScanner

# The dangerous libs are WHITELISTED (imports pass); the point is the dangerous CALL is caught anyway.
_WL = frozenset({"numpy", "pandas", "pickle", "dill", "joblib"})


def _scan(src: str):
    return ImportScanner(whitelist=_WL).scan(src)


@pytest.mark.parametrize(
    "src",
    [
        'import numpy as np\nnp.load("x.npy", allow_pickle=True)',
        'import numpy\nnumpy.load("x.npy", allow_pickle=True)',
        'from numpy import load\nload("x.npy", allow_pickle=True)',
        'import pandas as pd\npd.read_pickle("x.pkl")',
        'from pandas import read_pickle\nread_pickle("x.pkl")',
        'import pickle\npickle.loads(b"...")',
        "import dill\ndill.load(f)",
        'import joblib\njoblib.load("m.pkl")',
    ],
)
def test_dangerous_calls_are_flagged(src):
    r = _scan(src)
    assert not r.ok
    assert any("dangerous call" in v for v in r.violations), r.violations


@pytest.mark.parametrize(
    "src",
    [
        'import numpy as np\nnp.load("x.npy")',  # safe default allow_pickle=False
        "import numpy as np\narr = np.array([1, 2])\nnp.linalg.norm(arr)\nnp.asarray(arr)",
        'import pandas as pd\npd.read_csv("x.csv")\npd.DataFrame({"a": [1]})',
        'import numpy as np\nnp.load("x.npy", allow_pickle=False)',  # explicit safe
    ],
)
def test_benign_numeric_ops_pass(src):
    r = _scan(src)
    assert r.ok, r.violations


def test_np_load_kwarg_gate_true_vs_default():
    assert _scan('import numpy as np\nnp.load("f.npy")').ok
    assert not _scan('import numpy as np\nnp.load("f", allow_pickle=True)').ok


def test_alias_import_np_load_resolved():
    # `import numpy as np` binding is followed so np.load(...) resolves to numpy.load.
    assert not _scan('import numpy as np\nnp.load("x", allow_pickle=True)').ok


def test_no_false_positive_on_unrelated_local_named_np():
    # `np` is a local object, NOT numpy (no import binding) — must NOT be flagged.
    assert _scan('np = object()\nnp.load("x", allow_pickle=True)').ok


def test_existing_banned_calls_still_flagged():
    # Regression: the pre-existing eval + importlib.import_module bans still fire alongside #1b.
    assert not _scan('eval("1")').ok
    r = ImportScanner(whitelist=frozenset({"importlib"})).scan(
        'import importlib\nimportlib.import_module("os")'
    )
    assert not r.ok
    assert any("banned dynamic-import" in v for v in r.violations), r.violations
