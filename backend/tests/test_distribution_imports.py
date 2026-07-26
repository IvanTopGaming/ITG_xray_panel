import tomllib

from tests.import_graph import (
    distribution_root,
    imported_modules,
    iter_sources,
    module_level_imports,
    relative_source_name,
    resolve_module_path,
    root_for,
    source_path,
    workspace_members,
)

DIRECTION_DOC = (
    "Distributions may only import downwards, along the edges each pyproject.toml actually declares. "
    'panel-sub and panel-botapi declare `dependencies = ["panel-core"]`, so they may import '
    "panel_core.* modules that panel-core ships; panel-core declares neither of them and therefore may "
    "import nothing they ship. Because panel_core is one namespace package, nothing about an import "
    "statement reveals which wheel the target comes from -- `from panel_core.services.billing import "
    "apply_payment` reads exactly like an intra-distribution import while actually making panel-core "
    "depend on panel-botapi, and dragging the yookassa SDK into every image that installs panel-core. "
    "The yookassa guard in tests/test_workspace_layout.py cannot see this: it matches literal `import "
    "yookassa` statements and never follows a panel_core.* edge."
)

ALLOWED_INVERSIONS_DOC = (
    "These are the two transitional inversions the panel-sub cut left behind, not a general licence. "
    "roles/master.py and roles/worker.py ship from panel-core but register the `subscription` "
    "blueprint, which ships from panel-sub; panel-sub already depends on panel-core, so the reverse "
    "edge cannot be declared without a workspace cycle. Production is unaffected (every image installs "
    "all three distributions), but `uv sync --package panel-core` alone cannot build the master or the "
    "worker: create_app() dies with ImportError: cannot import name 'subscription' from "
    "'panel_core.api' (unknown location). The fix is not a declaration -- it is the panel-master / "
    "panel-worker cut, which moves roles/{master,worker}.py out of panel-core. Emptying this set is an "
    "exit criterion of that cut: once those two modules ship from distributions that may declare a "
    "dependency on panel-sub, delete the entries (the staleness test below will demand it) and "
    "panel-core imports nothing from another distribution."
)

ALLOWED_INVERSIONS = frozenset(
    {
        ("panel-core", "roles/master.py", "panel_core.api.subscription", "panel-sub"),
        ("panel-core", "roles/worker.py", "panel_core.api.subscription", "panel-sub"),
    }
)

ROLE_DISPATCH_DOC = (
    "dispatch.py is the PANEL_ROLE selector: each branch imports exactly the role module the running "
    "host was configured for, inside create_app(), never at module scope. So the edge is only ever "
    "traversed on a host that installs that distribution by definition -- unlike the "
    "roles/{master,worker}.py inversions above, which fire on every master and every worker. This "
    "exemption is structural, not transitional: the dispatcher will still name all four roles after "
    "the panel-master / panel-worker cut. It holds only while the imports stay function-level, which "
    "test_the_role_dispatcher_imports_lazily below checks -- hoist one to module scope and importing "
    "panel_core.dispatch alone would drag every role's distribution in with it."
)

ROLE_DISPATCH_EXEMPTIONS = frozenset(
    {
        ("panel-core", "dispatch.py", "panel_core.roles.sub", "panel-sub"),
        ("panel-core", "dispatch.py", "panel_core.roles.botapi", "panel-botapi"),
    }
)

EXEMPT_EDGES = ALLOWED_INVERSIONS | ROLE_DISPATCH_EXEMPTIONS


def _project_table(member):
    with (member / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle).get("project", {})


def _requirement_name(spec):
    name = spec.split(";")[0].strip()
    for separator in ("[", "=", ">", "<", "!", "~", " ", "("):
        name = name.split(separator)[0]
    return name.strip().lower().replace("_", "-")


def _distributions():
    distributions = {}
    for member in workspace_members():
        project = _project_table(member)
        name = project.get("name")
        assert name, f"{member}/pyproject.toml declares no [project].name — ownership cannot be resolved"
        distributions[name] = {
            "path": member,
            "dependencies": {_requirement_name(spec) for spec in project.get("dependencies", [])},
        }
    return distributions


DISTRIBUTIONS = _distributions()

OWNER_BY_PATH = {entry["path"]: name for name, entry in DISTRIBUTIONS.items()}


