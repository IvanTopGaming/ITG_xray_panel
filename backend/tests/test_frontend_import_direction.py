from tests.frontend_import_graph import (
    PACKAGE_ROOTS,
    VACUITY_DOC,
    WORKSPACE_DOC,
    WORKSPACE_PREFIX,
    import_specifiers,
    iter_frontend_sources,
    package_for,
    relative_source_name,
    resolve_import,
    specifier_form,
)

DIRECTION_DOC = (
    "The frontend workspace splits into three packages so that admin-only code is physically absent "
    "from the node image: ui-core is the shared surface, admin is the master-only app, node is the "
    "thin router shell that ships alongside ui-core in the node image. The allowed direction is "
    "one-way and it is not enforced by the TypeScript path aliases alone -- `@ui/*` only covers the "
    "alias form. A relative specifier such as `../../admin/src/pages/Panels` reaches straight across "
    "the package boundary without ever touching an alias, which is exactly the import an ESLint/tsc/"
    "vite pass has no reason to reject: it resolves, it typechecks, and it bundles. So does the "
    "`@panel/<name>/...` workspace form -- see WORKSPACE_DOC. Allowed edges: admin -> ui-core, "
    "node -> ui-core. Forbidden: ui-core -> admin, ui-core -> node, admin -> node, node -> admin. "
    "ui-core must depend on nothing but itself, because it is what both images share; admin and node "
    "must never import each other directly, because that is exactly how a master-only page (or a "
    "node-only one) would ship into the wrong image without any alias ever admitting it. "
    "sub-page is the fourth package: it ships into the panel-sub image, not into either frontend image, "
    "so it may reach ui-core and nothing else -- an import of admin or node from it would bake "
    "master-only or node-only code into the subscription service.\n\n"
    + WORKSPACE_DOC
    + "\n\nThis guard fires on `import type` and type-only `export ... from` specifiers too, on "
    "purpose: a design-boundary guard should not special-case a transpiler flag, and a type that "
    "genuinely needs to cross the boundary belongs hoisted into ui-core/lib/types.ts, not imported "
    "past the guard because it happens to erase at build time."
)

ALLOWED_EDGES = frozenset({("admin", "ui-core"), ("node", "ui-core"), ("sub-page", "ui-core")})

NATURALLY_OCCURRING_FORMS = ("relative", "@ui/", "@/")


def _all_sources():
    paths = iter_frontend_sources()
    assert len(paths) > 30, (
        f"only {len(paths)} frontend source files found under {sorted(PACKAGE_ROOTS)} -- the workspace "
        f"layout moved (or every source file was deleted) and this guard would pass vacuously.\n\n{VACUITY_DOC}"
    )
    return paths


def _cross_package_edges():
    edges = set()
    resolved_by_form = {}
    for path in _all_sources():
        owner = package_for(path)
        for specifier in import_specifiers(path):
            target = resolve_import(path, specifier)
            if target is None:
                continue
            form = specifier_form(specifier)
            resolved_by_form[form] = resolved_by_form.get(form, 0) + 1
            target_owner = package_for(target)
            if target_owner is None or target_owner == owner:
                continue
            edges.add((owner, relative_source_name(path), specifier, target_owner))
    return edges, resolved_by_form


CROSS_PACKAGE_EDGES, RESOLVED_COUNTS_BY_FORM = _cross_package_edges()


def test_every_naturally_occurring_specifier_form_actually_resolves_something():
    dead = sorted(form for form in NATURALLY_OCCURRING_FORMS if RESOLVED_COUNTS_BY_FORM.get(form, 0) == 0)
    assert dead == [], (
        f"the resolver matched zero real files for specifier form(s) {dead}, even though the tree is "
        "full of them -- an aggregate resolved-count check across all forms combined would stay green "
        "here as long as any other form kept resolving, which is exactly how `elif False:` disabling "
        "the relative branch alone left this file at a clean pass. Each form has to prove itself alive "
        f"independently.\n\n{VACUITY_DOC}"
    )


def test_the_workspace_alias_form_resolver_is_alive():
    admin_app = PACKAGE_ROOTS["admin"] / "App.tsx"
    resolved = resolve_import(admin_app, f"{WORKSPACE_PREFIX}ui-core/lib/api")
    assert resolved is not None, (
        f"resolve_import({WORKSPACE_PREFIX}ui-core/lib/api) from admin/App.tsx did not resolve to a "
        "real file. No admin/node source uses this form today (it is the escape route the reviewer "
        "found, not an established convention), so this is a constructed positive control rather than "
        "a scan of the real tree -- it has to be exercised directly, or a dead @panel/ branch would "
        f"pass every scan-based assertion vacuously.\n\n{WORKSPACE_DOC}"
    )
    assert package_for(resolved) == "ui-core", (
        f"resolve_import({WORKSPACE_PREFIX}ui-core/lib/api) resolved to {resolved}, which this scan "
        f"attributes to {package_for(resolved)!r}, not 'ui-core' -- package_for() is broken.\n\n{WORKSPACE_DOC}"
    )
    assert resolved == PACKAGE_ROOTS["ui-core"] / "lib" / "api.ts", (
        f"resolve_import({WORKSPACE_PREFIX}ui-core/lib/api) resolved to {resolved}, not "
        f"packages/ui-core/src/lib/api.ts -- the subpath normalisation in resolve_import is wrong.\n\n{WORKSPACE_DOC}"
    )


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


SUB_PAGE_FORBIDDEN_MODULES = (
    "lib/api",
    "lib/version",
    "lib/panelRole",
    "lib/assertPanelRole",
    "hooks/useVersionStatus",
)

SUB_PAGE_DOC = (
    "sub-page is built without the __APP_VERSIONS__ / __FRONTEND_VERSION_KEY__ / __EXPECTED_PANEL_ROLE__ "
    "defines that ui-core's version and role modules read, so importing one of them fails the build -- "
    "but lib/api is the one that matters beyond the build: it is the admin axios client with an auth "
    "interceptor that logs the user out on any 401, and the subscription page is an unauthenticated "
    "surface that must never carry it."
)


def test_sub_page_imports_no_authenticated_or_build_define_module():
    root = PACKAGE_ROOTS.get("sub-page")
    assert root is not None, f"sub-page package missing\n\n{SUB_PAGE_DOC}"
    sources = iter_frontend_sources("sub-page")
    assert sources, f"no sub-page sources found under {root} -- this guard would pass vacuously\n\n{VACUITY_DOC}"
    offenders = []
    for path in sources:
        for specifier in import_specifiers(path):
            for forbidden in SUB_PAGE_FORBIDDEN_MODULES:
                if specifier.endswith(forbidden) or specifier.endswith(forbidden + ".ts"):
                    offenders.append(f"{relative_source_name(path)} -> {specifier}")
    assert offenders == [], "\n".join(sorted(offenders)) + f"\n\n{SUB_PAGE_DOC}"
