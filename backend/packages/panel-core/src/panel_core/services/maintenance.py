import fcntl
import os
import time
from contextlib import contextmanager

from flask import g, request

from panel_core.extensions import db


@contextmanager
def maintenance_operation(*, exclusive=False):
    if db.engine.dialect.name != "sqlite" or not db.engine.url.database:
        yield
        return
    path = f"{db.engine.url.database}.maintenance.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    lock = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        while True:
            try:
                fcntl.flock(descriptor, lock | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.01)
        yield
    finally:
        db.session.remove()
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def register_maintenance_hooks(app):
    @app.before_request
    def enter_operation():
        if request.endpoint == "backup.restore":
            return
        operation = maintenance_operation()
        operation.__enter__()
        g.node_maintenance_operation = operation

    @app.teardown_request
    def leave_operation(error):
        operation = g.pop("node_maintenance_operation", None)
        if operation is not None:
            operation.__exit__(None, None, None)
