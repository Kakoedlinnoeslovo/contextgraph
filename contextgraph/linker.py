"""Resolve raw references into graph edges.

The linker always runs over the whole repository (it is cheap: dictionary
lookups), so edges stay correct after incremental re-indexing no matter which
files changed. Resolution order for a reference `a.b.c` in file F:

1. `self.x` / `cls.x` / `super().x`   -> method on the enclosing class (or its bases)
2. import aliases of F                 -> fully qualified name
3. top-level symbols of F's module     -> module-qualified name
4. star imports of F
5. exact qualname, then unique dotted-suffix match (handles src/ layouts and
   packages imported under a shorter root)
6. re-exports through package __init__ aliases
7. `Owner.method` where Owner is a class -> method lookup through inheritance
8. last resort: a unique, non-generic method/function name (low confidence)
"""

from __future__ import annotations

from collections import defaultdict

from contextgraph.models import SYMBOL_TYPES, Node
from contextgraph.parse_python import BUILTINS
from contextgraph.store import Store
from contextgraph.util import is_test_path

LINKABLE_TYPES = {"module", "class", "function", "method", "test"}

GENERIC_NAMES = set(dir(dict)) | set(dir(list)) | set(dir(str)) | set(dir(set)) | set(dir(object)) | {
    "run", "start", "stop", "close", "open", "get", "set", "add", "update", "read", "write", "send",
    "call", "execute", "process", "handle", "load", "save", "parse", "build", "create", "delete",
    "remove", "init", "setup", "reset", "clear", "copy", "format", "info", "debug", "warning",
    "error", "exception", "log", "main", "apply", "render", "validate", "check", "wait", "next",
    "items", "keys", "values", "append", "extend", "insert", "join", "split", "strip", "encode",
    "decode", "dump", "dumps", "loads", "fetch", "list", "name", "value", "data", "result",
    "connect", "commit", "rollback", "flush", "emit", "on", "off", "then", "catch", "push", "map",
    "filter", "reduce", "forEach", "toString", "length", "test", "cancel", "done", "submit",
}

Edge = tuple[int, int, str, dict | None]


