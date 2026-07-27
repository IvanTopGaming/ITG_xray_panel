import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
ENTRYPOINT = FRONTEND / "entrypoint.sh"
INDEX_HTMLS = sorted(FRONTEND.glob("packages/*/index.html"))

SHELL_DOC = (
    "Caddy serves every terminated route with `script-src 'self'` (caddy/caddygen/generate.go), which "
    "blocks inline <script> and inline event handlers outright -- verified in Chromium against that "
    "exact header. An index.html that smuggles configuration through an inline script therefore reaches "
    "the browser with that configuration silently missing: window.__PANEL_ROLE__ came out undefined, "
    "assertPanelRole resolved it to 'master', and the node bundle -- which expects 'worker' -- wiped the "
    "page to a red error box. The admin bundle survived only because 'master' is what the empty fallback "
    "happens to produce. Configuration for the shell travels in a <meta> tag, never in a script. "
    "The sed-pattern check below guards the other half of the same failure: a sed whose left-hand side "
    "no longer occurs in the file exits 0 and changes nothing, so a renamed placeholder degrades to the "
    "same silently-unconfigured shell with no build or test failure anywhere."
)

INLINE_SCRIPT_RE = re.compile(
    r"<script(?![^>]*\bsrc=)[^>]*>(?P<body>.*?)</script>",
    re.DOTALL | re.IGNORECASE,
)
SED_LHS_RE = re.compile(r"""sed\s+-i\s+"s\|(?P<lhs>(?:[^|\\]|\\.)*)\|""")
VITE_NEEDLE_RE = re.compile(r"""html\.replace\(\s*(['"])(?P<needle>(?:\\.|(?!\1).)*)\1""", re.DOTALL)


def _unescape(value):
    return value.replace('\\"', '"')


def test_the_scan_actually_finds_the_shells():
    assert len(INDEX_HTMLS) >= 2, (
        f"expected at least the admin and node index.html under {FRONTEND / 'packages'}, found "
        f"{[str(p) for p in INDEX_HTMLS]} -- the workspace layout moved and this guard would pass "
        f"vacuously.\n\n{SHELL_DOC}"
    )


@pytest.mark.parametrize("path", INDEX_HTMLS, ids=lambda p: p.parent.name)
def test_no_inline_script_in_the_html_shell(path):
    bodies = [m.group("body").strip() for m in INLINE_SCRIPT_RE.finditer(path.read_text())]
    non_empty = [b for b in bodies if b]
    assert non_empty == [], (
        f"{path.relative_to(REPO)} carries an inline <script> body {non_empty!r}, which Caddy's CSP "
        f"blocks.\n\n{SHELL_DOC}"
    )


def _sed_patterns():
    patterns = [_unescape(m.group("lhs")) for m in SED_LHS_RE.finditer(ENTRYPOINT.read_text())]
    assert patterns, f'no `sed -i "s|...|...|"` found in {ENTRYPOINT}\n\n{SHELL_DOC}'
    return patterns


@pytest.mark.parametrize("path", INDEX_HTMLS, ids=lambda p: p.parent.name)
def test_every_entrypoint_sed_pattern_occurs_in_the_shell(path):
    text = path.read_text()
    missing = [p for p in _sed_patterns() if p not in text]
    assert missing == [], (
        f"entrypoint.sh rewrites {missing!r}, which does not occur in {path.relative_to(REPO)} -- the "
        f"sed would exit 0 having changed nothing.\n\n{SHELL_DOC}"
    )


@pytest.mark.parametrize("path", INDEX_HTMLS, ids=lambda p: p.parent.name)
def test_the_vite_dev_injection_needle_occurs_in_the_shell(path):
    config = path.parent / "vite.config.ts"
    if not config.is_file():
        pytest.skip(f"{config} has no vite config")
    match = VITE_NEEDLE_RE.search(config.read_text())
    if match is None:
        pytest.skip(f"{config} performs no transformIndexHtml replacement")
    needle = _unescape(match.group("needle"))
    assert needle in path.read_text(), (
        f"{config.relative_to(REPO)} replaces {needle!r}, which does not occur in "
        f"{path.relative_to(REPO)} -- the dev server would serve an unconfigured shell.\n\n{SHELL_DOC}"
    )
