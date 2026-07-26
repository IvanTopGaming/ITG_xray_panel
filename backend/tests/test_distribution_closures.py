import tomllib

from tests.import_graph import (
    BACKEND_PYPROJECT,
    distribution_root,
    imported_modules,
    iter_sources,
    relative_source_name,
    root_for,
    workspace_members,
)
from tests.test_distribution_imports import _requirement_name
from tests.test_workspace_layout import _distributions_with_dependencies

LOCAL_ROOTS = {"panel_core", "tests"}

ISOLATION_DOC = (
    "Each distribution must be the only one importing the third-party packages it owns. The direction "
    "guard in test_distribution_imports.py covers only panel_core.* edges; a bare `import docker` in a "
    "panel-core module is invisible to it, and would put the Xray runtime back into every image. The "
    "owner map below is deliberately small -- it names the packages whose placement the cut exists to "
    "control, not every dependency in the workspace.\n\n"
    "grpcio-tools and protobuf are deliberately absent from the owner map: nothing under packages/ "
    "imports grpc_tools (the Dockerfile invokes it as `python -m grpc_tools.protoc`) and google.protobuf "
    "is reached only transitively through the generated stubs, never by a direct import. Adding either "
    "would trip test_every_owned_package_is_actually_imported_somewhere, which is that test doing its job."
)

EXTERNAL_OWNERS = {
    "docker": "panel-worker",
    "filelock": "panel-worker",
    "grpc": "panel-worker",
    "psutil": "panel-adminapi",
    "yookassa": "panel-botapi",
}

DECLARED_DEPENDENCY_NAME = {
    "docker": "docker",
    "filelock": "filelock",
    "grpc": "grpcio",
    "psutil": "psutil",
    "yookassa": "yookassa",
}


def _project_name(member):
    with (member / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle).get("project", {}).get("name")


OWNERS = {member: _project_name(member) for member in workspace_members()}


def _owner(path):
    return OWNERS[distribution_root(root_for(path))]


def _external_roots(path):
    roots = set()
    for module in imported_modules(path):
        root = module.split(".")[0]
        if root and root not in LOCAL_ROOTS:
            roots.add(root)
    return roots


def _all_external_roots():
    seen = set()
    for path in iter_sources():
        seen |= _external_roots(path)
    return seen


def test_the_scan_actually_sees_external_imports():
    seen = _all_external_roots()
    assert "flask" in seen, (
        "no `flask` import was resolved anywhere in the workspace, which cannot be true - the scan is "
        f"broken and every assertion below is vacuous.\n\n{ISOLATION_DOC}"
    )


def test_owned_third_party_packages_are_imported_only_by_their_owning_distribution():
    offenders = []
    for path in iter_sources():
        owner = _owner(path)
        for root in sorted(_external_roots(path)):
            expected = EXTERNAL_OWNERS.get(root)
            if expected is not None and expected != owner:
                offenders.append(f"{owner}:{relative_source_name(path)} imports {root} (owned by {expected})")
    assert sorted(offenders) == [], f"{sorted(offenders)}\n\n{ISOLATION_DOC}"


def test_every_owned_package_is_actually_imported_somewhere():
    seen = _all_external_roots()
    stale = sorted(root for root in EXTERNAL_OWNERS if root not in seen)
    assert stale == [], (
        f"EXTERNAL_OWNERS names packages nothing imports: {stale}. An entry with nothing behind it "
        f"silently protects nothing.\n\n{ISOLATION_DOC}"
    )


def test_the_owning_distribution_actually_declares_the_package_it_owns():
    declared = _distributions_with_dependencies()
    offenders = []
    for root, owner in sorted(EXTERNAL_OWNERS.items()):
        package = DECLARED_DEPENDENCY_NAME[root]
        if package not in declared.get(owner, set()):
            offenders.append(f"{owner} owns `{root}` but its pyproject.toml does not declare `{package}`")
    assert offenders == [], (
        f"{offenders}\n\nA distribution that imports a package without declaring it only works by "
        "accident, riding on some other distribution's declaration pulling the wheel into the shared "
        f".venv. Deleting the declaration would break the image silently while every test stayed green.\n\n{ISOLATION_DOC}"
    )


def test_the_root_aggregator_declares_only_the_six_workspace_distributions():
    with BACKEND_PYPROJECT.open("rb") as handle:
        project = tomllib.load(handle).get("project", {})
    declared = {_requirement_name(spec) for spec in project.get("dependencies", [])}
    expected = {_project_name(member) for member in workspace_members()}
    assert declared == expected, (
        f"backend/pyproject.toml's [project].dependencies is {sorted(declared)}, expected exactly the "
        f"workspace distributions {sorted(expected)}. workspace_members() globs packages/* only, so the "
        "root aggregator's own dependency list is invisible to every other guard in this file and in "
        "test_distribution_imports.py / test_workspace_layout.py -- a third-party package added directly "
        "here would reinstall into every image while all of those guards stayed green."
    )
