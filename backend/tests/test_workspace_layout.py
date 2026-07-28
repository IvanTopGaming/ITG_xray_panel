import importlib
import importlib.util

import pytest

from tests.import_graph import distributions_with_dependencies


def test_panel_core_is_importable():
    mod = importlib.import_module("panel_core")
    assert hasattr(mod, "__path__")
    assert callable(importlib.import_module("panel_core.dispatch").create_app)


@pytest.mark.parametrize(
    "name",
    [
        "panel_core.models",
        "panel_core.extensions",
        "panel_core.db_config",
        "panel_core.panel_role",
        "panel_core.pg_migrate",
        "panel_core.db_migration",
        "panel_core.utils",
        "panel_core.version",
        "panel_core.observability",
        "panel_core.pg_compat",
        "panel_core.api.auth",
        "panel_core.api.inbound",
        "panel_core.api.subscription",
        "panel_core.jobs.payments",
        "panel_core.services.provisioning",
        "panel_core.services.panel_proxy",
    ],
)
def test_submodules_importable(name):
    assert importlib.import_module(name) is not None


def test_application_no_longer_lives_under_app_namespace():
    assert importlib.util.find_spec("panel_core.models") is not None
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.models")


def test_panel_sub_is_a_workspace_distribution():
    from tests.import_graph import SRC_ROOTS

    roots = {path.parents[1].name for path in SRC_ROOTS}
    assert "panel-sub" in roots, f"panel-sub must be a distribution under packages/; found {sorted(roots)}"


def test_subscription_modules_live_in_panel_sub():
    from tests.import_graph import source_path

    for relative in ("api/subscription.py", "roles/sub.py"):
        path = source_path(relative)
        assert "panel-sub" in path.parts, f"{relative} resolved to {path}, expected it under packages/panel-sub"


def test_subscription_still_imports_under_its_original_name():
    import panel_core.api.subscription
    import panel_core.roles.sub

    assert panel_core.api.subscription.bp.name == "subscription"
    assert callable(panel_core.roles.sub.create_app)


BOTAPI_SOURCES = (
    "api/bot_service.py",
    "api/billing.py",
    "services/billing.py",
    "jobs/payments.py",
    "roles/botapi.py",
)


def test_panel_botapi_is_a_workspace_distribution():
    from tests.import_graph import SRC_ROOTS

    roots = {path.parents[1].name for path in SRC_ROOTS}
    assert "panel-botapi" in roots, f"panel-botapi must be a distribution under packages/; found {sorted(roots)}"


def test_botapi_modules_live_in_panel_botapi():
    from tests.import_graph import source_path

    for relative in BOTAPI_SOURCES:
        path = source_path(relative)
        assert "panel-botapi" in path.parts, f"{relative} resolved to {path}, expected it under packages/panel-botapi"


def test_the_yookassa_sdk_is_imported_only_from_panel_botapi():
    import ast

    from tests.import_graph import iter_sources, root_for, root_label

    offenders = []
    for path in iter_sources():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(name == "yookassa" or name.startswith("yookassa.") for name in names):
                label = root_label(root_for(path))
                if not label.startswith("panel-botapi"):
                    offenders.append(f"{label}/{path.name}")
    assert offenders == [], (
        "the yookassa SDK must be imported only from panel-botapi, otherwise the dependency "
        f"leaks back into every image: {offenders}"
    )


def test_no_module_is_shipped_by_two_distributions():
    from collections import defaultdict

    from tests.import_graph import SRC_ROOTS, iter_sources, relative_source_name, root_for, root_label

    owners = defaultdict(list)
    for path in iter_sources():
        owners[relative_source_name(path)].append(root_label(root_for(path)))

    assert owners, (
        f"no python module found under any of {[str(r) for r in SRC_ROOTS]} — this guard would pass "
        "vacuously with nothing to compare"
    )

    duplicates = {name: sorted(labels) for name, labels in owners.items() if len(labels) > 1}
    assert duplicates == {}, (
        "these modules are shipped by more than one distribution, so which one wins depends on "
        f"sys.path order and every guard silently inspects only the first: {duplicates}\n"
        "A module must be moved between distributions with git mv, never copied.\n"
        f"scanned roots: {[str(r) for r in SRC_ROOTS]}"
    )


