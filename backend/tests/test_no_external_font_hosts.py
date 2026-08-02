import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]

FORBIDDEN = ("fonts.googleapis.com", "fonts.gstatic.com")

SKIP_DIRS = {".git", "node_modules", "dist", "docs", ".venv", ".superpowers", ".pytest_cache", ".impeccable"}
SCAN_SUFFIXES = {".html", ".css", ".ts", ".tsx", ".js", ".jsx", ".go", ".py", ".sh", ".yml", ".yaml", ".md"}

SELF_REFERENTIAL = {
    "backend/tests/test_no_external_font_hosts.py",
    "caddy/caddygen/generate_test.go",
}

FONT_DOC = (
    "The subscription page is opened precisely when the user's access is already failing, and the panel "
    "shell is opened from the same networks -- sending either browser to Google for a font is both a "
    "reliability bug and a third-party request from the user's device. Roboto and Roboto Mono are "
    "self-hosted in ui-core/src/fonts, so no source file may name a Google font host again. This scan "
    "includes caddy/caddygen/generate.go on purpose: the CSP allowances for those two hosts are the "
    "part most likely to be left behind, and leaving them would quietly re-permit the request the "
    "self-hosting was meant to remove. SELF_REFERENTIAL lists the only two files that must name the "
    "forbidden hosts in order to forbid them -- this guard and the Go test asserting the same thing "
    "about the CSP. Nothing else belongs in it."
)


def _scan_files():
    files = []
    for path in REPO.rglob("*"):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        relative = path.relative_to(REPO)
        if SKIP_DIRS & set(relative.parts):
            continue
        if relative.as_posix() in SELF_REFERENTIAL:
            continue
        files.append(path)
    assert len(files) > 200, (
        f"only {len(files)} files scanned under {REPO} -- the scan lost its way and would pass vacuously.\n\n{FONT_DOC}"
    )
    return files


def test_no_source_file_references_a_google_font_host():
    offenders = []
    for path in _scan_files():
        text = path.read_text(errors="ignore")
        for token in FORBIDDEN:
            if token in text:
                offenders.append(f"{path.relative_to(REPO)}: {token}")
    assert offenders == [], "\n".join(sorted(offenders)) + f"\n\n{FONT_DOC}"
