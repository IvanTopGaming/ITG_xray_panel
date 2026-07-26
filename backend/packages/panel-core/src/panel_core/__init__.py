from panel_core.bootstrap import bootstrap_gevent

bootstrap_gevent()

from panel_core.dispatch import create_app  # noqa: E402,F401 — re-exported for existing importers
