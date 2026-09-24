"""Plain-text renderers for search / expand / stats / map output (optimised for agents to read)."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from contextgraph.context import SourceReader, format_memory, render_snippet, short_name
from contextgraph.models import SYMBOL_TYPES, Memory, Node
from contextgraph.retrieval import Retrieval, lexical_search
from contextgraph.store import Store
from contextgraph.util import estimate_tokens, first_sentence, is_test_path

LIST_CAP = 12


def _conf(meta: str | None) -> str:
    if meta and json.loads(meta).get("confidence") == "low":
        return "~"
    return ""


def resolve_symbol(store: Store, text: str) -> tuple[Node | None, list[Node]]:
    text = text.strip()
    if ":" in text and not text.startswith(":"):
        path, _, line = text.rpartition(":")
        if line.isdigit():
            ln = int(line)
            inside = [n for n in store.find_nodes(file_path=path)
                      if n.start_line and n.end_line and n.start_line <= ln <= n.end_line]
            if inside:
                inside.sort(key=lambda n: n.end_line - n.start_line)
                return inside[0], inside[1:]
    for finder in (
        lambda: store.find_nodes(qualname=text),
        lambda: store.find_nodes(name=text),
        lambda: store.find_nodes(qualname_suffix=text),
    ):
        found = [n for n in finder() if n.type != "memory"]
        if found:
            found.sort(key=lambda n: (n.type == "test", n.type == "module" and "." not in text, -n.importance))
            return found[0], found[1:]
    ids = [nid for nid, _ in lexical_search(store, text, 6)]
    hits = store.get_nodes(ids)
    return None, [hits[nid] for nid in ids if nid in hits and hits[nid].type != "memory"]


def render_expand(store: Store, node: Node, others: list[Node], with_source: bool, root: Path) -> str:
    lines = [f"{node.qualname} ({node.type})", f"DEFINED IN  {node.span}"]
    if node.signature:
        lines.append(f"SIGNATURE   {node.signature}")
    if node.summary:
        lines.append(f"SUMMARY     {node.summary}")

    groups: dict[str, list[tuple[Node, str]]] = defaultdict(list)
    out_labels = {"calls": "CALLS", "inherits": "INHERITS", "imports": "IMPORTS", "references": "REFERENCES",
                  "tested_by": "TESTED BY", "documented_by": "DOCUMENTED BY", "contains": "CONTAINS"}
    in_labels = {"calls": "CALLED BY", "inherits": "SUBCLASSES", "imports": "IMPORTED BY",
                 "references": "REFERENCED BY", "contains": "CONTAINED IN", "memory_about": "MEMORIES"}
    out_edges = store.out_edges(node.id)
    in_edges = store.in_edges(node.id)
    ids = {t for t, _, _ in out_edges} | {s for s, _, _ in in_edges}
    nodes = store.get_nodes(ids)
    for t, rel, meta in out_edges:
        if t in nodes and rel in out_labels:
            groups[out_labels[rel]].append((nodes[t], _conf(meta)))
    for s, rel, meta in in_edges:
        if s in nodes and rel in in_labels:
            groups[in_labels[rel]].append((nodes[s], _conf(meta)))

    if node.type == "class":
        # Callers of the class's methods from outside the class are the real "users".
        child_ids = {n.id for n, _ in groups.get("CONTAINS", [])}
        users: dict[int, str] = {}
        for cid in child_ids:
            for s, rel, meta in store.in_edges(cid):
                if rel == "calls" and s not in child_ids and s != node.id:
                    users.setdefault(s, _conf(meta))
        user_nodes = store.get_nodes(users)
        groups["USED BY (via methods)"] = [(user_nodes[i], c) for i, c in users.items() if i in user_nodes]

    order = ["CONTAINED IN", "CONTAINS", "INHERITS", "SUBCLASSES", "IMPORTS", "IMPORTED BY", "CALLS",
             "CALLED BY", "USED BY (via methods)", "REFERENCES", "REFERENCED BY", "TESTED BY", "DOCUMENTED BY"]
    for label in order:
        items = groups.get(label)
        if not items:
            continue
        items.sort(key=lambda x: (-x[0].importance, x[0].qualname))
        lines.append(f"\n{label}")
        for n, conf in items[:LIST_CAP]:
            lines.append(f"  {conf}{short_name(n)} ({n.type}) — {n.location}")
        if len(items) > LIST_CAP:
            lines.append(f"  … +{len(items) - LIST_CAP} more")

    memories = groups.get("MEMORIES", [])
    if memories:
        stale = {m.uid: m.stale for m in store.all_memories()}
        lines.append("\nMEMORIES")
        for n, _ in memories:
            uid = n.key.split(":", 1)[1]
            flag = " [may be outdated]" if stale.get(uid) else ""
            lines.append(f"  ({uid}) {n.docstring}{flag}")

    if with_source and node.file_path and node.type != "memory":
        lines.append("\nSOURCE")
        lines.append(render_snippet(node, SourceReader(root), store, 80).rstrip())
    if others:
        lines.append("\nOTHER MATCHES")
        lines += [_match_line(n) for n in others[:8]]
    if any(conf for items in groups.values() for _, conf in items):
        lines.append("\n(~ = low-confidence, name-based link)")
    return "\n".join(lines) + "\n"


def _match_line(n: Node) -> str:
    return f"  {n.qualname} ({n.type}) — {n.location}"


def render_no_match(symbol: str, others: list[Node]) -> str:
    if not others:
        return f"No symbol matching '{symbol}'.\n"
    return f"No exact match for '{symbol}'. Closest:\n" + "".join(_match_line(n) + "\n" for n in others)


def render_memories(memories: list[Memory], query: str | None) -> str:
    if not memories:
        return "No relevant memories.\n" if query else "No memories.\n"
    lines = []
    for m in memories:
        flag = " [may be outdated]" if m.stale else ""
        lines.append(f"({m.uid}) {m.created_at[:10]} {m.topic or '-'}: {m.content}{flag}")
        if m.files or m.symbols:
            lines.append(f"    about: {', '.join(m.files + m.symbols)}")
    return "\n".join(lines) + "\n"


def render_search(store: Store, r: Retrieval, limit: int) -> str:
    lines = [f"Results for: {r.query}  (retrieval: {r.mode})\n"]
    shown = [s for s in r.ranked if s.node.type != "file"][:limit]
    for i, s in enumerate(shown, start=1):
        n = s.node
        lines.append(f"{i}. {short_name(n)} ({n.type})  {n.location}  score {s.score:.2f}")
        if n.summary:
            lines.append(f"   {n.summary}")
        related = []
        nbr_ids = [(t, rel, "→") for t, rel, _ in store.out_edges(n.id) if rel not in ("contains", "imports")]
        nbr_ids += [(src, rel, "←") for src, rel, _ in store.in_edges(n.id) if rel in ("calls", "inherits")]
        nbrs = store.get_nodes(x[0] for x in nbr_ids)
        nbr_ids.sort(key=lambda x: -(nbrs[x[0]].importance if x[0] in nbrs else 0))
        for other, rel, arrow in nbr_ids[:5]:
            if other in nbrs and nbrs[other].type != "memory":
                related.append(f"{arrow} {rel.replace('_', ' ')} {short_name(nbrs[other])}")
        if related:
            lines.append("   related: " + "; ".join(related))
        lines.append("")
    if r.memories:
        lines.append("MEMORIES")
        lines += ["  " + format_memory(m) for m in r.memories[:5]]
    if not shown and not r.memories:
        lines.append("No matches. Try other words, or `contextgraph map` for an overview.")
    return "\n".join(lines).rstrip() + "\n"


def render_stats(store: Store, db_path: Path) -> str:
    c = store.counts()
    nodes = c["nodes_by_type"]
    edges = c["edges_by_relation"]
    symbols = sum(v for k, v in nodes.items() if k in SYMBOL_TYPES)
    langs = c["languages"]
    total_files = sum(langs.values()) or 1
    size = sum(p.stat().st_size for p in db_path.parent.glob(db_path.name + "*") if p.is_file())
    lines = [
        "Repository",
        "────────────────────",
        f"Files indexed        {c['files']:,}",
        f"Modules              {nodes.get('module', 0):,}",
        f"Symbols              {symbols:,}",
        f"Tests                {nodes.get('test', 0):,}",
        f"Documentation nodes  {nodes.get('documentation', 0):,}",
        f"Graph edges          {sum(edges.values()):,}",
    ]
    for rel in sorted(edges, key=lambda k: -edges[k]):
        lines.append(f"  {rel:<18} {edges[rel]:,}")
    lines += [
        f"Embeddings           {c['embeddings']:,}",
        f"Persistent memories  {c['memories']:,}" + (f" ({c['stale_memories']} possibly stale)" if c["stale_memories"] else ""),
        "",
        "Languages",
    ]
    for lang, count in sorted(langs.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {lang:<18} {100 * count / total_files:5.1f}%")
    lines += ["", f"Database size        {size / 1e6:.1f} MB", f"Last indexed         {store.get_meta('indexed_at')}"]
    return "\n".join(lines) + "\n"


def build_map(store: Store, budget: int) -> str:
    """Aider-style ranked repository overview that fits a token budget."""
    nodes = store.all_nodes(include_memory=False)
    by_parent: dict[int, list] = defaultdict(list)
    parent = store.parent_ids()
    for n in nodes:
        pid = parent.get(n.id)
        if pid is not None:
            by_parent[pid].append(n)
    modules = [n for n in nodes if n.type == "module" and not is_test_path(n.file_path or "")]

    def module_score(m) -> float:
        kids = sorted((c.importance for c in by_parent[m.id]), reverse=True)[:5]
        return m.importance + sum(kids)

    modules.sort(key=module_score, reverse=True)
    header = "REPOSITORY MAP (most central modules first)\n"
    used = estimate_tokens(header) + 20
    out = [header]
    for m in modules:
        if not m.docstring and not any(c.is_public for c in by_parent[m.id]):
            continue
        block = [f"\n{m.file_path}" + (f" — {first_sentence(m.docstring, 100)}" if m.docstring else "")]
        kids = [c for c in by_parent[m.id] if c.is_public and c.type in ("class", "function")]
        kids.sort(key=lambda c: -c.importance)
        for c in kids[:8]:
            doc = first_sentence(c.docstring, 80)
            label = "class " + c.name if c.type == "class" else (c.signature or c.name)
            if len(label) > 110:
                label = label[:109] + "…"
            block.append(f"  {label}  :{c.start_line}" + (f" — {doc}" if doc else ""))
            if c.type == "class":
                methods = [x for x in by_parent[c.id] if x.is_public and not x.name.startswith("__")]
                methods.sort(key=lambda x: -x.importance)
                if methods:
                    block.append("    methods: " + ", ".join(x.name for x in methods[:6]))
        text = "\n".join(block) + "\n"
        cost = estimate_tokens(text)
        if used + cost > budget:
            if used + 20 > budget:
                break
            continue
        used += cost
        out.append(text)
    body = "".join(out)
    return body + f"\n~{estimate_tokens(body)} tokens / {budget}\n"
