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


WORKSPACE_PREFIX = "@panel/"

WORKSPACE_DOC = (
    "npm workspaces symlinks every packages/* member into frontend/node_modules/@panel/<name> "
    "regardless of what any package.json declares as a dependency -- with moduleResolution: bundler "
    "and no package.json `exports` field, a deep import through that symlink (`@panel/admin/src/"
    "pages/Panels`) resolves, typechecks and bundles exactly like the relative-path escape, and is "
    "arguably the more likely mistake of the two: `@panel/ui-core/lib/api` is the idiomatic monorepo "
    "specifier and what an editor's auto-import offers. resolve_import has to follow it into the "
    "same packages/<name>/src tree that `@panel/<name>` symlinks to, whether or not the specifier "
    "itself spells out the `src/` segment -- the real symlink target is the package root, so both "
    "`@panel/admin/src/pages/Panels` (subpath includes `src/`, matching the reviewer's reproduction) "
    "and `@panel/ui-core/lib/api` (subpath omits it, matching the idiomatic form) must land on the "
    "same file that `@ui/lib/api` already resolves to."
)


def resolve_import(importer_path, specifier):
    if specifier.startswith("@ui/"):
        base = PACKAGE_ROOTS["ui-core"] / specifier[len("@ui/") :]
    elif specifier.startswith(WORKSPACE_PREFIX):
        rest = specifier[len(WORKSPACE_PREFIX) :]
        package_name, _, subpath = rest.partition("/")
        if package_name not in PACKAGE_ROOTS:
            return None
        if subpath.startswith("src/"):
            subpath = subpath[len("src/") :]
        base = PACKAGE_ROOTS[package_name] / subpath if subpath else PACKAGE_ROOTS[package_name]
    elif specifier.startswith("@/"):
        importer_package = package_for(importer_path)
        base = PACKAGE_ROOTS[importer_package] / specifier[len("@/") :]
    elif specifier.startswith("."):
        base = (importer_path.parent / specifier).resolve()
    else:
        return None
    return _resolve_file(base)


def specifier_form(specifier):
    if specifier.startswith("@ui/"):
        return "@ui/"
    if specifier.startswith(WORKSPACE_PREFIX):
        return WORKSPACE_PREFIX
    if specifier.startswith("@/"):
        return "@/"
    if specifier.startswith("."):
        return "relative"
    return None
