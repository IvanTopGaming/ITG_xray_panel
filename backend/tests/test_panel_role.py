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
    from app.panel_role import is_worker

    assert is_worker() is expected
