"""§8.12: four details deferred out of phase 6, all on the page a paying user actually opens.

Each is small and each fails silently, which is why they survived four phases. Textual guards,
because the bundle has no test runner in this repo and the properties are single attributes that get
reverted one at a time.

Two of the original five items are moot rather than done: "master and worker answer 503 without
logging it" stopped existing in wave 3b, when both roles stopped serving the subscription surface at
all.
"""

from __future__ import annotations

import pathlib
import re

import pytest


SUB_PAGE = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "packages" / "sub-page"


def _read(relative):
    path = SUB_PAGE / relative
    assert path.is_file(), f"{relative} moved — this guard is stale"
    return path.read_text()


def test_the_page_says_something_without_javascript():
    """It is a bundle: with scripts off the user gets a blank page and no idea why.

    The fallback also tells them the thing that is actually useful — the URL in the address bar *is*
    the subscription, so a client app can be fed by hand without this page working at all.
    """

    html = _read("index.html")
    assert "<noscript>" in html, "a user with JavaScript off sees an empty document and no explanation"
    assert "JavaScript" in html
    assert html.count("address bar") + html.count("адресной строки") >= 2, (
        "the fallback does not tell the user their subscription URL is right there in the address bar, "
        "which is the one thing that still works when the page does not."
    )


def test_the_import_buttons_stay_reachable_by_keyboard():
    """`disabled` drops a control out of the tab order entirely.

    On desktop these buttons cannot work — the deep links are mobile-only — and the page explains
    that in a sentence underneath. A keyboard or screen-reader user was the one person who never
    reached either the buttons or the explanation.
    """

    source = _read("src/components/AppButtons.tsx")
    assert "aria-disabled={isDesktop}" in source, "the desktop state is expressed with `disabled` again"
    assert re.search(r"(?<![\w-])disabled=\{isDesktop\}", source) is None, (
        "the native `disabled` attribute is back, which takes the buttons out of the tab order again"
    )
    assert "aria-describedby" in source and "apps-desktop-hint" in source, (
        "the buttons are focusable but nothing ties them to the sentence that explains why they do nothing"
    )
    assert "!isDesktop && open(app)" in source, (
        "aria-disabled is advisory — the handler has to refuse the press itself, or the button is now "
        "merely labelled as disabled while still firing."
    )


def test_the_not_installed_hint_clears_itself():
    """It was cleared only by the next press, and on a desktop there is no next press."""

    source = _read("src/components/AppButtons.tsx")
    assert "MISSING_VISIBLE_MS" in source, "the 'app is not installed' line still stays until the page is reloaded"
    assert "setMissing(null)" in source


@pytest.mark.parametrize("lang", ("ru", "en"))
def test_the_qr_has_an_accessible_name(lang):
    qr = _read("src/components/QrPanel.tsx")
    assert 'role="img"' in qr and "aria-label" in qr, "the QR is an unlabelled graphic to a screen reader"
    assert "qr_alt" in qr

    i18n = _read("src/lib/i18n.ts")
    assert "qr_alt:" in i18n
    assert i18n.count("qr_alt:") == 2, f"qr_alt is missing from one of the two languages (checking {lang})"
