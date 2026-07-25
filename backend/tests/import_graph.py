import ast
import pathlib
from collections import deque

SRC = pathlib.Path(__file__).resolve().parents[1] / "packages" / "panel-core" / "src" / "panel_core"


def module_name(path):
    parts = list(path.relative_to(SRC.parent).with_suffix("").parts)
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


def resolve_module_path(module):
    if module != "panel_core" and not module.startswith("panel_core."):
        return None
    base = SRC.joinpath(*module.split(".")[1:])
    candidate = base.with_suffix(".py")
    if candidate.is_file():
        return candidate
    package_init = base / "__init__.py"
    if package_init.is_file():
        return package_init
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
