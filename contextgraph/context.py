"""Token-budgeted context packing.

Tier order (each tier gets a soft share of the budget; unused share rolls over):
summaries -> memories -> relationships -> source snippets -> tests -> docs -> pointers.
"""

from __future__ import annotations

from pathlib import Path

from contextgraph.config import Config
from contextgraph.models import CODE_TYPES, Memory, Node
from contextgraph.retrieval import Scored, retrieve
from contextgraph.store import Store
from contextgraph.util import estimate_tokens, sha1

TIER_SHARE = {"arch": 0.22, "memory": 0.08, "rel": 0.08, "source": 0.45, "tests": 0.07, "docs": 0.05}
FOOTER_RESERVE = 80
SNIPPET_MAX_LINES = 60
SECTION_TITLES = {
    "arch": "RELEVANT SYMBOLS", "memory": "MEMORY (from previous sessions)",
    "rel": "RELATIONSHIPS", "source": "SOURCE", "tests": "TESTS", "docs": "DOCS",
    "more": "MORE (not included; read with path:line)",
}


def short_name(n: Node) -> str:
    if n.type in ("method", "test") and n.qualname.count(".") >= 1:
        parts = n.qualname.split(".")
        return ".".join(parts[-2:]) if n.type == "method" or parts[-2][:1].isupper() else parts[-1]
    if n.type == "documentation":
        return f"{n.name} ({n.file_path})"
    if n.type == "module":
        return n.qualname
    return n.name


def format_memory(m: Memory, stale_note: str = " [may be outdated]") -> str:
    return f"({m.uid}) {m.topic + ': ' if m.topic else ''}{m.content}{stale_note if m.stale else ''}"


class Budget:
    """The overall token budget, minus a reserve for the footer."""

    def __init__(self, total: int):
        self.total = total
        self.used = 0

    def remaining(self) -> int:
        return self.total - FOOTER_RESERVE - self.used

    def take(self, text: str) -> bool:
        cost = estimate_tokens(text)
        if cost > self.remaining():
            return False
        self.used += cost
        return True


class Tier:
    """One section's soft share of the budget; an entry must fit both the share and the overall budget."""

    def __init__(self, budget: Budget, limit: int, entries: list[str]):
        self.budget = budget
        self.limit = limit
        self.entries = entries
        self.spent = 0

    def fits(self, text: str) -> bool:
        return self.spent + estimate_tokens(text) <= self.limit

    def add(self, text: str) -> bool:
        if not self.fits(text) or not self.budget.take(text):
            return False
        self.spent += estimate_tokens(text)
        self.entries.append(text)
        return True

    @property
    def unused(self) -> int:
        return max(0, self.limit - self.spent)


class SourceReader:
    def __init__(self, root: Path):
        self.root = root
        self.cache: dict[str, list[str]] = {}
        self.stale_files: set[str] = set()

    def lines(self, path: str) -> list[str]:
        if path not in self.cache:
            try:
                self.cache[path] = (self.root / path).read_text(errors="replace").splitlines()
            except OSError:
                self.cache[path] = []
        return self.cache[path]

    def segment(self, node: Node) -> list[str]:
        lines = self.lines(node.file_path or "")
        seg = lines[(node.start_line or 1) - 1:(node.end_line or 0)]
        if node.content_hash and node.type != "module" and sha1("\n".join(seg)) != node.content_hash:
            self.stale_files.add(node.file_path or "")
        return seg


def render_snippet(node: Node, reader: SourceReader, store: Store, max_lines: int) -> str:
    seg = reader.segment(node)
    if not seg:
        return ""
    start = node.start_line or 1
    end = start + len(seg) - 1
    if node.type == "class" and len(seg) > max_lines:
        body = [seg[0]]
        if node.docstring:
            body.append(f'    """{node.docstring.strip().splitlines()[0]}"""')
        for child in store.children(node.id):
            sig = child.signature or child.name
            body.append(f"    {sig}  # L{child.start_line}")
        text = "\n".join(body)
        note = f"(class outline; full body {node.file_path}:{start}-{end})"
    elif len(seg) > max_lines:
        keep = max_lines - 5
        text = "\n".join(seg[:keep])
        note = (f"(showing lines {start}-{start + keep - 1}; "
                f"read {node.file_path} offset={start + keep} limit={end - start - keep + 1} for the rest)")
    else:
        text = "\n".join(seg)
        note = ""
    header = f"--- {node.file_path}:{start}-{end}  {node.qualname}"
    return f"{header}\n{text}\n" + (f"{note}\n" if note else "")


