import ast
import pathlib

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


def imported_modules(path):
    tree = ast.parse(path.read_text())
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = _absolute_module(path, node.module, node.level)
            if not base:
                continue
            found.add(base)
            for alias in node.names:
                if alias.name != "*":
                    found.add(f"{base}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
    return found
