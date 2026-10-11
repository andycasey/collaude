import io
import secrets
import sys

import pytest

from helpers import ADMIN_DSN, cl


@pytest.fixture(scope="module")
def db():
    """A fresh database with groups g1, g2 and users alice (g1), bob (g2), carol (g1, g2)."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    tag = secrets.token_hex(3)
    dbname = f"collaude_test_{tag}"
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute(f"create database {dbname}")
    admin = make_conninfo(**{**conninfo_to_dict(ADMIN_DSN), "dbname": dbname})
    run = lambda *a: cl.main(["admin", "--admin-dsn", admin, *a])
    run("migrate")
    run("migrate")  # idempotent
    run("add-group", "g1", "--name", "Group one")
    run("add-group", "g2")
    users = {}
    for name, groups in (("alice", ["g1"]), ("bob", ["g2"]), ("carol", ["g1", "g2"])):
        role = f"t{tag}_{name}"
        buf, old = io.StringIO(), sys.stdout
        sys.stdout = buf
        try:
            run("add-user", f"{name}@example.org", "--name", name.title(), "--role", role,
                *[x for g in groups for x in ("--group", g)])
        finally:
            sys.stdout = old
        users[name] = buf.getvalue().strip().splitlines()[-1].strip()
        assert users[name].startswith("host=") or "password=" in users[name]
    yield {"admin": admin, "tag": tag, **users}
    with psycopg.connect(ADMIN_DSN, autocommit=True) as c:
        c.execute(f"drop database {dbname} with (force)")
        for name in ("alice", "bob", "carol"):
            c.execute(f"drop role if exists t{tag}_{name}")