def build_context(store: Store, root: Path, cfg: Config, query: str, budget_tokens: int) -> str:
    r = retrieve(store, cfg, query)
    b = Budget(budget_tokens)
    reader = SourceReader(root)
    head = f"QUERY\n{r.query}\n"
    b.take(head)
    sections: dict[str, list[str]] = {k: [] for k in SECTION_TITLES}
    carry = 0  # a tier's unused share rolls over to the next one

    def tier(name: str) -> Tier:
        return Tier(b, int(TIER_SHARE[name] * budget_tokens) + carry, sections[name])

    code = [s for s in r.ranked if s.node.type in CODE_TYPES]
    tests = [s for s in r.ranked if s.node.type == "test"]
    docs = [s for s in r.ranked if s.node.type == "documentation"]

    # 1. Architecture: names + summaries.
    t = tier("arch")
    included: set[int] = set()
    for i, s in enumerate(code[:15], start=1):
        n = s.node
        entry = f"{i}. {short_name(n)} ({n.type}) — {n.span}\n   {n.summary or ''}\n"
        if s.via and s.relevance < 0.05:
            entry = entry.rstrip("\n") + f"\n   (found via {s.via})\n"
        if not t.add(entry):
            break
        included.add(n.id)
    carry = t.unused

    # 2. Memories.
    t = tier("memory")
    for m in r.memories[:6]:
        if not t.add(f"- {format_memory(m, ' [may be outdated: linked code changed since this was written]')}\n"):
            break
    carry = t.unused

    # 3. Relationships among the selected nodes (and to other top-ranked nodes).
    t = tier("rel")
    ranked_ids = {s.node.id: s.node for s in r.ranked}
    rel_lines: list[tuple[int, str]] = []
    seen_rel: set[str] = set()
    for nid in included:
        src = ranked_ids[nid]
        for tid, rel, _ in store.out_edges(nid):
            if rel in ("contains", "imports") or tid not in ranked_ids:
                continue
            line = f"- {short_name(src)} {rel.replace('_', ' ')} {short_name(ranked_ids[tid])}\n"
            if line not in seen_rel:
                seen_rel.add(line)
                rel_lines.append((0 if tid in included else 1, line))
    for _, line in sorted(rel_lines, key=lambda x: x[0]):
        if not t.add(line):
            break
    carry = t.unused

    # 4. Source snippets.
    snippet_ids: set[int] = set()
    covered: set[int] = set()

    def add_snippets(candidates: list[Scored], t: Tier) -> None:
        for s in candidates:
            n = s.node
            if n.type == "module" or n.id in snippet_ids or n.id in covered:
                continue
            text = render_snippet(n, reader, store, SNIPPET_MAX_LINES)
            if not text or not t.add(text):
                continue
            snippet_ids.add(n.id)
            if n.type == "class" and n.end_line and n.start_line and \
                    n.end_line - n.start_line + 1 <= SNIPPET_MAX_LINES:
                covered.update(c.id for c in store.children(n.id))

    t = tier("source")
    add_snippets(code[:20], t)
    carry = t.unused

    # 5. Tests: those linked to selected symbols, then ranked tests.
    t = tier("tests")
    test_nodes: list[Node] = []
    for nid in list(included) + list(snippet_ids):
        for tid, rel, _ in store.out_edges(nid):
            if rel == "tested_by":
                tn = store.get_node(tid)
                if tn and tn.type != "module" and tn.id not in {x.id for x in test_nodes}:
                    test_nodes.append(tn)
    for s in tests:
        if s.node.id not in {x.id for x in test_nodes}:
            test_nodes.append(s.node)
    shown_tests: set[int] = set()
    for i, tn in enumerate(test_nodes[:12]):
        entry = f"- {short_name(tn)} — {tn.span}\n"
        if i < 2:
            snippet = render_snippet(tn, reader, store, 25)
            if snippet and t.fits(entry + snippet):
                entry = snippet
        if t.add(entry):
            shown_tests.add(tn.id)
    carry = t.unused

    # 6. Docs.
    t = tier("docs")
    doc_nodes = [s.node for s in docs]
    for nid in included:
        for tid, rel, _ in store.out_edges(nid):
            if rel == "documented_by" and tid not in {d.id for d in doc_nodes}:
                dn = store.get_node(tid)
                if dn:
                    doc_nodes.append(dn)
    for dn in doc_nodes[:6]:
        seg = reader.segment(dn)[:12]
        entry = f"--- {dn.span}  {dn.name}\n" + "\n".join(seg) + "\n"
        if not t.fits(entry):
            entry = f"- {dn.name} — {dn.span}\n"
        t.add(entry)

    # Second pass: spend leftover budget on more source, keeping a slice for pointers.
    leftover = b.remaining() - int(0.05 * budget_tokens)
    if leftover > 100:
        add_snippets(code[:30], Tier(b, leftover, sections["source"]))

    # 7. Pointers to what didn't fit.
    shown = included | snippet_ids | shown_tests
    for s in r.ranked:
        if s.node.id in shown or s.node.type == "file":
            continue
        entry = f"- {short_name(s.node)} ({s.node.type}) — {s.node.location}\n"
        if not b.take(entry):
            break
        sections["more"].append(entry)

    out = [head]
    for key, title in SECTION_TITLES.items():
        if sections[key]:
            out.append(f"\n{title}\n" + "".join(sections[key]))
    if reader.stale_files:
        out.append("\nNOTE: some files changed since indexing; run `contextgraph index` for fresh results.\n")
    body = "".join(out)
    used = estimate_tokens(body)
    return body + f"\n~{used} tokens used / {budget_tokens} (retrieval: {r.mode})\n"
