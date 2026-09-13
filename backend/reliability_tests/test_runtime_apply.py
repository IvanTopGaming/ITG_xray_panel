import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from flask import Flask

from panel_core.extensions import db
from panel_core.models import SystemSetting


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    from panel_core.services import runtime_apply

    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{tmp_path / 'state.sqlite'}",
        XRAY_CONFIG_LOCK_PATH=str(tmp_path / "config.lock"),
    )
    db.init_app(app)
    monkeypatch.setattr(runtime_apply, "generate_config_file", lambda: None)
    monkeypatch.setattr(runtime_apply, "restart_xray_container", lambda: None)
    with app.app_context():
        db.create_all()
        yield app, runtime_apply
        db.session.remove()
        db.drop_all()


def state():
    from panel_core.models import RuntimeApplyState

    return db.session.get(RuntimeApplyState, 1, populate_existing=True)


def test_dirty_marker_rolls_back_with_desired_mutation(runtime):
    app, service = runtime
    db.session.add(SystemSetting(key="change", value="not committed"))
    service.mark_runtime_dirty()
    db.session.rollback()
    assert db.session.get(SystemSetting, "change") is None
    assert state() is None or state().desired_revision == 0


def test_failed_apply_is_retried_without_another_mutation(runtime, monkeypatch):
    app, service = runtime
    db.session.add(SystemSetting(key="change", value="saved"))
    revision = service.mark_runtime_dirty()
    db.session.commit()

    def fail():
        raise RuntimeError("offline")

    monkeypatch.setattr(service, "restart_xray_container", fail)
    with pytest.raises(service.RuntimeApplyError):
        service.synchronize_runtime(expected_revision=revision)
    assert db.session.get(SystemSetting, "change").value == "saved"
    assert state().desired_revision == revision
    assert state().applied_revision < revision
    assert "offline" in state().last_error
    monkeypatch.setattr(service, "restart_xray_container", lambda: None)
    assert service.retry_pending_runtime()
    assert state().applied_revision == revision
    assert state().last_error == ""


def test_dirty_state_after_commit_is_recovered_by_new_app_context(runtime):
    app, service = runtime
    revision = service.mark_runtime_dirty()
    db.session.commit()
    with app.app_context():
        assert service.retry_pending_runtime()
        assert state().applied_revision == revision


def test_old_pending_revision_forces_full_reload(runtime, monkeypatch):
    app, service = runtime
    service.mark_runtime_dirty()
    db.session.commit()
    revision = service.mark_runtime_dirty()
    db.session.commit()
    actions = []
    monkeypatch.setattr(service, "restart_xray_container", lambda: actions.append("full"))
    service.synchronize_runtime(lambda: actions.append("partial"), expected_revision=revision)
    assert actions == ["full"]
    assert state().applied_revision == revision


def test_single_clean_change_may_use_partial_apply(runtime, monkeypatch):
    app, service = runtime
    revision = service.mark_runtime_dirty()
    db.session.commit()
    actions = []
    monkeypatch.setattr(service, "restart_xray_container", lambda: actions.append("full"))
    service.synchronize_runtime(lambda: actions.append("partial"), expected_revision=revision)
    assert actions == ["partial"]
    assert state().applied_revision == revision


def test_older_apply_cannot_clear_a_newer_revision(runtime, monkeypatch):
    app, service = runtime
    revision = service.mark_runtime_dirty()
    db.session.commit()

    def mutate_again():
        service.mark_runtime_dirty()
        db.session.commit()

    service.synchronize_runtime(mutate_again, expected_revision=revision)
    assert state().desired_revision == revision + 1
    assert state().applied_revision < state().desired_revision
    service.retry_pending_runtime()
    assert state().applied_revision == revision + 1


def test_nested_lock_still_excludes_other_processes(runtime):
    app, service = runtime
    code = "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
    with service.runtime_lock():
        with service.runtime_lock():
            result = subprocess.run(
                [sys.executable, "-c", code, app.config["XRAY_CONFIG_LOCK_PATH"]], capture_output=True, timeout=3
            )
            assert result.returncode != 0
    result = subprocess.run(
        [sys.executable, "-c", code, app.config["XRAY_CONFIG_LOCK_PATH"]], capture_output=True, timeout=3
    )
    assert result.returncode == 0


def test_concurrent_mutations_keep_every_revision(runtime):
    app, service = runtime

    def mutate(number):
        with app.app_context(), service.runtime_lock():
            db.session.add(SystemSetting(key=f"change-{number}", value="saved"))
            revision = service.mark_runtime_dirty()
            db.session.commit()
            service.synchronize_runtime(expected_revision=revision)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(mutate, range(8)))
    assert state().desired_revision == 8
    assert state().applied_revision == 8
    assert SystemSetting.query.count() == 8


def test_gevent_workers_do_not_inherit_each_others_lock(runtime):
    app, service = runtime
    script = """
from gevent import monkey
monkey.patch_all()
import gevent
import sys
from flask import Flask
from panel_core.services.runtime_apply import runtime_lock
app = Flask(__name__)
app.config['XRAY_CONFIG_LOCK_PATH'] = sys.argv[1]
events = []
def change(name):
    with app.app_context(), runtime_lock():
        events.append(name + ' start')
        gevent.sleep(0.03)
        events.append(name + ' end')
gevent.joinall([gevent.spawn(change, 'first'), gevent.spawn(change, 'second')], raise_error=True)
assert events == ['first start', 'first end', 'second start', 'second end'], events
"""
    result = subprocess.run(
        [sys.executable, "-c", script, app.config["XRAY_CONFIG_LOCK_PATH"]], capture_output=True, timeout=10
    )
    assert result.returncode == 0, result.stderr.decode()
