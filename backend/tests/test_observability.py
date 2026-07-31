import logging
import time
from unittest.mock import MagicMock, patch

import pytest
import requests
from flask import Flask

from panel_core.observability import init_request_logging, run_job_logged, setup_logging


@pytest.fixture(autouse=True)
def _restore_logging_state():
    root = logging.getLogger()
    saved = {name: logging.getLogger(name).level for name in ("", "sqlalchemy.engine", "urllib3", "requests")}
    old_handlers = list(root.handlers)
    yield
    for name, level in saved.items():
        logging.getLogger(name).setLevel(level)
    root.handlers[:] = old_handlers


class TestSetupLogging:
    def test_level_from_env(self, monkeypatch):
        monkeypatch.setenv("BACKEND_LOG_LEVEL", "DEBUG")
        setup_logging()
        assert logging.getLogger().level == logging.DEBUG

    def test_invalid_level_falls_back_to_info(self, monkeypatch):
        monkeypatch.setenv("BACKEND_LOG_LEVEL", "BANANA")
        setup_logging()
        assert logging.getLogger().level == logging.INFO

    def test_debug_mode_enables_sql_echo(self, monkeypatch):
        monkeypatch.setenv("BACKEND_LOG_LEVEL", "DEBUG")
        setup_logging()
        assert logging.getLogger("sqlalchemy.engine").level == logging.INFO

    def test_idempotent_handler_setup(self, monkeypatch):
        monkeypatch.setenv("BACKEND_LOG_LEVEL", "INFO")
        root = logging.getLogger()
        before = len(root.handlers)
        setup_logging()
        after_first = len(root.handlers)
        setup_logging()
        assert len(root.handlers) == after_first
        assert after_first >= before


class TestRequestLogging:
    def _make_app(self):
        app = Flask(__name__)
        init_request_logging(app)

        @app.get("/ping")
        def ping():
            return {"ok": True}

        @app.get("/healthz")
        def healthz():
            return {"status": "ok"}

        return app

    def test_logs_method_path_status_duration(self, caplog):
        app = self._make_app()
        with caplog.at_level(logging.INFO, logger="app.requests"):
            app.test_client().get("/ping")
        records = [r for r in caplog.records if r.name == "app.requests"]
        assert records
        msg = records[0].getMessage()
        assert "GET" in msg
        assert "/ping" in msg
        assert "200" in msg
        assert "ms" in msg

    def test_healthz_not_logged_at_info(self, caplog):
        app = self._make_app()
        with caplog.at_level(logging.INFO, logger="app.requests"):
            app.test_client().get("/healthz")
        assert not [r for r in caplog.records if r.name == "app.requests"]

    def test_healthz_logged_at_debug(self, caplog):
        app = self._make_app()
        with caplog.at_level(logging.DEBUG, logger="app.requests"):
            app.test_client().get("/healthz")
        assert [r for r in caplog.records if r.name == "app.requests"]

    def test_slow_request_logged_as_warning(self, caplog, monkeypatch):
        import panel_core.observability as obs

        monkeypatch.setattr(obs, "_SLOW_REQUEST_MS", 0.0)
        app = self._make_app()
        with caplog.at_level(logging.INFO, logger="app.requests"):
            app.test_client().get("/ping")
        records = [r for r in caplog.records if r.name == "app.requests"]
        assert records[0].levelno == logging.WARNING

    def test_query_string_is_included(self, caplog):
        app = self._make_app()
        with caplog.at_level(logging.INFO, logger="app.requests"):
            app.test_client().get("/ping?panel=local")
        msg = next(r for r in caplog.records if r.name == "app.requests").getMessage()
        assert "panel=local" in msg


class TestRunJobLogged:
    def test_logs_duration_at_info(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="app.jobs"):
            run_job_logged("test_job", 60, lambda: None)
        msgs = [r.getMessage() for r in caplog.records if r.name == "app.jobs"]
        assert any("test_job" in m and "done in" in m for m in msgs)

    def test_warns_when_overrunning_interval(self, caplog):
        def _slow():
            time.sleep(0.01)

        with caplog.at_level(logging.INFO, logger="app.jobs"):
            run_job_logged("test_job", 0.001, _slow)
        records = [r for r in caplog.records if r.name == "app.jobs"]
        assert records[0].levelno == logging.WARNING
        assert "overran" in records[0].getMessage()

    def test_exception_propagates_with_duration_log(self, caplog):
        def _boom():
            raise RuntimeError("kaput")

        with caplog.at_level(logging.INFO, logger="app.jobs"), pytest.raises(RuntimeError):
            run_job_logged("boom_job", 60, _boom)
        msgs = [r.getMessage() for r in caplog.records if r.name == "app.jobs"]
        assert any("boom_job" in m and "failed after" in m for m in msgs)


class TestSqlTiming:
    def test_slow_sql_warns(self, app, db, caplog, monkeypatch):
        from sqlalchemy import text
        import panel_core.extensions as ext

        monkeypatch.setattr(ext, "_SLOW_SQL_MS", 0.0)
        with caplog.at_level(logging.WARNING, logger="app.sql"):
            db.session.execute(text("SELECT 1")).scalar()
        assert any("slow SQL" in r.getMessage() for r in caplog.records if r.name == "app.sql")

    def test_statements_logged_at_debug(self, app, db, caplog):
        from sqlalchemy import text

        with caplog.at_level(logging.DEBUG, logger="app.sql"):
            db.session.execute(text("SELECT 1")).scalar()
        assert any("SELECT 1" in r.getMessage() for r in caplog.records if r.name == "app.sql")


class TestFederationTiming:
    def _resp(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {}
        resp.raise_for_status = MagicMock()
        return resp

    def test_success_logged_at_debug_with_duration(self, caplog):
        from panel_core.services.panel_proxy import FederationClient

        client = FederationClient("https://child.example.com", "tok")
        with caplog.at_level(logging.DEBUG, logger="panel_core.services.panel_proxy"):
            with patch.object(client._session, "get", return_value=self._resp()):
                client.snapshot()
        msgs = [r.getMessage() for r in caplog.records if r.name == "panel_core.services.panel_proxy"]
        assert any("federation" in m and "ms" in m for m in msgs)

    def test_failure_logged_as_warning(self, caplog):
        from panel_core.services.panel_proxy import FederationClient, RemotePanelError

        client = FederationClient("https://child.example.com", "tok")
        with caplog.at_level(logging.WARNING, logger="panel_core.services.panel_proxy"):
            with patch.object(client._session, "get", side_effect=requests.ConnectionError("boom")):
                with pytest.raises(RemotePanelError):
                    client.snapshot()
        msgs = [r.getMessage() for r in caplog.records if r.name == "panel_core.services.panel_proxy"]
        assert any("unreachable" in m for m in msgs)
