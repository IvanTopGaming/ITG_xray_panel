from tests.frontend_import_graph import (
    PACKAGE_ROOTS,
    VACUITY_DOC,
    import_specifiers,
    iter_frontend_sources,
    package_for,
    relative_source_name,
    resolve_import,
)

DIRECTION_DOC = (
    "The frontend workspace splits into three packages so that admin-only code is physically absent "
    "from the node image: ui-core is the shared surface, admin is the master-only app, node is the "
    "thin router shell that ships alongside ui-core in the node image. The allowed direction is "
    "one-way and it is not enforced by the TypeScript path aliases alone -- `@ui/*` only covers the "
    "alias form. A relative specifier such as `../../admin/src/pages/Panels` reaches straight across "
    "the package boundary without ever touching an alias, which is exactly the import an ESLint/tsc/"
    "vite pass has no reason to reject: it resolves, it typechecks, and it bundles. Allowed edges: "
    "admin -> ui-core, node -> ui-core. Forbidden: ui-core -> admin, ui-core -> node, admin -> node, "
    "node -> admin. ui-core must depend on nothing but itself, because it is what both images share; "
    "admin and node must never import each other directly, because that is exactly how a master-only "
    "page (or a node-only one) would ship into the wrong image without any alias ever admitting it."
)

ALLOWED_EDGES = frozenset({("admin", "ui-core"), ("node", "ui-core")})


def _all_sources():
    paths = iter_frontend_sources()
    assert len(paths) > 30, (
        f"only {len(paths)} frontend source files found under {sorted(PACKAGE_ROOTS)} -- the workspace "
        f"layout moved (or every source file was deleted) and this guard would pass vacuously.\n\n{VACUITY_DOC}"
    )
    return paths


def _cross_package_edges():
    edges = set()
    resolved_count = 0
    for path in _all_sources():
        owner = package_for(path)
        for specifier in import_specifiers(path):
            target = resolve_import(path, specifier)
            if target is None:
                continue
            resolved_count += 1
            target_owner = package_for(target)
            if target_owner is None or target_owner == owner:
                continue
            edges.add((owner, relative_source_name(path), specifier, target_owner))
    assert resolved_count > 0, (
        "zero import specifiers resolved to a real path across every frontend source file -- the "
        f"resolver is broken and this guard would pass vacuously on an empty edge set.\n\n{VACUITY_DOC}"
    )
    return edges


CROSS_PACKAGE_EDGES = _cross_package_edges()


def test_cross_package_edges_are_actually_detected():
    detected = {(owner, target) for owner, _, _, target in CROSS_PACKAGE_EDGES}
    assert ("admin", "ui-core") in detected, (
        "no admin -> ui-core import edge was resolved, which cannot be true: packages/admin/src/App.tsx "
        "imports @ui/stores/authStore. The edge scan is broken, so the direction test below proves nothing."
    )
    assert ("node", "ui-core") in detected, (
        "no node -> ui-core import edge was resolved, which cannot be true: packages/node/src/App.tsx "
        "imports @ui/stores/authStore. The edge scan is broken, so the direction test below proves nothing."
    )


def test_no_package_imports_outside_the_allowed_direction():
    offenders = sorted(
        f"{source} imports {specifier!r} (ships from {target})"
        for owner, source, specifier, target in CROSS_PACKAGE_EDGES
        if (owner, target) not in ALLOWED_EDGES
    )
    assert offenders == [], f"undeclared cross-package imports: {offenders}\n\n{DIRECTION_DOC}"
