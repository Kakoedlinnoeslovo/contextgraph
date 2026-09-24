"""Python indexer built on the standard-library ast module.

Extracts modules, classes, functions, methods, imports, inheritance, calls and
annotation references. Call names are rewritten to file-local dotted names
using light type inference (self.attr assignments, annotated parameters,
`x = ClassName(...)`) so the linker can resolve `self.manager.scale()` to
`WorkerManager.scale`.
"""

from __future__ import annotations

import ast
import builtins

from contextgraph.models import FileResult, Node, RawRef
from contextgraph.util import is_test_path, segment_hash, sha1

BUILTINS = frozenset(dir(builtins))
_WRAPPERS = {"Optional", "Annotated", "Type", "type", "Final", "ClassVar", "Required", "NotRequired"}


def module_name_for(path: str) -> str:
    p = path[:-3] if path.endswith(".py") else path
    parts = [x for x in p.split("/") if x]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if len(parts) > 1 and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts) or "__root__"


def dotted(expr: ast.AST | None) -> str | None:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        base = dotted(expr.value)
        return f"{base}.{expr.attr}" if base else f"?.{expr.attr}"
    if isinstance(expr, ast.Call):
        if isinstance(expr.func, ast.Name) and expr.func.id == "super":
            return "super()"
        return None
    if isinstance(expr, ast.Subscript):
        return dotted(expr.value)
    return None


def ann_type(expr: ast.AST | None) -> str | None:
    """Best-effort single class name from an annotation (unwraps Optional, X | None, "X")."""
    if expr is None:
        return None
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        try:
            expr = ast.parse(expr.value, mode="eval").body
        except SyntaxError:
            return None
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
        left, right = ann_type(expr.left), ann_type(expr.right)
        if right in (None, "None"):
            return left
        if left in (None, "None"):
            return right
        return None
    if isinstance(expr, ast.Subscript):
        base = dotted(expr.value)
        if base and base.split(".")[-1] in _WRAPPERS:
            inner = expr.slice
            if isinstance(inner, ast.Tuple) and inner.elts:
                inner = inner.elts[0]
            return ann_type(inner)
        return None
    d = dotted(expr)
    if d in (None, "None") or (d and d.startswith("?")):
        return None
    return d


def annotation_names(expr: ast.AST | None) -> set[str]:
    names: set[str] = set()
    if expr is None:
        return names
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        try:
            expr = ast.parse(expr.value, mode="eval").body
        except SyntaxError:
            return names
    for node in ast.walk(expr):
        if isinstance(node, (ast.Name, ast.Attribute)):
            d = dotted(node)
            if d and not d.startswith("?") and d.split(".")[0] not in BUILTINS and d.split(".")[-1] not in _WRAPPERS:
                names.add(d)
    # Drop prefixes of longer dotted names (typing.Optional -> keep only full chains)
    return {n for n in names if not any(o != n and o.startswith(n + ".") for o in names)}


def _looks_like_class(name: str | None) -> bool:
    return bool(name) and name.split(".")[-1][:1].isupper()


def _decorated(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, sig: str) -> str:
    """Prefix the decorators and cap the length."""
    decos = " ".join("@" + ast.unparse(d) for d in node.decorator_list)
    sig = f"{decos} {sig}" if decos else sig
    return sig if len(sig) <= 240 else sig[:239] + "…"


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    try:
        sig = f"{prefix} {node.name}({ast.unparse(node.args)})"
        if node.returns is not None:
            sig += f" -> {ast.unparse(node.returns)}"
        return _decorated(node, sig)
    except Exception:  # ast.unparse can hit RecursionError on deeply nested expressions
        return f"{prefix} {node.name}(...)"


def _class_signature(node: ast.ClassDef) -> str:
    try:
        bases = ", ".join(ast.unparse(b) for b in [*node.bases, *node.keywords])
        return _decorated(node, f"class {node.name}" + (f"({bases})" if bases else ""))
    except Exception:  # see _signature
        return f"class {node.name}"


def _is_public(name: str) -> bool:
    return not name.startswith("_") or (name.startswith("__") and name.endswith("__"))


class _Ctx:
    def __init__(self, path: str, lines: list[str], modname: str, is_test: bool):
        self.path = path
        self.lines = lines
        self.modname = modname
        self.is_test = is_test
        self.nodes: list[Node] = []
        self.refs: list[RawRef] = []
        self.keys: set[str] = set()

    def unique_key(self, key: str, line: int) -> str:
        """Redefinitions (property setters, conditional defs) share a qualname:
        the first keeps the canonical key, later ones get a line suffix."""
        if key in self.keys:
            key = f"{key}~{line}"
        self.keys.add(key)
        return key


