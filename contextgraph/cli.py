from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path
from typing import Optional

import typer

from contextgraph import memory as mem
from contextgraph.config import Config, RepoPaths, find_repo_root
from contextgraph.context import build_context
from contextgraph.indexer import index_repo
from contextgraph.linker import Linker
from contextgraph.models import SYMBOL_TYPES, Memory
from contextgraph.render import (
    build_map,
    render_expand,
    render_memories,
    render_no_match,
    render_search,
    render_stats,
    resolve_symbol,
)
from contextgraph.retrieval import retrieve
from contextgraph.store import Store

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="A local code graph and memory for coding agents: the code, links, tests and past findings "
         "that matter for a task, within a token budget.",
)

_repo: Path | None = None  # set by the global --repo/-C option


@app.callback()
def _root(repo: Optional[Path] = typer.Option(None, "--repo", "-C", help="Repository root (default: nearest dir with .contextgraph/).")):
    global _repo
    _repo = repo.resolve() if repo else None


def _repo_root(path: Path | None = None) -> Path:
    """-C, else PATH, else the nearest directory with an index, else the working directory."""
    return (_repo or path or find_repo_root() or Path.cwd()).resolve()


def _require_index() -> tuple[Path, Store]:
    root = _repo or find_repo_root()
    if root is None or not RepoPaths(root).db.exists():
        typer.echo("No ContextGraph index found. Run `contextgraph index` in the repository root first.", err=True)
        raise typer.Exit(2)
    return root, Store(RepoPaths(root).db)