def test_no_data_file_is_shipped_by_two_distributions():
    from collections import defaultdict

    from tests.import_graph import SRC_ROOTS, iter_non_python_sources, relative_source_name, root_for, root_label

    owners = defaultdict(list)
    for path in iter_non_python_sources():
        owners[relative_source_name(path)].append(root_label(root_for(path)))

    assert owners, (
        f"no non-.py file found under any of {[str(r) for r in SRC_ROOTS]} — this guard would pass "
        "vacuously with nothing to compare. panel_core/data/bot_texts_defaults.yaml is expected to be "
        "one such file today."
    )

    duplicates = {name: sorted(labels) for name, labels in owners.items() if len(labels) > 1}
    assert duplicates == {}, (
        "these files are shipped by more than one distribution: "
        f"{duplicates}\n"
        "panel_core/resources.py resolves package data through "
        'importlib.resources.files("panel_core.data"), which on a namespace package returns a '
        "MultiplexedPath that silently picks a winner ordered by distribution directory, so a "
        "duplicate is a live production hazard, not just dead weight.\n"
        "A file must be moved between distributions with git mv, never copied.\n"
        f"scanned roots: {[str(r) for r in SRC_ROOTS]}"
    )


WORKER_MODULES = (
    "xray/local.py",
    "xray/engine.py",
    "xray/grpc_client.py",
    "services/stats.py",
    "roles/worker.py",
)

WORKER_ONLY_DEPENDENCIES = ("docker", "filelock", "grpcio", "grpcio-tools", "protobuf")


def test_panel_worker_ships_the_local_xray_stack():
    from tests.import_graph import SRC_ROOTS, source_path

    roots = {root.parents[1].name for root in SRC_ROOTS}
    assert "panel-worker" in roots, f"panel-worker must be a distribution under packages/; found {sorted(roots)}"
    for relative in WORKER_MODULES:
        path = source_path(relative)
        assert "panel-worker" in path.parts, f"{relative} resolved to {path}, expected it under packages/panel-worker"


def test_the_heavy_stack_is_declared_only_by_panel_worker():
    declared = distributions_with_dependencies()
    offenders = sorted(
        f"{name} declares {dependency}"
        for name, entry in declared.items()
        for dependency in WORKER_ONLY_DEPENDENCIES
        if dependency in entry and name != "panel-worker"
    )
    assert offenders == [], (
        f"the gRPC/docker/filelock stack must be declared by panel-worker alone: {offenders}. Leaving any "
        "of it in panel-core's dependency list puts the whole Xray runtime into every image, which is the "
        "thing this cut exists to prevent."
    )
    assert declared["panel-worker"] >= set(WORKER_ONLY_DEPENDENCIES), (
        f"panel-worker declares {sorted(declared['panel-worker'])}, missing part of "
        f"{sorted(WORKER_ONLY_DEPENDENCIES)} — it ships engine.py and grpc_client.py and must declare them."
    )


def test_panel_worker_does_not_declare_panel_sub():
    """Wave 3b: the node stopped serving subscriptions, so it stopped shipping them.

    Dropping the blueprint registration and leaving the dependency would keep the suite green
    while the image kept carrying the code — and the promised fan-out saving (three images
    rebuilt per subscription edit down to one) would simply not arrive.
    """

    declared = distributions_with_dependencies()["panel-worker"]
    assert "panel-sub" not in declared, (
        f"panel-worker still declares panel-sub, but roles/worker.py registers no `subscription` "
        f"blueprint any more. Declared: {sorted(declared)}"
    )


MASTER_MODULES = (
    "api/bot_admin.py",
    "api/panels.py",
    "roles/master.py",
)

# Wave 2: the background jobs left the master for a service that runs on its own host.
CRON_MODULES = (
    "jobs/billing.py",
    "jobs/panels.py",
    "roles/cron.py",
)


def test_panel_master_ships_the_orchestrator_surface():
    from tests.import_graph import SRC_ROOTS, source_path

    roots = {root.parents[1].name for root in SRC_ROOTS}
    assert "panel-master" in roots, f"panel-master must be a distribution under packages/; found {sorted(roots)}"
    for relative in MASTER_MODULES:
        path = source_path(relative)
        assert "panel-master" in path.parts, f"{relative} resolved to {path}, expected it under packages/panel-master"


