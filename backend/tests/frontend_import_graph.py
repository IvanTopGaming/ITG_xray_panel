import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]

FRONTEND_PACKAGES = REPO / "frontend" / "packages"

SOURCE_SUFFIXES = (".ts", ".tsx")

IMPORT_RE = re.compile(
    r"""(?:from\s+|import\s*\(\s*|^\s*import\s+)['"]([^'"]+)['"]""",
    re.MULTILINE,
)

VACUITY_DOC = (
    "This scan anchors on frontend/packages/*/src, not on the frontend/ tree as a whole -- if the "
    "workspace layout moves or every source file is deleted the guard must fail loudly instead of "
    "silently checking zero files, which is the exact way the ui-core/admin direction bug on the "
    "backend side would have gone unnoticed if test_distribution_imports.py trusted an empty scan."
)


def _package_roots():
    roots = {}
    for path in sorted(FRONTEND_PACKAGES.glob("*/src")):
        if path.is_dir():
            roots[path.parent.name] = path
    assert roots, f"no packages/*/src directory found under {FRONTEND_PACKAGES}\n\n{VACUITY_DOC}"
    return roots


PACKAGE_ROOTS = _package_roots()


def iter_frontend_sources(package=None):
    roots = [PACKAGE_ROOTS[package]] if package else list(PACKAGE_ROOTS.values())
    paths = []
    for root in roots:
        for suffix in SOURCE_SUFFIXES:
            paths.extend(root.rglob(f"*{suffix}"))
    return sorted(set(paths))


def package_for(path):
    path = path.resolve()
    for name, root in PACKAGE_ROOTS.items():
        if path == root or root in path.parents:
            return name
    return None


def relative_source_name(path):
    path = path.resolve()
    name = package_for(path)
    return f"{name}:{path.relative_to(PACKAGE_ROOTS[name]).as_posix()}"


def import_specifiers(path):
    return IMPORT_RE.findall(path.read_text())


def _resolve_file(base):
    if base.suffix in SOURCE_SUFFIXES and base.is_file():
        return base
    if base.suffix == "":
        for suffix in SOURCE_SUFFIXES:
            candidate = base.with_suffix(suffix)
            if candidate.is_file():
                return candidate
        for suffix in SOURCE_SUFFIXES:
            candidate = base / f"index{suffix}"
            if candidate.is_file():
                return candidate
    return None


def resolve_import(importer_path, specifier):
    if specifier.startswith("@ui/"):
        base = PACKAGE_ROOTS["ui-core"] / specifier[len("@ui/") :]
    elif specifier.startswith("@/"):
        importer_package = package_for(importer_path)
        base = PACKAGE_ROOTS[importer_package] / specifier[len("@/") :]
    elif specifier.startswith("."):
        base = (importer_path.parent / specifier).resolve()
    else:
        return None
    return _resolve_file(base)