def _split(value: Optional[str]) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@app.command()
def index(
    path: Optional[Path] = typer.Argument(None, help="Repository root (default: the current index's root, or the current directory)."),
    no_embed: bool = typer.Option(False, "--no-embed", help="Skip embeddings (keyword search only)."),
    force: bool = typer.Option(False, "--force", help="Re-parse every file."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print a single summary line."),
):
    """Index (or incrementally re-index) a repository."""
    root = _repo_root(path)
    t0 = time.perf_counter()
    if not quiet:
        typer.echo(f"Indexing {root} …")
    log = (lambda _m: None) if quiet else (lambda m: typer.echo(m, err=True))
    rep = index_repo(root, embed=not no_embed, force=force, log=log)
    elapsed = time.perf_counter() - t0
    if quiet:
        typer.echo(f"contextgraph: {rep.parsed} parsed, {rep.unchanged} unchanged, {rep.deleted} removed ({elapsed:.1f}s)")
        return
    c = rep.counts
    symbols = sum(v for k, v in c["nodes_by_type"].items() if k in SYMBOL_TYPES)
    embedded = f" (+{rep.embedded:,} new)" if rep.embedded else ""
    typer.echo(
        f"Indexed {rep.files_seen:,} files in {elapsed:.1f}s ({rep.parsed:,} parsed, {rep.unchanged:,} unchanged, "
        f"{rep.deleted:,} removed): {symbols:,} symbols, {sum(c['edges_by_relation'].values()):,} edges, "
        f"{c['memories']:,} memories, {c['embeddings']:,} embeddings{embedded}."
    )
    if rep.skipped:
        typer.echo("Skipped: " + ", ".join(f"{v} {k}" for k, v in rep.skipped.items()))
    if not no_embed and rep.embedding_model is None:
        typer.echo("Note: fastembed is not installed, so search is keyword-only. Install the [embed] extra for semantic search.")
    typer.echo("Run `contextgraph stats` for details.")


@app.command()
def stats():
    """Show index statistics."""
    root, store = _require_index()
    typer.echo(render_stats(store, RepoPaths(root).db), nl=False)


@app.command()
def search(query: str, limit: int = typer.Option(10, "--limit", "-n")):
    """Find the symbols, files and memories most relevant to a question."""
    root, store = _require_index()
    typer.echo(render_search(store, retrieve(store, Config.load(root), query), limit), nl=False)


@app.command()
def context(query: str, budget: int = typer.Option(None, "--budget", "-b", help="Approximate token budget (default 5000).")):
    """Compact, token-budgeted context for a task: summaries, memories, relations, source, tests, docs."""
    root, store = _require_index()
    cfg = Config.load(root)
    typer.echo(build_context(store, root, cfg, query, budget or cfg.default_budget), nl=False)


@app.command("map")
def map_(budget: int = typer.Option(1500, "--budget", "-b")):
    """Ranked overview of the repository's most central modules and symbols."""
    _, store = _require_index()
    typer.echo(build_map(store, budget), nl=False)


@app.command()
def expand(symbol: str, source: bool = typer.Option(False, "--source", "-s", help="Include the symbol's source.")):
    """Show a symbol's relationships: callers, callees, bases, tests, docs, memories.

    SYMBOL can be a name (Autoscaler), a dotted suffix (Autoscaler.reconcile), a
    qualified name, or path:line.
    """
    root, store = _require_index()
    node, others = resolve_symbol(store, symbol)
    if node is None:
        typer.echo(render_no_match(symbol, others), nl=False)
        raise typer.Exit(1)
    typer.echo(render_expand(store, node, others, source, root), nl=False)


@app.command()
def remember(
    text: str = typer.Option(..., "--text", "-t", help="The fact to remember (stable, non-obvious, useful later)."),
    topic: Optional[str] = typer.Option(None, "--topic"),
    files: Optional[str] = typer.Option(None, "--files", help="Comma-separated repo-relative paths it is about."),
    symbols: Optional[str] = typer.Option(None, "--symbols", help="Comma-separated symbol names it is about."),
):
    """Store a durable discovery so future sessions retrieve it."""
    root, store = _require_index()
    memory = Memory(uid=mem.new_uid(), topic=topic, content=text.strip(), created_at=mem.now_iso(),
                    files=_split(files), symbols=_split(symbols))
    targets = mem.resolve_targets(store, Linker(store), memory)
    store.close()
    mem.append_record(RepoPaths(root).memories, {
        "uid": memory.uid, "topic": memory.topic, "text": memory.content, "files": memory.files,
        "symbols": memory.symbols, "created_at": memory.created_at, "anchors": mem.anchors_for(targets),
    })
    index_repo(root)
    linked = ", ".join(sorted({t.qualname for t in targets})[:8]) or "nothing (no matching files/symbols)"
    typer.echo(f"Remembered ({memory.uid}). Linked to: {linked}")


@app.command()
def memories(query: Optional[str] = typer.Argument(None)):
    """List stored memories, or those relevant to QUERY."""
    root, store = _require_index()
    found = retrieve(store, Config.load(root), query).memories if query else store.all_memories()
    typer.echo(render_memories(found, query), nl=False)


@app.command()
def forget(uid: str):
    """Delete a memory by id."""
    root, store = _require_index()
    known = {m.uid for m in store.all_memories()}
    store.close()
    if uid not in known:
        typer.echo(f"No memory with id {uid}.", err=True)
        raise typer.Exit(1)
    mem.append_record(RepoPaths(root).memories, {"uid": uid, "deleted": True, "created_at": mem.now_iso()})
    index_repo(root)
    typer.echo(f"Forgot {uid}.")


def skill_source() -> Path:
    """SKILL.md: bundled into the wheel as contextgraph/_skill/, or skills/contextgraph/ in a source checkout."""
    packaged = Path(__file__).resolve().parent / "_skill" / "SKILL.md"
    if packaged.exists():
        return packaged
    return Path(__file__).resolve().parents[1] / "skills" / "contextgraph" / "SKILL.md"


@app.command("install-skill")
def install_skill(
    agent: str = typer.Option("all", "--agent", help="claude | codex | all"),
    user: bool = typer.Option(False, "--user", help="Install for your user instead of this repository."),
):
    """Install the ContextGraph skill for Claude Code (.claude/skills) and/or Codex (.agents/skills)."""
    src = skill_source()
    if not src.exists():
        typer.echo(f"Skill file not found at {src}", err=True)
        raise typer.Exit(1)
    base = Path.home() if user else _repo_root()
    targets = {"claude": base / ".claude" / "skills" / "contextgraph",
               "codex": base / ".agents" / "skills" / "contextgraph"}
    chosen = list(targets) if agent == "all" else [agent]
    for name in chosen:
        if name not in targets:
            typer.echo(f"Unknown agent '{name}'", err=True)
            raise typer.Exit(1)
        targets[name].mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, targets[name] / "SKILL.md")
        typer.echo(f"Installed {name} skill → {targets[name] / 'SKILL.md'}")


def main() -> None:
    try:
        app()
    except BrokenPipeError:  # e.g. `contextgraph context … | head`
        sys.stderr.close()


if __name__ == "__main__":
    main()
