from tests.import_graph import (
    HEAVY_ROOTS_DOC,
    SRC,
    XRAY_HEAVY_MODULES,
    function_level_imports,
    heavy_import_chain,
)

ALLOWED_LAZY_HEAVY_FILES = {
    "dispatch.py": (
        "the role dispatcher: create_app() imports panel_core.roles.worker only when PANEL_ROLE=worker, "
        "which is exactly how the light roles avoid the heavy stack"
    ),
    "xray/gateway.py": (
        "the seam itself: LocalXrayGateway imports panel_core.xray.{engine,grpc_client} per call, and "
        "imports the module rather than the function so the ~125 patch sites keep working"
    ),
}


def _sources():
    paths = sorted(SRC.rglob("*.py"))
    assert paths, (
        f"no python sources found under {SRC} — this guard would pass vacuously. "
        "If panel_core moved, point tests/import_graph.SRC at its new location."
    )
    return paths


def _lazy_heavy_hits():
    hits = []
    for path in _sources():
        relative = path.relative_to(SRC).as_posix()
        for function, modules in sorted(function_level_imports(path).items()):
            for module in sorted(modules):
                chain = heavy_import_chain(module, extra=XRAY_HEAVY_MODULES)
                if chain is not None:
                    hits.append((relative, f"{relative}::{function} -> " + " -> ".join(chain)))
    return hits


HINT = (
    "A function-scope (lazy) import reaches the heavy stack. Module-level guards cannot see these — "
    "tests/import_graph.module_level_imports skips function bodies on purpose — which is how "
    "services/provisioning.py survived two reviews while lazily importing a worker module. "
    f"{HEAVY_ROOTS_DOC} Only these files may hold a lazy heavy import, each for a stated reason:\n"
    + "\n".join(f"  {name}: {reason}" for name, reason in sorted(ALLOWED_LAZY_HEAVY_FILES.items()))
    + "\nIf you are adding a file here, say why in ALLOWED_LAZY_HEAVY_FILES — a lazy import is a "
    "deliberate seam, not a way to dodge the layering guard."
)


def test_lazy_heavy_imports_stay_inside_the_declared_seams():
    offenders = sorted(hit for path, hit in _lazy_heavy_hits() if path not in ALLOWED_LAZY_HEAVY_FILES)
    assert offenders == [], "\n".join(offenders) + f"\n\n{HINT}"


def test_the_declared_lazy_seams_still_exist():
    for name in ALLOWED_LAZY_HEAVY_FILES:
        assert (SRC / name).is_file(), (
            f"{name} is in ALLOWED_LAZY_HEAVY_FILES but no longer exists. After a package cut the entry "
            "must follow the file to its new path, otherwise the allowlist silently protects nothing and "
            "the real seam starts failing this guard."
        )


def test_the_known_lazy_seams_are_actually_exercised():
    seams = {path for path, _ in _lazy_heavy_hits()}
    assert seams == set(ALLOWED_LAZY_HEAVY_FILES), (
        f"lazy heavy imports live in {sorted(seams)}, but the allowlist declares "
        f"{sorted(ALLOWED_LAZY_HEAVY_FILES)}. An allowlist entry with nothing behind it means the guard "
        "is no longer looking where it thinks it is — re-check the import graph before relaxing this."
    )