class Linker:
    def __init__(self, store: Store):
        self.nodes: dict[int, Node] = {n.id: n for n in store.all_nodes(include_memory=False)}
        self.parent = store.parent_ids()
        self.by_qual: dict[str, list[int]] = defaultdict(list)
        self.suffix: dict[str, list[int]] = defaultdict(list)
        self.by_name: dict[str, list[int]] = defaultdict(list)
        self.module_of_file: dict[str, int] = {}
        self.modname_of_file: dict[str, str] = {}
        self.children: dict[int, dict[str, int]] = defaultdict(dict)
        for nid, n in self.nodes.items():
            if n.type in ("module", "file"):
                self.module_of_file[n.file_path] = nid
                if n.type == "module":
                    self.modname_of_file[n.file_path] = n.qualname
            if n.type not in LINKABLE_TYPES:
                continue
            self.by_qual[n.qualname].append(nid)
            self.by_name[n.name].append(nid)
            parts = n.qualname.split(".")
            for i in range(1, len(parts) - 1):
                self.suffix[".".join(parts[i:])].append(nid)
            pid = self.parent.get(nid)
            if pid is not None:
                self.children[pid][n.name] = nid
        self.aliases: dict[str, dict[str, str]] = defaultdict(dict)
        self.star: dict[str, list[str]] = defaultdict(list)
        self.refs = store.raw_refs()
        for src_id, ref in self.refs:
            if ref.kind != "import":
                continue
            src = self.nodes.get(src_id)
            if src is None:
                continue
            if ref.name == "*":
                self.star[src.file_path].append(ref.target or "")
            else:
                self.aliases[src.file_path][ref.name] = ref.target or ref.name
        self.bases: dict[int, list[int]] = defaultdict(list)

    # -- resolution ---------------------------------------------------------

    def _pick(self, ids: list[int], file_path: str | None) -> tuple[int, str] | None:
        ids = [i for i in ids if self.nodes[i].type in LINKABLE_TYPES]
        if not ids:
            return None
        if len(ids) == 1:
            return ids[0], "high"
        same = [i for i in ids if self.nodes[i].file_path == file_path]
        if len(same) == 1:
            return same[0], "high"
        mod = self.modname_of_file.get(file_path or "", "")

        def shared_prefix(i: int) -> int:
            a, b = mod.split("."), self.nodes[i].qualname.split(".")
            k = 0
            while k < min(len(a), len(b)) and a[k] == b[k]:
                k += 1
            return k

        best = max(ids, key=lambda i: (shared_prefix(i), -len(self.nodes[i].qualname)))
        return best, "low"

    def lookup(self, full: str, file_path: str | None, depth: int = 0) -> tuple[int, str] | None:
        if not full or depth > 3:
            return None
        hit = self._pick(self.by_qual.get(full, []), file_path) or self._pick(self.suffix.get(full, []), file_path)
        if hit:
            return hit
        parts = full.split(".")
        # Re-exports: pkg.Name where pkg/__init__.py does `from .impl import Name`.
        for i in range(len(parts) - 1, 0, -1):
            prefix, rest = ".".join(parts[:i]), parts[i:]
            mod = self._pick(self.by_qual.get(prefix, []) or self.suffix.get(prefix, []), file_path)
            if not mod or self.nodes[mod[0]].type != "module":
                continue
            aliases = self.aliases.get(self.nodes[mod[0]].file_path, {})
            if rest[0] in aliases and aliases[rest[0]] != full:
                found = self.lookup(".".join([aliases[rest[0]], *rest[1:]]), file_path, depth + 1)
                if found:
                    return found
            break
        return None

    def method_lookup(self, class_id: int, name: str, depth: int = 0) -> int | None:
        if depth > 5:
            return None
        hit = self.children.get(class_id, {}).get(name)
        if hit is not None:
            return hit
        for base in self.bases.get(class_id, []):
            found = self.method_lookup(base, name, depth + 1)
            if found is not None:
                return found
        return None

    def class_by_qual(self, qual: str, file_path: str | None) -> int | None:
        for i in self.by_qual.get(qual, []):
            if self.nodes[i].type == "class" and (file_path is None or self.nodes[i].file_path == file_path):
                return i
        return None

    def expand_alias(self, name: str, file_path: str) -> str:
        aliases = self.aliases.get(file_path, {})
        best = None
        for key in aliases:
            if name == key or name.startswith(key + "."):
                if best is None or len(key) > len(best):
                    best = key
        if best is not None:
            return aliases[best] + name[len(best):]
        mod = self.modname_of_file.get(file_path)
        head = name.split(".")[0]
        if mod and f"{mod}.{head}" in self.by_qual:
            return f"{mod}.{name}"
        for base in self.star.get(file_path, []):
            candidate = f"{base}.{name}" if base else name
            if self.lookup(candidate, file_path):
                return candidate
        return name

    def unique_fallback(self, name: str) -> tuple[int, str] | None:
        if len(name) < 4 or name in GENERIC_NAMES or name.startswith("__"):
            return None
        ids = [i for i in self.by_name.get(name, []) if self.nodes[i].type in SYMBOL_TYPES]
        if len(ids) == 1:
            return ids[0], "low"
        return None

    def resolve(self, name: str, file_path: str, scope: str | None) -> tuple[int, str] | None:
        parts = name.split(".")
        head = parts[0]
        if head in ("self", "cls"):
            if not scope or len(parts) < 2:
                return None
            cls_id = self.class_by_qual(scope, file_path)
            if cls_id is not None and len(parts) == 2:
                m = self.method_lookup(cls_id, parts[1])
                if m is not None:
                    return m, "high"
            return self.unique_fallback(parts[-1])
        if head == "super()":
            cls_id = self.class_by_qual(scope, file_path) if scope else None
            if cls_id is not None and len(parts) == 2:
                for base in self.bases.get(cls_id, []):
                    m = self.method_lookup(base, parts[1])
                    if m is not None:
                        return m, "high"
            return None
        if head == "?":
            return self.unique_fallback(parts[-1])
        full = self.expand_alias(name, file_path)
        hit = self.lookup(full, file_path)
        if hit:
            return hit
        if len(parts) >= 2:
            owner = self.lookup(full.rsplit(".", 1)[0], file_path)
            if owner and self.nodes[owner[0]].type == "class":
                m = self.method_lookup(owner[0], parts[-1])
                if m is not None:
                    return m, owner[1]
            aliased = head in self.aliases.get(file_path, {})
            if not aliased and head not in BUILTINS and owner is None:
                # `obj.method()` on an object of unknown type.
                return self.unique_fallback(parts[-1])
        return None

    def resolve_path(self, path: str) -> int | None:
        path = path.strip("/")
        if path in self.module_of_file:
            return self.module_of_file[path]
        matches = [f for f in self.module_of_file if f.endswith("/" + path)]
        if len(matches) == 1:
            return self.module_of_file[matches[0]]
        return None

    # -- edges --------------------------------------------------------------

    def link(self) -> list[Edge]:
        edges: list[Edge] = []
        for child, par in self.parent.items():
            if child in self.nodes and par in self.nodes:
                edges.append((par, child, "contains", None))

        # Inheritance first: method lookup through bases needs it.
        for src_id, ref in self.refs:
            if ref.kind != "inherit" or src_id not in self.nodes:
                continue
            src = self.nodes[src_id]
            hit = self.resolve(ref.name, src.file_path, None)
            if hit and self.nodes[hit[0]].type == "class":
                self.bases[src_id].append(hit[0])
                edges.append((src_id, hit[0], "inherits", {"confidence": hit[1]}))

        for src_id, ref in self.refs:
            src = self.nodes.get(src_id)
            if src is None or ref.kind == "inherit":
                continue
            if ref.kind == "import":
                if ref.name == "*" or not ref.target:
                    continue
                hit = self.lookup(ref.target, src.file_path)
                if hit is None and "." in ref.target:
                    hit = self.lookup(ref.target.rsplit(".", 1)[0], src.file_path)
                if hit:
                    edges.append((src_id, hit[0], "imports", {"confidence": hit[1]}))
                continue
            if ref.kind in ("call", "reference"):
                hit = self.resolve(ref.name, src.file_path, ref.scope)
                if hit is not None:
                    rel = "calls" if ref.kind == "call" else "references"
                    edges.append((src_id, hit[0], rel, {"confidence": hit[1]}))
                continue
            if ref.kind == "mention":
                hit = self._resolve_mention(ref.name)
                if hit is not None:
                    edges.append((hit, src_id, "documented_by", None))
                continue
            if ref.kind == "path_mention":
                target = self.resolve_path(ref.name)
                if target is not None and target != self.module_of_file.get(src.file_path):
                    edges.append((target, src_id, "documented_by", None))

        edges.extend(self._tested_by(edges))
        return edges

    def _resolve_mention(self, ident: str) -> int | None:
        if "." in ident:
            hit = self.lookup(ident, None)
            if hit is None:
                return None
            if hit[1] == "high":
                return hit[0]
            return None
        if len(ident) < 4 or ident in GENERIC_NAMES:
            return None
        ids = [i for i in self.by_name.get(ident, []) if self.nodes[i].type in LINKABLE_TYPES]
        if len(ids) == 1:
            return ids[0]
        classes = [i for i in ids if self.nodes[i].type == "class"]
        return classes[0] if len(classes) == 1 else None

    def _tested_by(self, edges: list[Edge]) -> list[Edge]:
        out: list[Edge] = []
        seen: set[tuple[int, int]] = set()

        for s, t, rel, _ in edges:
            if rel != "calls":
                continue
            src, tgt = self.nodes[s], self.nodes[t]
            if not is_test_path(src.file_path or "") or is_test_path(tgt.file_path or ""):
                continue
            test_id = s
            for target in (t, self.parent.get(t)):
                if target is None or target not in self.nodes:
                    continue
                if self.nodes[target].type == "module" and target != t:
                    continue
                if (target, test_id) not in seen:
                    seen.add((target, test_id))
                    out.append((target, test_id, "tested_by", None))

        # Name heuristic: tests/test_foo.py <-> foo.py
        modules_by_stem: dict[str, list[int]] = defaultdict(list)
        for f, nid in self.module_of_file.items():
            if self.nodes[nid].type == "module" and not is_test_path(f):
                modules_by_stem[self.nodes[nid].name].append(nid)
        for f, nid in self.module_of_file.items():
            if not is_test_path(f) or self.nodes[nid].type != "module":
                continue
            stem = f.rsplit("/", 1)[-1].removesuffix(".py")
            stem = stem.removeprefix("test_").removesuffix("_test")
            cands = modules_by_stem.get(stem, [])
            if len(cands) == 1 and (cands[0], nid) not in seen:
                seen.add((cands[0], nid))
                out.append((cands[0], nid, "tested_by", {"confidence": "low"}))
        return out
