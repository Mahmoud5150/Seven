"""Shared fixtures. `any_db` runs a test on SQLite AND on a real PostgreSQL
(an embedded server from the `pgserver` package; skipped if it isn't installed)."""

import itertools
import tempfile

import pytest

_pg = {}
_n = itertools.count()


def _pg_server():
    if "srv" not in _pg:
        pgserver = pytest.importorskip("pgserver")
        _pg["srv"] = pgserver.get_server(tempfile.mkdtemp(), cleanup_mode="stop")
    return _pg["srv"]


@pytest.fixture(params=["sqlite", "postgres"])
def any_db(request, tmp_path):
    """A database URL for a fresh, empty database."""
    if request.param == "sqlite":
        return str(tmp_path / "t.db")
    srv = _pg_server()
    name = f"t{next(_n)}"
    srv.psql(f"CREATE DATABASE {name};")
    return srv.get_uri(name)


def pytest_sessionfinish(session, exitstatus):
    if "srv" in _pg:
        _pg["srv"].cleanup()
