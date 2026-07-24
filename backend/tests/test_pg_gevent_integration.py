import os
import time

import pytest

DSN = os.getenv("DATABASE_URL_TEST", "").strip()
pytestmark = pytest.mark.skipif(not DSN, reason="DATABASE_URL_TEST not set")


def test_psycopg2_yields_to_gevent_hub():
    from app.pg_compat import patch_gevent_psycopg

    patch_gevent_psycopg()

    import gevent
    import psycopg2

    def query():
        conn = psycopg2.connect(DSN)
        try:
            cur = conn.cursor()
            cur.execute("SELECT pg_sleep(0.3)")
            cur.fetchall()
        finally:
            conn.close()

    t0 = time.monotonic()
    jobs = [gevent.spawn(query) for _ in range(5)]
    gevent.joinall(jobs, timeout=10)
    elapsed = time.monotonic() - t0

    assert all(j.successful() for j in jobs)
    assert elapsed < 1.0, f"queries serialized ({elapsed:.2f}s) — gevent not cooperating"
