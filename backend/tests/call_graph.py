import ast
import re
from collections import deque

from tests.import_graph import SRC_ROOTS, SRC_ROOTS_DOC, iter_sources, module_name, package_name


def _absolute(path, module, level):
    if not level:
        return module or ""
    parts = [part for part in package_name(path).split(".") if part]
    if level > 1:
        parts = parts[: -(level - 1)]
    if module:
        parts.extend(module.split("."))
    return ".".join(parts)


class _Collector(ast.NodeVisitor):
    def __init__(self, path, index):
        self.path = path
        self.mod = module_name(path)
        self.index = index
        self.scope = []

    def _bind(self, node):
        base = _absolute(self.path, node.module, node.level)
        if not base:
            return
        for alias in node.names:
            if alias.name == "*":
                continue
            self.index.imports.setdefault(self.mod, {}).setdefault(alias.asname or alias.name, (base, alias.name))

    def visit_ImportFrom(self, node):
        self._bind(node)

    def _function(self, node):
        qualname = f"{self.mod}:{'.'.join(self.scope + [node.name])}"
        calls = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                target = child.func
                if isinstance(target, ast.Name):
                    calls.add(target.id)
                elif isinstance(target, ast.Attribute):
                    calls.add(target.attr)
            elif isinstance(child, ast.ImportFrom):
                self._bind(child)
        self.index.functions[qualname] = {
            "module": self.mod,
            "name": node.name,
            "line": node.lineno,
            "calls": calls,
        }
        self.index.by_name.setdefault(node.name, set()).add(qualname)
        self.index.defs.setdefault(self.mod, {}).setdefault(node.name, set()).add(qualname)
        for decorator in node.decorator_list:
            source = ast.unparse(decorator)
            if re.match(r"bp\.\w+\(", source):
                self.index.routes.setdefault(self.mod, []).append(qualname)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_ClassDef(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()


class CallGraph:
    def __init__(self):
        self.functions = {}
        self.by_name = {}
        self.imports = {}
        self.defs = {}
        self.routes = {}
        sources = iter_sources()
        assert sources, (
            f"no python sources found under any of {[str(r) for r in SRC_ROOTS]} — every call-graph guard "
            f"would pass vacuously.\n\n{SRC_ROOTS_DOC}"
        )
        for path in sources:
            _Collector(path, self).visit(ast.parse(path.read_text()))

    def _targets(self, caller, name):
        module = self.functions[caller]["module"]
        bound = self.imports.get(module, {}).get(name)
        if bound is not None:
            candidate = f"{bound[0]}:{bound[1]}"
            if candidate in self.functions:
                return {candidate}
        local = self.defs.get(module, {}).get(name)
        if local:
            return set(local)
        return self.by_name.get(name, set())

    def route_handlers(self, *modules):
        found = []
        for module in modules:
            found.extend(self.routes.get(module, []))
        return found

    def path_to(self, seed, sinks):
        seen = {seed: [seed]}
        queue = deque([seed])
        while queue:
            current = queue.popleft()
            for name in sorted(self.functions[current]["calls"]):
                for target in sorted(self._targets(current, name)):
                    if target in seen:
                        continue
                    seen[target] = seen[current] + [target]
                    if target in sinks:
                        return seen[target]
                    queue.append(target)
        return None

    def describe(self, qualname):
        info = self.functions[qualname]
        return f"{info['module']}:{info['line']} {info['name']}"


_GRAPH = None


def call_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = CallGraph()
    return _GRAPH
