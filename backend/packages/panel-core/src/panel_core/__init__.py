import gevent.monkey

gevent.monkey.patch_all()

from panel_core.pg_compat import patch_gevent_psycopg

patch_gevent_psycopg()

from .panel_role import is_worker, is_sub, is_bot_api


def create_app():
    if is_sub():
        from panel_core.roles import sub

        return sub.create_app()
    if is_bot_api():
        from panel_core.roles import botapi

        return botapi.create_app()
    if is_worker():
        from panel_core.roles import worker

        return worker.create_app()

    from panel_core.roles import master

    return master.create_app()
