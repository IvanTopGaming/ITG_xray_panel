import pathlib
import re


REPO = pathlib.Path(__file__).resolve().parents[2]

DEEPLINKS = REPO / "frontend/packages/sub-page/src/lib/deeplinks.ts"

I18N = REPO / "frontend/packages/sub-page/src/lib/i18n.ts"

SCHEME_DOC = (
    "A broken deep link on this page does not look broken -- it looks like the user's fault, which is "
    "worse than a silent failure. AppButtons.open() sets window.location.href to the scheme and starts "
    "a 1200ms timer; if the tab never backgrounds it renders 'Looks like {app} is not installed.' plus "
    "an install link. A one-character typo in a scheme produces exactly that message on a phone that "
    "has the app installed, and the install link the page then offers cannot fix it. The user retries, "
    "support retries, and the real cause is invisible from both ends. Nothing else in the repo pins "
    "these strings: they are template literals inside an arrow function, so tsc, ESLint and the vite "
    "build are all equally happy with any typo that stays syntactically a string.\n\n"
    "Adding a fourth app is a deliberate edit here, not an oversight -- pin its scheme in this table "
    "at the same time, or it ships with no guard at all."
)

RAW_URL_DOC = (
    "The subscription URL is embedded RAW on purpose -- do NOT 'fix' this by wrapping it in "
    "encodeURIComponent, and do not let a linter or a well-meaning refactor add it back. Every one of "
    "these vendors documents and expects the unencoded form. Hiddify is the case that proves it: its "
    "Dart handler reads the subscription URL as uri.path.substring(1), which does no percent-decoding "
    "at all, so an encoded URL arrives with its %3A / %2F escapes intact and the import fails on a URL "
    "that merely looks plausible in a log. The failure then presents as the not-installed message "
    "described above, so encoding the URL does not produce an encoding-shaped bug report -- it "
    "produces an 'app is broken' one."
)

SUB_APP_SCHEMES = (
    ("happ", "happ://add/"),
    ("v2raytun", "v2raytun://import/"),
    ("hiddify", "hiddify://import/"),
)

ENTRY_RE = re.compile(r"\bid:\s*'([^']+)'.*?scheme:\s*\(subUrl\)\s*=>\s*`([^`]*)`", re.DOTALL)

PLACEHOLDER_DOC = (
    "t() substitutes {n} / {h} / {app} by a literal String.replace of the braced token, so a value "
    "that loses its placeholder does not throw, does not warn and does not fail the build -- it "
    "renders the sentence with the number missing ('days left' with no count) or, for a renamed "
    "token, prints the brace form verbatim to the user. The existing string-table guards in "
    "test_server_page_is_gone.py pin key names and month values, which means a key can keep its name "
    "and both languages while quietly losing the only part of it that carries data. These are the "
    "three values in the table that interpolate anything."
)

PLACEHOLDERS = (
    ("days_left", "{n}"),
    ("auto_update", "{h}"),
    ("not_installed", "{app}"),
)


def _entries():
    return ENTRY_RE.findall(DEEPLINKS.read_text(encoding="utf-8"))


def _string_value(lang: str, key: str) -> str | None:
    text = I18N.read_text(encoding="utf-8")
    body = text[text.index("const STRINGS") :]
    start = body.index(f"\n  {lang}: {{")
    end = body.index("\n  },", start)
    match = re.search(rf"\b{key}:\s*'((?:[^'\\]|\\.)*)'", body[start:end])
    return match.group(1) if match else None


def test_every_deep_link_scheme_is_pinned_exactly():
    entries = _entries()
    assert [app_id for app_id, _ in entries] == [app_id for app_id, _ in SUB_APP_SCHEMES], (
        f"deeplinks.ts declares apps {[app_id for app_id, _ in entries]}, this guard pins "
        f"{[app_id for app_id, _ in SUB_APP_SCHEMES]}. If the parser matched nothing the whole file "
        f"is vacuous; if an app was added or renamed, pin it.\n\n{SCHEME_DOC}"
    )
    for (app_id, template), (pinned_id, prefix) in zip(entries, SUB_APP_SCHEMES):
        expected = prefix + "${subUrl}"
        assert template == expected, (
            f"{app_id}: scheme builds {template!r}, pinned as {expected!r} -- check the scheme name, "
            f"the '://' and the trailing slash character by character.\n\n{SCHEME_DOC}"
        )


def test_the_subscription_url_is_embedded_without_percent_encoding():
    text = DEEPLINKS.read_text(encoding="utf-8")
    assert "encodeURIComponent" not in text, (
        f"{DEEPLINKS.name} now percent-encodes the subscription URL.\n\n{RAW_URL_DOC}"
    )


def test_every_interpolating_string_keeps_its_placeholder():
    for lang in ("ru", "en"):
        for key, placeholder in PLACEHOLDERS:
            value = _string_value(lang, key)
            assert value, (
                f"{lang}.{key} did not parse out of {I18N.name} -- either the key is gone or the "
                f"parser is broken, and every other assertion here is vacuous.\n\n{PLACEHOLDER_DOC}"
            )
            assert placeholder in value, (
                f"{lang}.{key} = {value!r} lost its {placeholder} placeholder\n\n{PLACEHOLDER_DOC}"
            )
