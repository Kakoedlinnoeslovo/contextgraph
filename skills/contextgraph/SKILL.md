---
name: contextgraph
description: Repository knowledge graph + persistent memory for this codebase. Use BEFORE broad exploration (many greps / opening many files) when the repo has a .contextgraph/ directory - to understand architecture, find where something is implemented, locate callers/callees/tests of a symbol, or plan a multi-file change. Returns compact, token-budgeted context with exact path:line pointers, plus facts saved by previous sessions.
---

# ContextGraph

`contextgraph` is a CLI that has already indexed this repository (symbols, imports,
calls, inheritance, tests, docs, and memories from earlier sessions). It exists to
save you from re-reading the codebase. It assists your normal tools — it does not
replace grep/read.

## Step 1 — do this first, before any Grep/Glob/Read

Run this via Bash, with the user's task in your own words:

    contextgraph context "<the task>"

Read its output fully. It lists the relevant symbols with summaries, their
relationships, source excerpts, related tests and docs, and saved memories.
For a large multi-file change, add `--budget 10000`.

## Step 2 — narrow down only if needed

- Around one symbol (callers, callees, bases, subclasses, tests, docs, memories):
  `contextgraph expand <Name | Class.method | path:line>`. Add `--source` to see the code.
  In its output, `~` marks a low-confidence (name-based) link.
- Where does X live: `contextgraph search "<words or identifiers>"`
- Vague task and first time in the repo: `contextgraph map`

## How to use the results

1. Trust the `path:line` pointers: read only those line ranges instead of whole
   files. Items under `MORE` exist but were cut for budget.
2. `MEMORY` entries are facts saved by earlier sessions. Entries marked
   "may be outdated" point to code that changed since - verify before relying on them.
3. If results look irrelevant, rephrase once with concrete identifiers, then fall back
   to grep/glob. Don't loop on the tool.
4. After you edit files, run `contextgraph index -q` before querying again.

## Before finishing

If you discovered a stable, non-obvious fact that would save the next session real
exploration (an invariant, a hidden coupling, where a behaviour actually lives), save it:

    contextgraph remember --topic <short-topic> --text "<one or two sentences>" \
      --files <path1,path2> [--symbols <Name1,Name2>]

Do not store task-specific notes, things obvious from names, or anything temporary.
