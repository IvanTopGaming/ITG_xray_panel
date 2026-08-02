import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
ENTRYPOINT = FRONTEND / "entrypoint.sh"
INDEX_HTMLS = sorted(FRONTEND.glob("packages/*/index.html"))

WORKFLOWS = (
    REPO / ".github" / "workflows" / "release.yml",
    REPO / ".github" / "workflows" / "dev-build.yml",
)
UI_PACKAGE_BUILD_ARG_RE = re.compile(r"--build-arg\s+UI_PACKAGE=([A-Za-z0-9._-]+)")

NGINX_DOC = (
    "entrypoint.sh runs inside the nginx image built by frontend/Dockerfile, and it rewrites exactly "
    "one file: /usr/share/nginx/html/index.html. So the sed-pattern check below is a statement about "
    "the shells that image serves, not about every index.html in the workspace. sub-page is the first "
    "shell that is not one of them -- panel-sub reads its bundle straight off disk and Flask returns "
    "the shell byte for byte, so it deliberately carries neither `<base href>` (vite's `base: './'` "
    "makes every asset path relative, which works on SUB_DOMAIN and on the PANEL_SECRET_PATH fallback "
    "alike) nor a panel-role meta tag (one server, one bundle, nothing to mismatch). "
    "The scoped set is DERIVED, never hand-listed: it is the UI_PACKAGE build-args the release and "
    "dev-build workflows pass to frontend/Dockerfile, which is the only way a package becomes an "
    "nginx-served image in the first place. A future nginx-served package therefore joins this scan "
    "the moment CI learns to build it, with no edit here. The exclusion is checked from both sides: "
    "test_a_shell_outside_the_nginx_image_carries_no_entrypoint_placeholder fails if an excluded "
    "shell contains an entrypoint placeholder anyway, which is what a package that IS nginx-served "
    "but missing from the workflows would look like."
)

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


def _nginx_served_package_names():
    names = set()
    for workflow in WORKFLOWS:
        assert workflow.is_file(), f"{workflow.relative_to(REPO)} does not exist\n\n{NGINX_DOC}"
        names.update(UI_PACKAGE_BUILD_ARG_RE.findall(workflow.read_text()))
    return sorted(names)


NGINX_SERVED_PACKAGES = _nginx_served_package_names()
NGINX_SERVED_SHELLS = [p for p in INDEX_HTMLS if p.parent.name in NGINX_SERVED_PACKAGES]
SHELLS_OUTSIDE_THE_NGINX_IMAGE = [p for p in INDEX_HTMLS if p.parent.name not in NGINX_SERVED_PACKAGES]


def test_the_nginx_served_set_is_derived_and_complete():
    assert NGINX_SERVED_PACKAGES, (
        f"no `--build-arg UI_PACKAGE=<name>` found in {[str(w.relative_to(REPO)) for w in WORKFLOWS]} -- "
        f"the derivation went dead and the sed-pattern check below would scan nothing.\n\n{NGINX_DOC}"
    )
    scanned = sorted(p.parent.name for p in NGINX_SERVED_SHELLS)
    assert scanned == NGINX_SERVED_PACKAGES, (
        f"CI builds nginx images for {NGINX_SERVED_PACKAGES}, but only {scanned} have an index.html "
        f"under {FRONTEND / 'packages'} -- the derivation and the workspace disagree.\n\n{NGINX_DOC}"
    )


def _shell_ids(paths):
    return [p.parent.name for p in paths]


@pytest.mark.parametrize("path", NGINX_SERVED_SHELLS, ids=_shell_ids(NGINX_SERVED_SHELLS))
def test_every_entrypoint_sed_pattern_occurs_in_the_shell(path):
    text = path.read_text()
    missing = [p for p in _sed_patterns() if p not in text]
    assert missing == [], (
        f"entrypoint.sh rewrites {missing!r}, which does not occur in {path.relative_to(REPO)} -- the "
        f"sed would exit 0 having changed nothing.\n\n{SHELL_DOC}\n\n{NGINX_DOC}"
    )


@pytest.mark.parametrize("path", SHELLS_OUTSIDE_THE_NGINX_IMAGE, ids=_shell_ids(SHELLS_OUTSIDE_THE_NGINX_IMAGE))
def test_a_shell_outside_the_nginx_image_carries_no_entrypoint_placeholder(path):
    text = path.read_text()
    present = [p for p in _sed_patterns() if p in text]
    assert present == [], (
        f"{path.relative_to(REPO)} carries the entrypoint placeholder(s) {present!r} but no workflow "
        f"builds an nginx image for {path.parent.name!r}, so nothing will ever rewrite them -- either "
        f"the placeholders are dead and must go, or the package really is nginx-served and the "
        f"workflows are missing its UI_PACKAGE build-arg.\n\n{NGINX_DOC}"
    )


@pytest.mark.parametrize("path", INDEX_HTMLS, ids=lambda p: p.parent.name)
def test_the_vite_dev_injection_needle_occurs_in_the_shell(path):
    config = path.parent / "vite.config.ts"
    if not config.is_file():
        pytest.skip(f"{config} has no vite config")
    text = config.read_text()
    if "transformIndexHtml" not in text:
        pytest.skip(f"{config} performs no transformIndexHtml replacement")
    match = VITE_NEEDLE_RE.search(text)
    assert match is not None, (
        f"{config.relative_to(REPO)} has a transformIndexHtml hook but no parseable html.replace() "
        f"needle -- the guard would silently vanish rather than fire.\n\n{SHELL_DOC}"
    )
    needle = _unescape(match.group("needle"))
    assert needle in path.read_text(), (
        f"{config.relative_to(REPO)} replaces {needle!r}, which does not occur in "
        f"{path.relative_to(REPO)} -- the dev server would serve an unconfigured shell.\n\n{SHELL_DOC}"
    )
