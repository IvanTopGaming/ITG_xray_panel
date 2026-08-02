from importlib.resources import files

DATA_PACKAGE = "panel_core.data"

BOT_TEXTS_DEFAULTS = "bot_texts_defaults.yaml"
BOT_TEXTS_META = "bot_texts_meta.yaml"


def data_file(name):
    return files(DATA_PACKAGE).joinpath(name)


def read_data_text(name):
    resource = data_file(name)
    if not resource.is_file():
        return None
    return resource.read_text(encoding="utf-8")
