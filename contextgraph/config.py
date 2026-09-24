from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

INDEX_DIR = ".contextgraph"
DB_NAME = "contextgraph.db"
MEMORIES_FILE = "memories.jsonl"
CONFIG_FILE = "config.toml"

DEFAULT_IGNORE = [
    "node_modules/", ".git/", ".venv/", "venv/", "env/", "__pycache__/", "dist/", "build/",
    ".mypy_cache/", ".pytest_cache/", ".ruff_cache/", ".tox/", ".next/", "coverage/",
    ".contextgraph/", "site-packages/", "*_pb2.py", "*_pb2_grpc.py",
]


@dataclass
class Config:
    """Settings a repository can override in .contextgraph/config.toml (all optional).

    `ignore` replaces the default ignore patterns; `extra_ignore` adds to them.
    """

    ignore: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE))
    max_file_bytes: int = 512_000
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    default_budget: int = 5000

    @classmethod
    def load(cls, repo_root: Path) -> Config:
        cfg = cls()
        path = repo_root / INDEX_DIR / CONFIG_FILE
        if not path.exists():
            return cfg
        data = tomllib.loads(path.read_text())
        for f in fields(cls):
            if f.name in data:
                setattr(cfg, f.name, data[f.name])
        cfg.ignore = [*cfg.ignore, *data.get("extra_ignore", [])]
        return cfg


@dataclass
class RepoPaths:
    root: Path

    @property
    def index_dir(self) -> Path:
        return self.root / INDEX_DIR

    @property
    def db(self) -> Path:
        return self.index_dir / DB_NAME

    @property
    def memories(self) -> Path:
        return self.index_dir / MEMORIES_FILE


def find_repo_root() -> Path | None:
    """Nearest ancestor of the working directory (inclusive) that contains a .contextgraph directory."""
    cur = Path.cwd().resolve()
    for p in [cur, *cur.parents]:
        if (p / INDEX_DIR).is_dir():
            return p
    return None