def _dependency_closure(name):
    closure = set()
    queue = [name]
    while queue:
        for dependency in DISTRIBUTIONS.get(queue.pop(), {}).get("dependencies", set()):
            if dependency in DISTRIBUTIONS and dependency not in closure:
                closure.add(dependency)
                queue.append(dependency)
    return closure


def _owner(path):
    member = distribution_root(root_for(path))
    owner = OWNER_BY_PATH.get(member)
    assert owner, f"{path} lives under {member}, which is not a uv workspace member — ownership is unknown"
    return owner


def _cross_distribution_edges():
    edges = set()
    for path in iter_sources():
        owner = _owner(path)
        for module in sorted(imported_modules(path)):
            target = resolve_module_path(module)
            if target is None:
                continue
            target_owner = _owner(target)
            if target_owner != owner:
                edges.add((owner, relative_source_name(path), module, target_owner))
    return edges


CROSS_DISTRIBUTION_EDGES = _cross_distribution_edges()


def test_the_declared_dependency_graph_is_read_from_the_pyprojects():
    assert set(DISTRIBUTIONS) == {"panel-core", "panel-sub", "panel-botapi"}, (
        f"workspace membership changed: {sorted(DISTRIBUTIONS)}. Update nothing here — the guard reads "
        "the declarations — but check that the new member is covered by the direction test below."
    )
    assert DISTRIBUTIONS["panel-sub"]["dependencies"] >= {"panel-core"}
    assert DISTRIBUTIONS["panel-botapi"]["dependencies"] >= {"panel-core"}
    assert _dependency_closure("panel-core") == set(), (
        "panel-core gained a workspace dependency. That is the one edge the layout cannot have: "
        f"both other distributions depend on panel-core, so any reverse edge is a cycle.\n\n{DIRECTION_DOC}"
    )


def test_cross_distribution_edges_are_actually_detected():
    detected = {(owner, target_owner) for owner, _, _, target_owner in CROSS_DISTRIBUTION_EDGES}
    assert ("panel-botapi", "panel-core") in detected, (
        "no panel-botapi → panel-core import edge was resolved, which cannot be true: "
        "packages/panel-botapi/src/panel_core/services/billing.py imports panel_core.models and "
        "panel_core.extensions. The edge scan is broken, so the direction test below proves nothing."
    )
    assert ("panel-sub", "panel-core") in detected, (
        "no panel-sub → panel-core import edge was resolved, which cannot be true: "
        "packages/panel-sub/src/panel_core/roles/sub.py imports panel_core.app_base. The edge scan is "
        "broken, so the direction test below proves nothing."
    )


def test_no_distribution_imports_outside_its_declared_dependencies():
    offenders = sorted(
        f"{owner}:{source} imports {module} (ships from {target_owner})"
        for owner, source, module, target_owner in CROSS_DISTRIBUTION_EDGES
        if target_owner not in _dependency_closure(owner) and (owner, source, module, target_owner) not in EXEMPT_EDGES
    )
    assert offenders == [], (
        f"undeclared cross-distribution imports: {offenders}\n\n{DIRECTION_DOC}\n\n"
        "Either move the module into the distribution that needs it, or declare the dependency in the "
        "importer's pyproject.toml — but a dependency on panel-sub or panel-botapi cannot be declared "
        f"from panel-core, because that is a cycle.\n\n{ALLOWED_INVERSIONS_DOC}\n\n{ROLE_DISPATCH_DOC}"
    )


def test_the_exempted_edges_still_exist():
    stale = sorted(EXEMPT_EDGES - CROSS_DISTRIBUTION_EDGES)
    assert stale == [], (
        f"these exempted edges no longer happen: {stale}. Delete them from ALLOWED_INVERSIONS / "
        "ROLE_DISPATCH_EXEMPTIONS — an entry that matches nothing silently widens the guard for whoever "
        f"re-creates the same edge later.\n\n{ALLOWED_INVERSIONS_DOC}\n\n{ROLE_DISPATCH_DOC}"
    )


def test_the_role_dispatcher_imports_lazily():
    path = source_path("dispatch.py")
    eager = sorted(
        module
        for module in module_level_imports(path)
        if (target := resolve_module_path(module)) is not None and _owner(target) != _owner(path)
    )
    assert eager == [], f"panel_core.dispatch imports {eager} at module scope\n\n{ROLE_DISPATCH_DOC}"