class PythonParser:
    def parse(self, path: str, source: str) -> FileResult:
        content_hash = sha1(source)
        lines = source.splitlines()
        modname = module_name_for(path)
        mod_key = f"module:{path}:{modname}"
        module = Node(key=mod_key, type="module", name=modname.split(".")[-1], qualname=modname,
                      file_path=path, start_line=1, end_line=max(len(lines), 1), content_hash=content_hash)
        result = FileResult(path=path, language="python", content_hash=content_hash, nodes=[module])
        try:
            tree = ast.parse(source, filename=path)
        except (SyntaxError, ValueError):
            return result  # still record the module so the file is visible in the graph

        base_name = path.rsplit("/", 1)[-1]
        module.docstring = ast.get_docstring(tree)
        module.is_public = not base_name.startswith("_") or base_name == "__init__.py"
        ctx = _Ctx(path, lines, modname, is_test_path(path))
        ctx.nodes.append(module)
        self._imports(tree, ctx, mod_key, path.endswith("__init__.py"))
        module_level: list[ast.stmt] = []
        module_vars: dict[str, str] = {}
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                t = self._value_type(stmt.value, module_vars)
                if t:
                    module_vars[stmt.targets[0].id] = t
        for stmt in tree.body:
            if isinstance(stmt, ast.ClassDef):
                self._class(stmt, ctx, parent_key=mod_key, prefix=modname)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._function(stmt, ctx, parent_key=mod_key, prefix=modname, owner=None,
                               attr_types={}, module_vars=module_vars)
            else:
                module_level.append(stmt)
        self._calls(module_level, ctx, mod_key, scope=None, attr_types={}, var_types={})
        result.nodes = ctx.nodes
        result.refs = ctx.refs
        return result

    # -- imports ------------------------------------------------------------

    def _imports(self, tree: ast.Module, ctx: _Ctx, mod_key: str, is_package: bool) -> None:
        package = ctx.modname if is_package else ctx.modname.rpartition(".")[0]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    ctx.refs.append(RawRef(mod_key, "import", alias.asname or alias.name, alias.name))
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base_parts = package.split(".") if package else []
                    up = node.level - 1
                    if up:
                        base_parts = base_parts[:-up] if up <= len(base_parts) else []
                    base = ".".join(p for p in [".".join(base_parts), node.module or ""] if p)
                else:
                    base = node.module or ""
                for alias in node.names:
                    if alias.name == "*":
                        ctx.refs.append(RawRef(mod_key, "import", "*", base))
                        continue
                    target = f"{base}.{alias.name}" if base else alias.name
                    ctx.refs.append(RawRef(mod_key, "import", alias.asname or alias.name, target))

    # -- definitions --------------------------------------------------------

    def _span(self, node: ast.AST) -> tuple[int, int]:
        start = node.lineno
        for d in getattr(node, "decorator_list", []) or []:
            start = min(start, d.lineno)
        return start, getattr(node, "end_lineno", None) or node.lineno

    def _class(self, node: ast.ClassDef, ctx: _Ctx, parent_key: str, prefix: str) -> None:
        qual = f"{prefix}.{node.name}"
        start, end = self._span(node)
        key = ctx.unique_key(f"class:{ctx.path}:{qual}", start)
        ctx.nodes.append(Node(
            key=key, type="class", name=node.name, qualname=qual, file_path=ctx.path,
            start_line=start, end_line=end, signature=_class_signature(node),
            docstring=ast.get_docstring(node), is_public=_is_public(node.name),
            content_hash=segment_hash(ctx.lines, start, end), parent_key=parent_key,
        ))
        for base in node.bases:
            name = dotted(base)
            if name and not name.startswith("?") and name not in BUILTINS:
                ctx.refs.append(RawRef(key, "inherit", name, scope=qual))
        for d in node.decorator_list:
            name = dotted(d.func if isinstance(d, ast.Call) else d)
            if name and not name.startswith("?") and name.split(".")[0] not in BUILTINS:
                ctx.refs.append(RawRef(key, "call", name, scope=qual))

        attr_types = self._attr_types(node)
        class_level: list[ast.stmt] = []
        for stmt in node.body:
            if isinstance(stmt, ast.ClassDef):
                self._class(stmt, ctx, parent_key=key, prefix=qual)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._function(stmt, ctx, parent_key=key, prefix=qual, owner=qual, attr_types=attr_types)
            else:
                class_level.append(stmt)
        self._calls(class_level, ctx, key, scope=qual, attr_types=attr_types, var_types={})

    def _attr_types(self, cls: ast.ClassDef) -> dict[str, str]:
        types: dict[str, str] = {}
        for stmt in cls.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                t = ann_type(stmt.annotation)
                if t:
                    types[stmt.target.id] = t
        for stmt in cls.body:
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = self._param_types(stmt)
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.AnnAssign):
                    tgt = sub.target
                    if (isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name)
                            and tgt.value.id == "self"):
                        t = ann_type(sub.annotation)
                        if t:
                            types.setdefault(tgt.attr, t)
                elif isinstance(sub, ast.Assign):
                    for tgt in sub.targets:
                        if (isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name)
                                and tgt.value.id == "self"):
                            t = self._value_type(sub.value, params)
                            if t:
                                types.setdefault(tgt.attr, t)
        return types

    @staticmethod
    def _param_types(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, str]:
        out = {}
        a = fn.args
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs]:
            t = ann_type(arg.annotation)
            if t:
                out[arg.arg] = t
        return out

    @staticmethod
    def _value_type(value: ast.AST, known: dict[str, str]) -> str | None:
        if isinstance(value, ast.Call):
            name = dotted(value.func)
            if name and not name.startswith("?") and _looks_like_class(name):
                return name
        elif isinstance(value, ast.Name) and value.id in known:
            return known[value.id]
        return None

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, ctx: _Ctx, parent_key: str,
                  prefix: str, owner: str | None, attr_types: dict[str, str],
                  module_vars: dict[str, str] | None = None) -> None:
        qual = f"{prefix}.{node.name}"
        is_test = ctx.is_test and node.name.startswith("test")
        ntype = "test" if is_test else ("method" if owner else "function")
        start, end = self._span(node)
        key = ctx.unique_key(f"{ntype}:{ctx.path}:{qual}", start)
        ctx.nodes.append(Node(
            key=key, type=ntype, name=node.name, qualname=qual, file_path=ctx.path,
            start_line=start, end_line=end, signature=_signature(node),
            docstring=ast.get_docstring(node), is_public=_is_public(node.name),
            content_hash=segment_hash(ctx.lines, start, end), parent_key=parent_key,
        ))
        refs: set[str] = set()
        a = node.args
        for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]:
            if arg is not None:
                refs |= annotation_names(arg.annotation)
        refs |= annotation_names(node.returns)
        for r in sorted(refs):
            ctx.refs.append(RawRef(key, "reference", r, scope=owner))
        for d in node.decorator_list:
            name = dotted(d.func if isinstance(d, ast.Call) else d)
            if name and not name.startswith("?") and name.split(".")[0] not in BUILTINS | {"self"}:
                ctx.refs.append(RawRef(key, "call", name, scope=owner))
        var_types = {**(module_vars or {}), **self._param_types(node)}
        self._calls(node.body, ctx, key, scope=owner, attr_types=attr_types, var_types=var_types)

    # -- calls --------------------------------------------------------------

    def _calls(self, body: list[ast.stmt], ctx: _Ctx, src_key: str, scope: str | None,
               attr_types: dict[str, str], var_types: dict[str, str]) -> None:
        var_types = dict(var_types)
        # First pass: local variable types from `x = ClassName(...)`, `x: T = ...`, `with T() as x`.
        for stmt in body:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Assign) and len(sub.targets) == 1 and isinstance(sub.targets[0], ast.Name):
                    t = self._value_type(sub.value, var_types)
                    if t:
                        var_types.setdefault(sub.targets[0].id, t)
                elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                    t = ann_type(sub.annotation)
                    if t:
                        var_types.setdefault(sub.target.id, t)
                elif isinstance(sub, (ast.With, ast.AsyncWith)):
                    for item in sub.items:
                        if isinstance(item.optional_vars, ast.Name):
                            t = self._value_type(item.context_expr, var_types)
                            if t:
                                var_types.setdefault(item.optional_vars.id, t)
        seen: set[str] = set()
        for stmt in body:
            for sub in ast.walk(stmt):
                if not isinstance(sub, ast.Call):
                    continue
                name = dotted(sub.func)
                if not name:
                    continue
                name = self._rewrite(name, attr_types, var_types)
                head = name.split(".")[0]
                if "." not in name and head in BUILTINS:
                    continue
                if name in seen:
                    continue
                seen.add(name)
                ctx.refs.append(RawRef(src_key, "call", name, scope=scope))

    @staticmethod
    def _rewrite(name: str, attr_types: dict[str, str], var_types: dict[str, str]) -> str:
        parts = name.split(".")
        if parts[0] == "self" and len(parts) >= 3 and parts[1] in attr_types:
            return ".".join([attr_types[parts[1]], *parts[2:]])
        if parts[0] in var_types and len(parts) >= 2:
            return ".".join([var_types[parts[0]], *parts[1:]])
        return name
