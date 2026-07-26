import ast
import pathlib
import tomllib
from collections import deque

BACKEND = pathlib.Path(__file__).resolve().parents[1]

PACKAGES = BACKEND / "packages"

BACKEND_PYPROJECT = BACKEND / "pyproject.toml"

SRC_ROOTS_DOC = (
    "Guards anchor on every packages/*/src/panel_core directory, not on packages/panel-core alone. "
    "panel_core is a namespace package precisely so a subpackage can move to a second distribution: "
    "after that move a whole-directory anchor still points at a non-empty directory, every guard stays "
    "green, and the modules that moved are scanned by nothing at all."
)


def _src_roots():
    roots = tuple(sorted(path for path in PACKAGES.glob("*/src/panel_core") if path.is_dir()))
    assert roots, f"no packages/*/src/panel_core source root found under {PACKAGES}\n\n{SRC_ROOTS_DOC}"
    return roots


SRC_ROOTS = _src_roots()


def root_for(path):
    for root in SRC_ROOTS:
        if path == root or root in path.parents:
            return root
    raise AssertionError(f"{path} lies outside every panel_core source root: {[str(r) for r in SRC_ROOTS]}")


def iter_sources(package=None):
    paths = []
    for root in SRC_ROOTS:
        base = root if package is None else root / package
        if base.is_dir():
            paths.extend(base.rglob("*.py"))
    return sorted(paths)


def source_path(relative):
    matches = [root / relative for root in SRC_ROOTS if (root / relative).is_file()]
    assert len(matches) == 1, (
        f"expected exactly one panel_core/{relative} across {[str(r) for r in SRC_ROOTS]}, found "
        f"{[str(m) for m in matches]}\n\n{SRC_ROOTS_DOC}"
    )
    return matches[0]


def relative_source_name(path):
    return path.relative_to(root_for(path)).as_posix()


def iter_root_modules():
    paths = []
    for root in SRC_ROOTS:
        paths.extend(root.glob("*.py"))
    return sorted(paths)


def discovered_packages():
    names = set()
    for root in SRC_ROOTS:
        for path in root.rglob("*.py"):
            relative = path.relative_to(root)
            if len(relative.parts) > 1:
                names.add(relative.parts[0])
    return sorted(names)


def discovered_directories():
    names = set()
    for root in SRC_ROOTS:
        for path in root.iterdir():
            if path.is_dir() and path.name != "__pycache__":
                names.add(path.name)
    return sorted(names)


def root_label(root):
    return f"{root.parents[1].name}:panel_core"


def distribution_root(root):
    return root.parents[1]


def scanned_distributions():
    return {distribution_root(root) for root in SRC_ROOTS}


def workspace_member_globs():
    with BACKEND_PYPROJECT.open("rb") as handle:
        config = tomllib.load(handle)
    globs = config.get("tool", {}).get("uv", {}).get("workspace", {}).get("members", [])
    assert globs, f"{BACKEND_PYPROJECT} declares no [tool.uv.workspace] members — member discovery is broken"
    return list(globs)


def workspace_members():
    globs = workspace_member_globs()
    members = set()
    for pattern in globs:
        for path in BACKEND.glob(pattern):
            if (path / "pyproject.toml").is_file():
                members.add(path)
    assert members, f"no uv workspace member resolved from {globs} under {BACKEND} — member discovery is broken"
    return sorted(members)


def member_has_python_code(member):
    return any(path.is_file() for path in member.rglob("*.py"))


def project_table(member):
    with (member / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle).get("project", {})


def project_name(member, *, context=""):
    name = project_table(member).get("name")
    assert name, f"{member}/pyproject.toml declares no [project].name{context}"
    return name


def requirement_name(spec):
    name = spec.split(";")[0].strip()
    for separator in ("[", "=", ">", "<", "!", "~", " ", "("):
        name = name.split(separator)[0]
    return name.strip().lower().replace("_", "-")


def distributions():
    result = {}
    for member in workspace_members():
        project = project_table(member)
        name = project_name(member, context=" — ownership cannot be resolved")
        result[name] = {
            "path": member,
            "dependencies": {requirement_name(spec) for spec in project.get("dependencies", [])},
        }
    return result


