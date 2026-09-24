"""Dump the data behind images/how-it-works.gif from an indexed repository.

Runs one real `contextgraph context` call and records what each retrieval stage
picked (search hits, seeds, graph walk, briefing), plus the graph itself and the
token counts the GIF compares. Read-only: it never writes to the index.

    uv run --extra embed python images/how-it-works/extract.py /path/to/celery \
        "What happens when a worker receives a revoke request?" 3000

Writes images/how-it-works/data.js next to this file. The GIF in the README was made
from celery at commit eb3dfa3, with the memory from images/demo.tape saved.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import contextgraph.context as C
import contextgraph.retrieval as R
from contextgraph.config import Config, RepoPaths
from contextgraph.store import Store
from contextgraph.util import estimate_tokens, is_test_path

HERE = Path(__file__).parent
DRAWN_RELATIONS = ("calls", "inherits", "tested_by", "documented_by", "references")


def instrument(trace: dict) -> None:
    """Wrap the retrieval stages so one real run records what each of them returned."""
    lexical_search, expand, search = R.lexical_search, R.expand, R.SemanticIndex.search

    def lex(store, query, limit, memories=False):
        out = lexical_search(store, query, limit, memories)
        if not memories:
            trace["keyword"] = [nid for nid, _ in out]
        return out

    def sem(self, qvec, k):
        out = search(self, qvec, k)
        trace["semantic_keys"] = [key for key, _ in out]
        return out

    def exp(store, seeds, importance):
        out = expand(store, seeds, importance)
        trace["seeds"] = list(seeds)
        trace["walk"] = {nid: (via[1] if via else None, via[0].split()[0] if via else None)
                         for nid, (_, via) in out.items()}
        return out

    R.lexical_search, R.expand, R.SemanticIndex.search = lex, exp, sem


def briefing_items(text: str) -> dict[str, list[tuple[str, int]]]:
    """(path, start line) of everything the briefing shows, by section."""
    items: dict[str, list[tuple[str, int]]] = {"symbols": [], "source": [], "tests": [], "more": []}
    names = {"arch": "symbols", "source": "source", "tests": "tests", "more": "more"}
    headers = {title: names.get(key) for key, title in C.SECTION_TITLES.items()}
    section = None
    for line in text.splitlines():
        if line in headers or line == "QUERY":
            section = headers.get(line)
            continue
        if m := re.match(r"--- (\S+):(\d+)-\d+  ", line):
            items["tests" if section == "tests" else "source"].append((m[1], int(m[2])))
        elif section == "symbols" and (m := re.match(r"\d+\. .* — (\S+):(\d+)-\d+$", line)):
            items["symbols"].append((m[1], int(m[2])))
        elif section == "tests" and (m := re.match(r"- .* — (\S+):(\d+)-\d+$", line)):
            items["tests"].append((m[1], int(m[2])))
        elif section == "more" and (m := re.match(r"- .* — (\S+):(\d+)$", line)):
            items["more"].append((m[1], int(m[2])))
    return items


def file_tokens(root: Path, paths) -> int:
    return sum(estimate_tokens((root / p).read_text(errors="replace")) for p in paths)


def main() -> None:
    root, query, budget = Path(sys.argv[1]).resolve(), sys.argv[2], int(sys.argv[3])
    cfg = Config.load(root)
    store = Store(RepoPaths(root).db)
    trace: dict = {}
    instrument(trace)
    text = C.build_context(store, root, cfg, query, budget)
    ranked = R.retrieve(store, cfg, query).ranked

    nodes = store.all_nodes(include_memory=True)
    by_id = {n.id: n for n in nodes}
    by_key = {n.key: n.id for n in nodes}
    by_span = {(n.file_path, n.start_line): n.id for n in nodes if n.type not in ("module", "file")}

    # Clusters: one per file, symbols ordered by importance so hubs sit in the middle.
    drawn = [n for n in nodes if n.type not in ("memory",) and n.file_path]
    files = sorted({n.file_path for n in drawn})
    file_ix = {f: i for i, f in enumerate(files)}
    drawn.sort(key=lambda n: (file_ix[n.file_path], -n.importance))
    node_ix = {n.id: i for i, n in enumerate(drawn)}
    kinds = {"module": 0, "file": 0, "class": 1, "function": 1, "method": 1, "test": 2, "documentation": 3}

    links, file_links = [], Counter()
    for s, t, rel, _ in store.all_edges():
        if s in node_ix and t in node_ix and rel in DRAWN_RELATIONS:
            links.append([node_ix[s], node_ix[t], DRAWN_RELATIONS.index(rel)])
        if s in node_ix and t in node_ix and rel != "contains":
            a, b = file_ix[by_id[s].file_path], file_ix[by_id[t].file_path]
            if a != b:
                file_links[(min(a, b), max(a, b))] += 1

    # Stages.
    semantic = [by_key[k] for k in trace.get("semantic_keys", []) if k in by_key]
    hits = [nid for nid in dict.fromkeys(trace["keyword"] + semantic) if by_id[nid].type != "memory"]
    seeds = set(trace["seeds"])
    hop = {nid: 0 for nid in seeds}

    def hops(nid: int) -> int:
        if nid not in hop:
            src = trace["walk"][nid][0]
            hop[nid] = 1 if src is None else hops(src) + 1
        return hop[nid]

    walk = [[node_ix[nid], node_ix[src], rel, hops(nid)]
            for nid, (src, rel) in trace["walk"].items()
            if src is not None and nid not in seeds and nid in node_ix and src in node_ix]

    items = briefing_items(text)
    picked = {sec: [node_ix[by_span[span]] for span in spans if by_span.get(span) in node_ix]
              for sec, spans in items.items()}
    memories = [m for m in store.all_memories() if f"({m.uid})" in text]
    shown = {i for w in walk for i in w[:2]} | {i for v in picked.values() for i in v}
    shown |= {node_ix[n] for n in hits + trace["seeds"] if n in node_ix}
    labels = {i: C.short_name(drawn[i]) for i in sorted(shown)}
    footer = re.search(r"~(\d+) tokens used / (\d+) \(retrieval: (\w+)\)", text)

    # Tokens an agent would read without ContextGraph.
    indexed = [p for p, in store.conn.execute("SELECT path FROM files")]
    word = sys.argv[4] if len(sys.argv) > 4 else "revoke"
    grep = subprocess.run(["grep", "-rl", word, "--include=*.py", "."], cwd=root,
                          capture_output=True, text=True).stdout.split()
    grep = [g.removeprefix("./") for g in grep]
    answer_files = sorted({by_id[by_span[s]].file_path for s in items["symbols"] + items["source"]
                           if s in by_span and not is_test_path(s[0])})

    counts = store.counts()["nodes_by_type"]
    data = {
        "meta": {
            "repo": root.name, "query": query, "budget": budget, "mode": footer[3],
            "commit": subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root,
                                     capture_output=True, text=True).stdout.strip(),
            "files": len(indexed), "symbols": sum(counts.get(k, 0) for k in ("class", "function", "method")),
            "tests": counts.get("test", 0), "edges": sum(store.counts()["edges_by_relation"].values()),
        },
        "files": [[f, is_test_path(f)] for f in files],
        "fileLinks": [[a, b, w] for (a, b), w in file_links.items()],
        "nodes": [[file_ix[n.file_path], kinds.get(n.type, 1), n.name] for n in drawn],
        "links": links,
        "relations": list(DRAWN_RELATIONS),
        "labels": labels,
        "stages": {
            "keyword": len(trace["keyword"]), "semantic": len(semantic),
            "hits": [node_ix[n] for n in hits if n in node_ix],
            "seeds": [node_ix[n] for n in trace["seeds"] if n in node_ix],
            "walk": walk,
            "ranked": [node_ix[s.node.id] for s in ranked if s.node.id in node_ix],
            "picked": picked,
            "memories": [{"uid": m.uid, "topic": m.topic, "about": [node_ix[t] for t, rel, _ in
                          store.out_edges(by_key[f"memory:{m.uid}"]) if rel == "memory_about" and t in node_ix]}
                         for m in memories],
        },
        "tokens": {
            "repo": file_tokens(root, indexed), "repoFiles": len(indexed),
            "grep": file_tokens(root, grep), "grepFiles": len(grep), "grepWord": word,
            "answer": file_tokens(root, answer_files), "answerFiles": answer_files,
            "briefing": int(footer[1]),
        },
        "briefing": text,
    }
    out = HERE / "data.js"
    out.write_text("window.DATA = " + json.dumps(data, separators=(",", ":")) + ";\n")
    s = data["stages"]
    print(f"{out}: {len(drawn)} nodes, {len(links)} links, {len(files)} files")
    print(f"hits {len(s['hits'])} (keyword {s['keyword']}, semantic {s['semantic']}), seeds {len(s['seeds'])}, "
          f"walk {len(s['walk'])}, ranked {len(s['ranked'])}, picked " +
          ", ".join(f"{k} {len(v)}" for k, v in picked.items()) + f", memories {len(memories)}")
    print("tokens:", {k: v for k, v in data["tokens"].items() if k != "answerFiles"}, data["tokens"]["answerFiles"])


if __name__ == "__main__":
    main()
