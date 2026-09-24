from __future__ import annotations

from dataclasses import dataclass, field

# Node types that are code symbols, and those plus modules.
SYMBOL_TYPES = {"class", "function", "method"}
CODE_TYPES = SYMBOL_TYPES | {"module"}


@dataclass
class Node:
    key: str
    type: str  # file | module | class | function | method | test | documentation | memory
    name: str
    qualname: str
    file_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    signature: str | None = None
    docstring: str | None = None
    is_public: bool = True
    content_hash: str | None = None
    parent_key: str | None = None
    # Populated when loaded from the store.
    id: int | None = None
    summary: str | None = None
    importance: float = 0.0

    @property
    def location(self) -> str:
        if not self.file_path:
            return ""
        if self.start_line:
            return f"{self.file_path}:{self.start_line}"
        return self.file_path

    @property
    def span(self) -> str:
        if self.start_line and self.end_line and self.end_line != self.start_line:
            return f"{self.file_path}:{self.start_line}-{self.end_line}"
        return self.location


@dataclass
class RawRef:
    """An unresolved reference found while parsing; the linker turns these into edges.

    kind: import | call | inherit | reference | mention | path_mention
    name: the reference as written in the file (file-local dotted name, alias for imports)
    target: for imports, the fully qualified dotted target; otherwise unused
    scope: qualname of the enclosing class, used to resolve self./cls. references
    """

    src_key: str
    kind: str
    name: str
    target: str | None = None
    scope: str | None = None


@dataclass
class FileResult:
    path: str
    language: str
    content_hash: str
    nodes: list[Node] = field(default_factory=list)
    refs: list[RawRef] = field(default_factory=list)


@dataclass
class Memory:
    uid: str
    topic: str | None
    content: str
    created_at: str
    files: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    id: int | None = None
    stale: bool = False
