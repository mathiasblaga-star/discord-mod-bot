"""
conftest.py — shared pytest fixtures for the entire test suite.

Problem this solves
-------------------
`database.py` does `from config import DB_PATH` at import time, creating a
module-level copy.  If test files each independently patch `config.DB_PATH`,
the already-imported `database.DB_PATH` stays frozen on whatever value it had
when the module was first loaded into `sys.modules`.

Solution
--------
This conftest runs before any test module is imported.  It:
  1. Creates a single temp DB file for the whole test session.
  2. Patches BOTH `config.DB_PATH` and `database.DB_PATH` so every module
     that references either name hits the same test database.
  3. Initialises the schema once via `init_db()`.
  4. Cleans up the temp file after the session.
"""

import asyncio
import importlib
import os
import sys
import tempfile

import pytest


# ── 1. Temp DB created before anything else is imported ─────────────────────

_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
TEST_DB_PATH = _tmp_db.name


# ── 2. Patch config before database.py is imported ──────────────────────────
#    conftest.py is evaluated by pytest before test modules are collected,
#    so this runs before the first `import database` in any test file.

# Ensure the project root is on sys.path
_root = os.path.dirname(os.path.dirname(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)

import config                      # noqa: E402  (must come after sys.path fix)
config.DB_PATH = TEST_DB_PATH      # patch config's copy

# Force-import database now so its module-level DB_PATH binding
# uses our patched config.DB_PATH value.
if "database" in sys.modules:
    # Already imported with a different path — reload it so the new
    # config.DB_PATH value is picked up by `from config import DB_PATH`.
    import database as _db_mod
    importlib.reload(_db_mod)
    import database                # re-bind the name
else:
    import database                # first import — picks up patched config.DB_PATH

# Patch database's own module-level name just to be safe
database.DB_PATH = TEST_DB_PATH    # noqa: F821


# ── 3. Schema initialisation (runs once per session) ─────────────────────────

def _run(coro):
    """Run a coroutine synchronously (conftest setup is sync)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_run(database.init_db())


# ── 4. Session-scoped fixture that confirms the DB path is correct ────────────

@pytest.fixture(scope="session", autouse=True)
def test_database_path():
    """Expose the temp DB path and confirm both modules see the same path."""
    assert config.DB_PATH == TEST_DB_PATH, "config.DB_PATH mismatch"
    assert database.DB_PATH == TEST_DB_PATH, "database.DB_PATH mismatch"
    yield TEST_DB_PATH


# ── 5. Cleanup after session ─────────────────────────────────────────────────

def pytest_sessionfinish(session, exitstatus):
    try:
        os.unlink(TEST_DB_PATH)
    except OSError:
        pass
