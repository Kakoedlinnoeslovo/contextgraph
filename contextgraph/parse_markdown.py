"""Markdown indexer: one documentation node per heading section.

Backticked identifiers and source-path mentions become `mention` /
`path_mention` raw refs, which the linker turns into `documented_by` edges.
"""

from __future__ import annotations

import re

from contextgraph.models import FileResult, Node, RawRef
from contextgraph.util import segment_hash, sha1

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_BACKTICK = re.compile(r"`([A-Za-z_][\w.]*?)(?:\(\))?`")
_PATH = re.compile(r"(?<![\w/])((?:[\w.-]+/)+[\w.-]+\.(?:py|md))\b")
_LINK = re.compile(r"\]\(([^)#\s]+)")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"


class MarkdownParser:
    def parse(self, path: str, source: str) -> FileResult:
        lines = source.splitlines()
        result = FileResult(path=path, language="markdown", content_hash=sha1(source))
        file_key = f"file:{path}:{path}"
        result.nodes.append(Node(
            key=file_key, type="file", name=path.rsplit("/", 1)[-1], qualname=path, file_path=path,
            start_line=1, end_line=max(len(lines), 1), content_hash=result.content_hash,
        ))

        headings: list[tuple[int, int, str]] = []  # (line_no, level, title)
        in_fence = False
        for i, line in enumerate(lines, start=1):
            if _FENCE.match(line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            m = _HEADING.match(line)
            if m:
                headings.append((i, len(m.group(1)), m.group(2).strip()))

        if not headings:
            headings = [(1, 1, path.rsplit("/", 1)[-1])]
        slugs: dict[str, int] = {}
        stack: list[tuple[int, str]] = []  # (level, node_key)
        for idx, (line_no, level, title) in enumerate(headings):
            end = headings[idx + 1][0] - 1 if idx + 1 < len(headings) else len(lines)
            slug = _slug(title)
            slugs[slug] = slugs.get(slug, 0) + 1
            if slugs[slug] > 1:
                slug = f"{slug}-{slugs[slug]}"
            qual = f"{path}#{slug}"
            key = f"documentation:{path}:{slug}"
            while stack and stack[-1][0] >= level:
                stack.pop()
            parent = stack[-1][1] if stack else file_key
            body_lines = lines[line_no:end]
            body = "\n".join(body_lines)
            result.nodes.append(Node(
                key=key, type="documentation", name=title, qualname=qual, file_path=path,
                start_line=line_no, end_line=max(end, line_no), docstring=_first_paragraph(body_lines),
                content_hash=segment_hash(lines, line_no, end), parent_key=parent,
            ))
            stack.append((level, key))
            seen: set[tuple[str, str]] = set()
            for m in _BACKTICK.finditer(body):
                ident = m.group(1).strip(".")
                if len(ident) >= 3 and ("mention", ident) not in seen:
                    seen.add(("mention", ident))
                    result.refs.append(RawRef(key, "mention", ident))
            for m in [*_PATH.finditer(body), *_LINK.finditer(body)]:
                p = m.group(1).lstrip("./")
                if "://" in p or ("path_mention", p) in seen:
                    continue
                seen.add(("path_mention", p))
                result.refs.append(RawRef(key, "path_mention", p))
        return result


def _first_paragraph(lines: list[str], max_chars: int = 400) -> str | None:
    para: list[str] = []
    in_fence = False
    for line in lines:
        if _FENCE.match(line):
            in_fence = not in_fence
            if para:
                break
            continue
        if in_fence:
            continue
        s = line.strip()
        if not s:
            if para:
                break
            continue
        if _HEADING.match(s):
            break
        para.append(s)
    text = " ".join(para)
    if not text:
        return None
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"
