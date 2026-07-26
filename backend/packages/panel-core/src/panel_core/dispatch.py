from panel_core.panel_role import is_worker, is_sub, is_bot_api


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
