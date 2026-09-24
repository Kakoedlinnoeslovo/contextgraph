# ContextGraph

A local code graph and memory for Claude Code and Codex. One command gives the agent the code that matters for a task (symbols, how they connect, source excerpts, related tests, and what earlier sessions learned) within a token budget, so it spends less time grepping around your repo.

In a test on [celery](https://github.com/celery/celery) (110k lines of Python), Claude Code answered an architecture question with 61% fewer input tokens and 38% lower cost when it used ContextGraph. That was a single run, so treat it as a hint, not a guarantee.

## Install

Requires Python 3.12+.

```bash
uv tool install "contextgraph[embed] @ git+https://github.com/Kakoedlinnoeslovo/contextgraph"
# or: pipx install "contextgraph[embed] @ git+https://github.com/Kakoedlinnoeslovo/contextgraph"
```

The `embed` extra adds local semantic search (fastembed, runs on your CPU). Without it, search is keyword-only and everything else still works. The first `index` downloads a small embedding model to `~/.cache/contextgraph/`, and on a large repo computing the embeddings can take a few minutes; later runs only embed what changed.

## Quickstart

In the root of the repository you work on:

```bash
contextgraph index            # builds .contextgraph/
contextgraph install-skill    # adds the skill for Claude Code (.claude/skills) and Codex (.agents/skills)
```

Now use Claude Code or Codex as usual. The skill tells the agent to run `contextgraph context "<task>"` before exploring, and then to read only the `path:line` ranges it needs. You can run the same command yourself to see what the agent gets.

## What the agent sees

Real output on celery, trimmed (`…` marks cuts):

```text
$ contextgraph context "What happens when a worker receives a revoke request?" --budget 3000

RELEVANT SYMBOLS
1. Mingle.on_revoked_received (method) — celery/worker/consumer/mingle.py:78-80
   def on_revoked_received(self, c, revoked). Calls: merge_revoked. Called by: Mingle.sync_with_node.
2. Request.revoked (method) — celery/worker/request.py:476-515
   def revoked(self). If revoked, skip task and mark state. Calls: Request._announce_revoked, …
…
MEMORY (from previous sessions)
- (5e7b5c5e) revoke-limits: Revoked ids live in worker_state.revoked, a LimitedSet capped at …

RELATIONSHIPS
- Mingle.on_revoked_received calls merge_revoked
- _revoke tested by test_revoke_skips_active_request_with_terminate
…
SOURCE
--- celery/worker/consumer/mingle.py:78-80  celery.worker.consumer.mingle.Mingle.on_revoked_received
    def on_revoked_received(self, c, revoked):
        if revoked:
            c.controller.state.merge_revoked(revoked)
…
TESTS
- test_revoke_skips_active_request_with_terminate — t/unit/worker/test_control.py:778-795

MORE (not included; read with path:line)
- Control.revoke (method) — celery/app/control.py:488
…
~2926 tokens used / 3000 (retrieval: hybrid)
```

## Commands

| Command | What it does |
|---|---|
| `context "<task>" [--budget N]` | Everything relevant to a task, packed into about N tokens (default 5000) |
| `search "<query>"` | Ranked symbols, files and memories |
| `expand <Name \| Class.method \| path:line> [--source]` | One symbol's callers, callees, bases, subclasses, tests, docs and memories |
| `map [--budget N]` | Overview of the repo's most central modules and classes |
| `remember --text "…" [--topic T] [--files a,b] [--symbols X,Y]` | Save a discovery for future sessions |
| `memories ["<query>"]` / `forget <id>` | List, search or delete memories |
| `index [--force] [--no-embed] [-q]` | Build or update the index |
| `stats` | What's in the index |
| `install-skill [--agent claude\|codex\|all] [--user]` | Install the agent skill (`--user`: for all your projects) |

Commands work from any subfolder of the repo. Use `-C <path>` to point at another repository.

## Memories

`contextgraph remember` saves a fact ("workers only count as ready after model init") and links it to the code it is about. Later sessions get it back in `context` output. When that code changes, the memory is flagged *may be outdated* instead of silently misleading the agent.

## Files and settings

Everything lives in `.contextgraph/` in your repo:

| File | |
|---|---|
| `contextgraph.db` | The index. A rebuildable cache, git-ignored automatically. |
| `memories.jsonl` | The memories. Commit it to share them with your team. |
| `config.toml` | Optional settings, see below. |

The index only re-parses files that changed, so re-run `contextgraph index -q` after edits (the skill tells agents to do this). To also refresh it at the start of every Claude Code session, add this hook to `.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{ "hooks": [{ "type": "command", "command": "contextgraph index -q" }] }]
  }
}
```

Files ignored by git and common build folders (`node_modules/`, `.venv/`, `dist/`, …) are skipped. To change what gets indexed, create `.contextgraph/config.toml`:

```toml
extra_ignore = ["vendor/", "*_generated.py"]  # skip these too
default_budget = 8000                          # tokens for `context` when --budget isn't given
```

## How it works

1. **Parse.** Python is parsed with the standard `ast` module, and Markdown into one node per heading. Light type inference resolves calls like `self.manager.scale()` to `WorkerManager.scale`.
2. **Link.** References are resolved across the whole repo (imports, re-exports, inheritance, `super()`), and `tested_by` and `documented_by` links are derived from tests and docs. Guesses based only on a name are marked low-confidence.
3. **Rank.** PageRank over the dependency graph finds the central code.
4. **Retrieve.** Keyword search (SQLite FTS5) and local embeddings pick starting points, a 2-hop graph expansion adds their neighbours, and the result is packed section by section into the token budget.

Everything runs locally: a single SQLite file, no server, no API key.

## Limitations

- Only Python and Markdown files are indexed; other files are skipped.
- Call resolution is heuristic. Dynamic dispatch, dependency injection and metaprogramming produce missing or low-confidence links.
- Agents don't always use the skill. Whether they call it depends on the model, the prompt and the task.

## Development

```bash
uv sync --all-extras
uv run pytest
```

## License

[Apache-2.0](LICENSE)
