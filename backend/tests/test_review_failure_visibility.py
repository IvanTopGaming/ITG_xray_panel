import builtins
import logging

import pytest


def test_failed_domain_cleanup_is_visible_to_scheduler(db, monkeypatch):
    from panel_core.models import DomainStat
    from panel_core.services.traffic_store import cleanup_old_domain_stats
    from sqlalchemy.orm import Query

    original = Query.delete

    def failed(query, *args, **kwargs):
        if query.column_descriptions[0]["entity"] is DomainStat:
            raise RuntimeError("database unavailable")
        return original(query, *args, **kwargs)

    monkeypatch.setattr(Query, "delete", failed)
    with pytest.raises(RuntimeError, match="database unavailable"):
        cleanup_old_domain_stats()


def test_missing_psycogreen_does_not_mark_patch_as_success(monkeypatch, caplog):
    from panel_core import pg_compat

    monkeypatch.setattr(pg_compat, "_patched", False)
    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "psycogreen.gevent":
            raise ImportError("not installed")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with caplog.at_level(logging.ERROR):
        pg_compat.patch_gevent_psycopg()
    assert not pg_compat._patched
    assert "psycogreen" in caplog.text