def distributions_with_dependencies():
    result = {name: entry["dependencies"] for name, entry in distributions().items()}
    assert result, "no workspace member resolved — this guard would pass vacuously"
    return result


HEAVY_ROOTS = (
    "app",
    "common",
    "docker",
    "filelock",
    "google",
    "grpc",
    "proxy",
)

XRAY_HEAVY_MODULES = ("panel_core.xray.engine", "panel_core.xray.grpc_client")

XRAY_SEAM_MODULES = ("panel_core.xray",) + XRAY_HEAVY_MODULES

HEAVY_ROOTS_DOC = (
    "'heavy' is defined once, in tests/import_graph.HEAVY_ROOTS: the top-level packages that only "
    "exist inside the worker's Docker image (grpc/google protobuf runtime, the generated Xray stubs "
    "app.*/common.*/proxy.*) or that only the worker needs (docker, filelock). Every guard derives its "
    "own view from that tuple -- do not re-spell the list locally, that divergence is what let "
    "google.protobuf slip past one guard while another caught it."
)


def heavy_root(module, extra=()):
    for heavy in tuple(extra) + HEAVY_ROOTS:
        if module == heavy or module.startswith(f"{heavy}."):
            return heavy
    return None


def module_name(path):
    parts = list(path.relative_to(root_for(path).parent).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def package_name(path):
    name = module_name(path)
    if path.name == "__init__.py":
        return name
    return name.rpartition(".")[0]


def _absolute_module(path, module, level):
    if not level:
        return module or ""
    parts = [part for part in package_name(path).split(".") if part]
    if level > 1:
        parts = parts[: -(level - 1)]
    if module:
        parts.extend(module.split("."))
    return ".".join(parts)


def _record(path, node, found):
    if isinstance(node, ast.ImportFrom):
        base = _absolute_module(path, node.module, node.level)
        if not base:
            return
        found.add(base)
        for alias in node.names:
            if alias.name != "*":
                found.add(f"{base}.{alias.name}")
    elif isinstance(node, ast.Import):
        for alias in node.names:
            found.add(alias.name)


def imported_modules(path):
    tree = ast.parse(path.read_text())
    found = set()
    for node in ast.walk(tree):
        _record(path, node, found)
    return found


def module_level_imports(path):
    tree = ast.parse(path.read_text())
    found = set()

    def visit(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                _record(path, child, found)
            else:
                visit(child)

    visit(tree)
    return found


def function_level_imports(path):
    tree = ast.parse(path.read_text())
    found = {}

    def visit(node, function):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, function or child.name)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                if function is not None:
                    imported = set()
                    _record(path, child, imported)
                    found.setdefault(function, set()).update(imported)
            else:
                visit(child, function)

    visit(tree, None)
    return found


def resolve_module_path(module):
    if module != "panel_core" and not module.startswith("panel_core."):
        return None
    for root in SRC_ROOTS:
        base = root.joinpath(*module.split(".")[1:])
        candidate = base.with_suffix(".py")
        if candidate.is_file():
            return candidate
        package_init = base / "__init__.py"
        if package_init.is_file():
            return package_init
    return None


def heavy_import_chain(module, extra=()):
    queue = deque([(module,)])
    visited = {module}
    while queue:
        chain = queue.popleft()
        current = chain[-1]
        root = heavy_root(current, extra)
        if root is not None:
            return chain if current == root else chain + (root,)
        target = resolve_module_path(current)
        if target is None:
            continue
        for mod in sorted(module_level_imports(target)):
            if mod not in visited:
                visited.add(mod)
                queue.append(chain + (mod,))
    return None


def import_chains(path):
    root = module_name(path)
    chains = {}
    queue = deque([(path, [root])])
    visited = {root}
    while queue:
        current, chain = queue.popleft()
        for mod in sorted(module_level_imports(current)):
            if mod not in chains:
                chains[mod] = chain + [mod]
            target = resolve_module_path(mod)
            if target is not None and mod not in visited:
                visited.add(mod)
                queue.append((target, chain + [mod]))
    return chains
