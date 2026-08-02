import pytest


@pytest.mark.parametrize(
    "value,expected",
    [
        ("worker", True),
        ("WORKER", True),
        ("  worker  ", True),
        ("master", False),
        ("", False),
        (None, False),
    ],
)
def test_is_worker(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("PANEL_ROLE", raising=False)
    else:
        monkeypatch.setenv("PANEL_ROLE", value)
    from panel_core.panel_role import is_worker

    assert is_worker() is expected


@pytest.mark.parametrize(
    "value,expected",
    [("sub", True), ("SUB", True), ("  sub  ", True), ("master", False), ("worker", False), ("", False), (None, False)],
)
def test_is_sub(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("PANEL_ROLE", raising=False)
    else:
        monkeypatch.setenv("PANEL_ROLE", value)
    from panel_core.panel_role import is_sub

    assert is_sub() is expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("bot", True),
        ("BOT", True),
        ("  bot  ", True),
        ("master", False),
        ("worker", False),
        ("sub", False),
        ("", False),
        (None, False),
    ],
)
def test_is_bot_api(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("PANEL_ROLE", raising=False)
    else:
        monkeypatch.setenv("PANEL_ROLE", value)
    from panel_core.panel_role import is_bot_api

    assert is_bot_api() is expected
