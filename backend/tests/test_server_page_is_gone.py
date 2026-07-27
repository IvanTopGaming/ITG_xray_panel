import pathlib
import re


REPO = pathlib.Path(__file__).resolve().parents[2]

SUBSCRIPTION = REPO / "backend/packages/panel-sub/src/panel_core/api/subscription.py"

I18N = REPO / "frontend/packages/sub-page/src/lib/i18n.ts"

SUB_PAGE_SRC = REPO / "frontend/packages/sub-page/src"

RAW_HTML_DOC = (
    "The deleted page ran every interpolated value through html.escape(), and a test pinned that a "
    "brand of '<script>x</script>' came back escaped. JSX escapes children by default, so the "
    "invariant now holds structurally rather than by a call the page has to remember to make -- "
    "except through dangerouslySetInnerHTML / innerHTML / outerHTML / insertAdjacentHTML, which are "
    "the only ways back to the old failure. brand, node names and inbound tags on this page are all "
    "admin-controlled strings rendered on an unauthenticated surface, so one of those calls is a "
    "stored-XSS hole with no login in front of it."
)

RAW_HTML_SINKS = ("dangerouslySetInnerHTML", "innerHTML", "outerHTML", "insertAdjacentHTML")

GONE_DOC = (
    "The server-rendered page was replaced by the sub-page bundle, and the two must not coexist: two "
    "copies of the same markup and two RU/EN string tables drift apart silently, and the f-string "
    "version also carried an inline <script> that Caddy's CSP blocks outright, so its copy button was "
    "dead on any deployment behind the proxy."
)

MOVED_DOC = (
    "_PAGE_STRINGS moved into the bundle as STRINGS/MONTHS in sub-page/src/lib/i18n.ts. The move is "
    "the only reason these keys are allowed to leave Python, so the destination is what has to be "
    "guarded now: t() falls back to English and then to a visible '<key>' placeholder rather than "
    "failing the build, so a key dropped in the port shows up as mojibake on the one page a user "
    "opens when their access is already broken. There is no test runner in the frontend workspace "
    "(no vitest, no jest), so this Python-side scan is the only guard the repo can carry."
)

FORBIDDEN_NAMES = (
    "render_aggregate_subscription_page",
    "_PAGE_STRINGS",
    "_format_bytes",
    "_format_date_localized",
    "_format_expiry",
    "_pick_lang",
)

PORTED_KEYS = (
    "default_brand",
    "status_active",
    "status_disabled",
    "hero_title",
    "copy",
    "hint",
    "valid_until",
    "days_left",
    "expired",
    "never",
    "devices",
    "connected",
    "nodes",
    "of_gb",
    "almost",
    "unlimited",
    "until",
    "download",
    "auto_update",
    "no_nodes",
    "copied",
)

RU_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)

EN_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _string_keys(lang: str) -> set[str]:
    text = I18N.read_text(encoding="utf-8")
    body = text[text.index("const STRINGS") :]
    start = body.index(f"\n  {lang}: {{")
    end = body.index("\n  },", start)
    return {m.group(1) for m in re.finditer(r"^\s{4}(\w+):", body[start:end], re.M)}


def _months(lang: str) -> list[str]:
    text = I18N.read_text(encoding="utf-8")
    body = text[text.index("export const MONTHS") : text.index("const STRINGS")]
    start = body.index(f"{lang}: [")
    return re.findall(r"'([^']+)'", body[start : body.index("]", start)])


def test_the_server_rendered_page_is_gone():
    text = SUBSCRIPTION.read_text()
    left = [name for name in FORBIDDEN_NAMES if name in text]
    assert left == [], f"{left} still present in {SUBSCRIPTION.name}\n\n{GONE_DOC}"


def test_no_html_document_is_built_in_python():
    text = SUBSCRIPTION.read_text().lower()
    assert "<!doctype html>" not in text, GONE_DOC


def test_the_string_table_moved_to_the_bundle_with_both_languages():
    ru, en = _string_keys("ru"), _string_keys("en")
    assert len(ru) >= len(PORTED_KEYS), (
        f"only {len(ru)} keys parsed out of {I18N.name}'s ru table, fewer than the {len(PORTED_KEYS)} "
        f"that came over from Python -- the parser is broken and every other assertion here is "
        f"vacuous.\n\n{MOVED_DOC}"
    )
    assert ru == en, (
        f"ru-only keys {sorted(ru - en)}, en-only keys {sorted(en - ru)}. t() falls back to English "
        f"for a missing ru key and to the '<key>' placeholder for a missing en one.\n\n{MOVED_DOC}"
    )


def test_every_server_page_string_key_survived_the_move():
    for lang in ("ru", "en"):
        missing = sorted(key for key in PORTED_KEYS if key not in _string_keys(lang))
        assert missing == [], f"{lang}: {missing} dropped in the port\n\n{MOVED_DOC}"


def test_the_bundle_never_injects_raw_html():
    sources = sorted(path for path in SUB_PAGE_SRC.rglob("*") if path.suffix in (".ts", ".tsx") and path.is_file())
    assert len(sources) >= 5, (
        f"only {len(sources)} sub-page sources found under {SUB_PAGE_SRC} -- the package moved and "
        f"this scan would pass vacuously.\n\n{RAW_HTML_DOC}"
    )
    offenders = sorted(
        f"{path.relative_to(REPO)} -> {sink}"
        for path in sources
        for sink in RAW_HTML_SINKS
        if sink in path.read_text(encoding="utf-8")
    )
    assert offenders == [], "\n".join(offenders) + f"\n\n{RAW_HTML_DOC}"


def test_the_month_names_moved_across_in_full():
    assert _months("ru") == list(RU_MONTHS), MOVED_DOC
    assert _months("en") == list(EN_MONTHS), MOVED_DOC
