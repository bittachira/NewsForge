"""OPS_HARDENING_PERSISTENCE CI probe: prove data survives container restarts.

    scripts/persistence_probe.py write   # in a first container: init schema + insert probe row
    scripts/persistence_probe.py read    # in a second container: require exactly 1 probe row

Runs with the container's own env (NEWSFORGE_DB_PATH=/data/newsforge.db) and the shared
engine/factory exactly like the production startup path (init_db -> get_session).
"""
import sys

from sqlalchemy import select

from newsforge.db import stories
from newsforge.db.session import get_session_factory, init_db

PROBE_SLUG = "ci-persistence-probe"


def _count_probe_rows() -> int:
    with get_session_factory()() as session:
        rows = session.scalars(
            select(stories).where(stories.slug == PROBE_SLUG)
        ).all()
    return len(rows)


def write() -> None:
    init_db()
    with get_session_factory()() as session:
        session.add(stories(slug=PROBE_SLUG, title="Persistence probe"))
        session.commit()


def read() -> int:
    init_db()
    return _count_probe_rows()


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) != 1 or args[0] not in ("write", "read"):
        sys.exit("usage: persistence_probe.py write|read")
    if args[0] == "write":
        write()
        print("PERSISTENCE_WRITE_OK")
    else:
        count = read()
        print(f"PROBE_COUNT={count}")
        if count != 1:
            print("FAIL: expected exactly 1 persisted row, got", count)
            sys.exit(1)
        print("PERSISTENCE_READ_OK")