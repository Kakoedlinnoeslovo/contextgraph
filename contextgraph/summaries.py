"""Zero-cost summaries built from signatures, docstrings and graph neighbours."""

from __future__ import annotations

from contextgraph.models import Node
from contextgraph.util import first_sentence

MAX_CHARS = 300


def _label(n: Node) -> str:
    if n.type in ("method", "test") and "." in n.qualname:
        owner, name = n.qualname.split(".")[-2:]
        if owner[:1].isupper():
            return f"{owner}.{name}"
    return n.name


def _names(nodes: list[Node], limit: int) -> str:
    ordered = sorted(nodes, key=lambda n: -n.importance)
    names: list[str] = []
    for n in ordered:
        label = _label(n)
        if label not in names:
            names.append(label)
        if len(names) >= limit:
            break
    more = len({_label(n) for n in nodes}) - len(names)
    return ", ".join(names) + (f" (+{more})" if more > 0 else "")


def summarize(node: Node, children: list[Node], callees: list[Node], callers: list[Node]) -> str:
    doc = first_sentence(node.docstring)
    if node.type == "module":
        parts = [f"Module {node.qualname}."]
        if doc:
            parts.append(doc)
        public = [c for c in children if c.is_public and c.type in ("class", "function")]
        if public:
            parts.append(f"Defines: {_names(public, 8)}.")
    elif node.type == "class":
        parts = [f"{node.signature}." if node.signature else f"class {node.name}."]
        if doc:
            parts.append(doc)
        methods = [c for c in children if c.is_public and c.type in ("method", "function", "test")
                   and not c.name.startswith("__")]
        if methods:
            parts.append(f"Methods: {_names(methods, 8)}.")
        if callers:
            parts.append(f"Used by: {_names(callers, 4)}.")
    elif node.type in ("function", "method", "test"):
        sig = node.signature or node.name
        parts = [sig if sig.endswith(".") else sig + "."]
        if doc:
            parts.append(doc)
        if callees:
            parts.append(f"Calls: {_names(callees, 5)}.")
        if callers:
            parts.append(f"Called by: {_names(callers, 4)}.")
    elif node.type == "documentation":
        parts = [f"Docs '{node.name}' ({node.file_path})."]
        if node.docstring:
            parts.append(first_sentence(node.docstring, 240))
    else:
        parts = [node.name]
        if doc:
            parts.append(doc)
    text = " ".join(parts)
    return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1] + "…"
