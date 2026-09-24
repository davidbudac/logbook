"""Every `dbwiki` module imports on its own, first, in a clean interpreter.

An import cycle only breaks when the modules in it are imported in the
unlucky order, and the test suite always imports them in the same, lucky
one — so a new cycle passes every other test and fails in production when a
cron line happens to start from the other end. This imports each module
first (every `dbwiki` entry dropped from `sys.modules` in between), in a
child interpreter so nothing here disturbs the modules the suite holds.
"""

import json
import pkgutil
import subprocess
import sys

import dbwiki

MODULES = sorted(m.name for m in pkgutil.walk_packages(dbwiki.__path__, "dbwiki.")
                 if not m.name.endswith("__main__"))

_PROBE = r"""
import importlib, json, sys, traceback
failed = {}
for name in json.loads(sys.argv[1]):
    for loaded in [m for m in sys.modules if m == "dbwiki" or m.startswith("dbwiki.")]:
        del sys.modules[loaded]
    try:
        importlib.import_module(name)
    except BaseException:
        failed[name] = traceback.format_exc(limit=-3)
print(json.dumps(failed))
"""


def _probe(script: str, *args: str) -> str:
    out = subprocess.run([sys.executable, "-c", script, *args],
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_the_walk_finds_the_modules():
    assert {"dbwiki.health", "dbwiki.alerts", "dbwiki.orchestrate",
            "dbwiki.errors", "dbwiki.portal.server"} <= set(MODULES)


def test_every_module_imports_first_in_a_fresh_interpreter():
    failed = json.loads(_probe(_PROBE, json.dumps(MODULES)))
    assert not failed, "\n".join(f"{name}:\n{tb}" for name, tb in failed.items())


def test_errors_is_a_leaf():
    """`errors` exists to sit below health, alerts and orchestrate; one
    import from the package would put it back inside their cycle."""
    loaded = json.loads(_probe(
        "import json, sys, dbwiki.errors; "
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('dbwiki'))))"))
    assert loaded == ["dbwiki", "dbwiki.errors"]
