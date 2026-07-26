import ast

from tests.import_graph import (
    SRC_ROOTS,
    SRC_ROOTS_DOC,
    discovered_directories,
    iter_sources,
    module_name,
)

RESOURCE_HINT = (
    "A path built from __file__ addresses one distribution's source tree. panel_core is a namespace "
    "package, so its subpackages may ship from different distributions, and production installs them "
    "editable (uv sync --frozen --no-dev) — the trees stay physically apart. The moment the segment "
    "leaves the module's own directory it resolves under whichever distribution happens to own the "
    "importing module, and the file is simply not there. A non-editable wheel merges everything into one "
    "site-packages/panel_core/ and hides this, so it fails only in the install mode production uses, and "
    "silently: api/bot_admin.py returned HTTP 200 with an empty key list and the Bot -> Texts tab went "
    "blank. Reach package data through panel_core.resources (importlib.resources.files('panel_core.data'), "
    "a MultiplexedPath that searches every contributing distribution) instead."
)

JOIN_FUNCTIONS = {"join", "joinpath"}


def _escape_segments():
    return {".."} | set(discovered_directories())


def _is_file_derived(node, derived):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and (sub.id == "__file__" or sub.id in derived):
            return True
    return False


def _file_derived_names(tree):
    derived = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name) or target.id in derived:
                continue
            if _is_file_derived(node.value, derived):
                derived.add(target.id)
                changed = True
    return derived


def _literal_segments(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [part for part in node.value.split("/") if part]
    return []


def _path_building_calls(tree, derived):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in JOIN_FUNCTIONS:
            if _is_file_derived(node.func.value, derived) or any(_is_file_derived(a, derived) for a in node.args):
                yield node, node.args
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if _is_file_derived(node.left, derived):
                yield node, [node.right]


def _offenders(path):
    tree = ast.parse(path.read_text())
    derived = _file_derived_names(tree)
    if not derived and not any(isinstance(n, ast.Name) and n.id == "__file__" for n in ast.walk(tree)):
        return []
    escapes = _escape_segments()
    found = []
    for node, args in _path_building_calls(tree, derived):
        for arg in args:
            for segment in _literal_segments(arg):
                if segment in escapes:
                    found.append(f"{module_name(path)}:{node.lineno} -> {segment!r}")
    return sorted(set(found))


def test_no_module_builds_a_resource_path_from_dunder_file():
    sources = iter_sources()
    assert sources, (
        f"no python sources found under any of {[str(r) for r in SRC_ROOTS]} — this guard would pass "
        f"vacuously.\n\n{SRC_ROOTS_DOC}"
    )
    offenders = sorted({hit for path in sources for hit in _offenders(path)})
    assert offenders == [], f"{offenders}\n\n{RESOURCE_HINT}"


def test_the_escape_segment_set_covers_every_namespace_subpackage():
    escapes = _escape_segments()
    expected = {"..", "api", "services", "jobs", "roles", "xray", "data"}
    assert expected <= escapes, (
        f"the guard's escape-segment set is {sorted(escapes)}, missing {sorted(expected - escapes)}. It is "
        "derived from the directories actually present under the panel_core source roots, so a package "
        f"that disappears from discovery silently stops being guarded.\n\n{SRC_ROOTS_DOC}"
    )


def test_package_data_is_reachable_through_the_resources_module():
    from panel_core.resources import BOT_TEXTS_DEFAULTS, BOT_TEXTS_META, data_file, read_data_text

    for name in (BOT_TEXTS_DEFAULTS, BOT_TEXTS_META):
        assert data_file(name).is_file(), f"panel_core.data/{name} is not reachable via importlib.resources"
        assert read_data_text(name), f"panel_core.data/{name} read back empty"

    assert read_data_text("definitely-not-a-real-resource.yaml") is None
