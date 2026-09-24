from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path

LANGUAGES = {
    ".py": "python",
    ".md": "markdown",
    ".mdx": "markdown",
}


def language_for(path: str) -> str | None:
    return LANGUAGES.get(os.path.splitext(path)[1].lower())


def _git_files(root: Path) -> list[str] | None:
    """Tracked + untracked-but-not-ignored files, relative to `root`. None if not a git repo."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return sorted({p for p in out.stdout.decode(errors="replace").split("\0") if p})


def _ignored(rel: str, patterns: list[str]) -> bool:
    parts = rel.split("/")
    for pat in patterns:
        if pat.endswith("/"):
            d = pat.rstrip("/")
            if d in parts[:-1]:
                return True
        elif fnmatch.fnmatch(parts[-1], pat) or fnmatch.fnmatch(rel, pat):
            return True
    return False


def crawl(root: Path, ignore: list[str], max_bytes: int) -> list[str]:
    """Repo-relative POSIX paths of indexable files."""
    candidates = _git_files(root)
    if candidates is None:
        candidates = []
        skip_dirs = {p.rstrip("/") for p in ignore if p.endswith("/")}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
            for f in filenames:
                candidates.append(Path(dirpath, f).relative_to(root).as_posix())
    out = []
    for rel in candidates:
        if language_for(rel) is None or _ignored(rel, ignore):
            continue
        full = root / rel
        try:
            size = full.stat().st_size
        except OSError:
            continue
        if size > max_bytes or not full.is_file():
            continue
        out.append(rel)
    return sorted(out)
