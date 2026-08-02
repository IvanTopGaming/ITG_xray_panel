import importlib

import pytest

from tests.import_graph import HEAVY_ROOTS_DOC, heavy_root, imported_modules, source_path


@pytest.mark.parametrize("name", ["get_channel", "_api_add_user_grpc", "_api_remove_user_grpc"])
def test_grpc_client_exposes(name):
    mod = importlib.import_module("panel_core.xray.grpc_client")
    assert callable(getattr(mod, name))


def test_stats_module_no_longer_imports_grpc_directly():
    offenders = sorted({mod for mod in imported_modules(source_path("services/stats.py")) if heavy_root(mod)})
    assert offenders == [], (
        f"services/stats.py must reach Xray only through panel_core.xray.grpc_client, but it imports "
        f"{offenders} directly. stats.py is the one services/ module allowed to be worker-side, so the "
        f"layering guard lets it through — this test is what keeps it from owning the protobuf stubs "
        f"itself. {HEAVY_ROOTS_DOC}"
    )


def test_grpc_client_owns_the_protobuf_namespaces():
    roots = {heavy_root(mod) for mod in imported_modules(source_path("xray/grpc_client.py"))}
    assert {"app", "grpc"} <= roots, (
        f"grpc_client.py is supposed to be the single owner of the generated protobuf stubs and gRPC, but "
        f"its heavy imports are {sorted(roots - {None})}. If they moved, the tests asserting everything "
        "else stays clean of them are now guarding an empty room."
    )