def test_panel_cron_ships_the_background_jobs():
    from tests.import_graph import SRC_ROOTS, source_path

    roots = {root.parents[1].name for root in SRC_ROOTS}
    assert "panel-cron" in roots, f"panel-cron must be a distribution under packages/; found {sorted(roots)}"
    for relative in CRON_MODULES:
        path = source_path(relative)
        assert "panel-cron" in path.parts, (
            f"{relative} resolved to {path}, expected it under packages/panel-cron. These jobs moved off the "
            f"master so that its outage stops halting free-tariff renewal, event replay and node polling; "
            f"leaving one behind in panel-master would put it back in the master's image and its process."
        )


def test_panel_master_does_not_declare_panel_sub():
    """Same cut as panel-worker: no registration, therefore no dependency."""

    declared = distributions_with_dependencies()["panel-master"]
    assert "panel-sub" not in declared, (
        f"panel-master still declares panel-sub, but roles/master.py registers no `subscription` "
        f"blueprint any more. Declared: {sorted(declared)}"
    )


ADMINAPI_MODULES = (
    "api/auth.py",
    "api/inbound.py",
    "api/outbound.py",
    "api/routing.py",
    "api/statistics.py",
    "api/system.py",
    "api/federation.py",
)


def test_panel_adminapi_ships_the_shared_admin_surface():
    from tests.import_graph import SRC_ROOTS, source_path

    roots = {root.parents[1].name for root in SRC_ROOTS}
    assert "panel-adminapi" in roots, f"panel-adminapi must be a distribution under packages/; found {sorted(roots)}"
    for relative in ADMINAPI_MODULES:
        path = source_path(relative)
        assert "panel-adminapi" in path.parts, (
            f"{relative} resolved to {path}, expected it under packages/panel-adminapi"
        )


def test_psutil_is_declared_only_by_panel_adminapi():
    offenders = sorted(
        f"{name} declares psutil"
        for name, entry in distributions_with_dependencies().items()
        if "psutil" in entry and name != "panel-adminapi"
    )
    assert offenders == [], (
        f"psutil must be declared by panel-adminapi alone: {offenders}. It is imported by exactly one "
        "module in the workspace (api/system.py), which ships from panel-adminapi; leaving it in "
        "panel-core's dependency list puts it in every image."
    )


def test_the_roles_declare_the_admin_surface_they_register():
    declared = distributions_with_dependencies()
    for name in ("panel-worker", "panel-master"):
        assert "panel-adminapi" in declared[name], (
            f"{name} must declare panel-adminapi: its role factory registers the auth, inbound, outbound, "
            f"routing, system, statistics and federation blueprints, all of which ship from "
            f"panel-adminapi. Declared: {sorted(declared[name])}"
        )


def test_the_workspace_has_exactly_the_seven_planned_distributions():
    expected = {
        "panel-core",
        "panel-adminapi",
        "panel-worker",
        "panel-master",
        "panel-sub",
        "panel-botapi",
        "panel-cron",
    }
    actual = set(distributions_with_dependencies())
    assert actual == expected, (
        f"the workspace holds {sorted(actual)}, expected {sorted(expected)}. Phase 8 wave 2 added panel-cron "
        "as the last cut; an eighth distribution or a missing one means the layout drifted from the design."
    )


def test_every_workspace_member_with_python_code_is_scanned():
    from tests.import_graph import (
        SRC_ROOTS,
        member_has_python_code,
        scanned_distributions,
        workspace_member_globs,
        workspace_members,
    )

    members = workspace_members()
    scanned = scanned_distributions()

    carriers = [member for member in members if member_has_python_code(member)]
    assert carriers, (
        f"not one of the {len(members)} workspace members ships any python code: "
        f"{sorted(member.name for member in members)}\n"
        "That means discovery is broken, not that the layout is clean — every member would be "
        "filtered out before the comparison and this guard would pass vacuously.\n"
        f"workspace globs: {workspace_member_globs()}"
    )

    missing = sorted(member.name for member in carriers if member not in scanned)

    assert missing == [], (
        "these workspace members ship Python code that no guard scans, because their layout does not "
        f"match packages/*/src/panel_core: {missing}\n"
        "Either move them to that layout or teach import_graph.SRC_ROOTS about them.\n"
        f"workspace globs: {workspace_member_globs()}\n"
        f"scanned roots: {[str(r) for r in SRC_ROOTS]}"
    )
