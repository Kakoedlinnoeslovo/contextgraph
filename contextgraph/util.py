"""Small shared helpers: hashing, identifier splitting, token estimates, path classification."""

from __future__ import annotations

import hashlib
import math
import re

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")

STOPWORDS = frozenset(
    "a an and are as at be by can could do does did for from has have how i if in into is it its "
    "me my of on or our should so than that the their then there these this those to was we what "
    "when where which while who why will with would you your does work works working "
    "function functions class classes method methods implemented implement implementation "
    "find show explain happen happens use used using get set make repo repository".split()
)


def sha1(text: str | bytes) -> str:
    data = text.encode() if isinstance(text, str) else text
    return hashlib.sha1(data).hexdigest()


def split_identifier(name: str) -> list[str]:
    """WorkerManager.scale_up -> [worker, manager, scale, up]"""
    parts: list[str] = []
    for chunk in re.split(r"[^A-Za-z0-9]+", name):
        if not chunk:
            continue
        parts.extend(p.lower() for p in _CAMEL.split(chunk) if p)
    return parts


def identifier_tokens(*names: str) -> str:
    """Space-joined identifier parts plus the raw identifiers, for FTS indexing."""
    seen: list[str] = []
    for name in names:
        if not name:
            continue
        for tok in [name.lower(), *split_identifier(name)]:
            if tok not in seen:
                seen.append(tok)
    return " ".join(seen)


def query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for word in _WORD.findall(query):
        for tok in {word.lower(), *split_identifier(word)}:
            if len(tok) > 1 and tok not in STOPWORDS and tok not in terms:
                terms.append(tok)
    return terms


def fts_match_expr(query: str) -> str | None:
    terms = query_terms(query)
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in terms)


def first_sentence(text: str | None, max_chars: int = 200) -> str:
    if not text:
        return ""
    para = text.strip().split("\n\n")[0]
    para = " ".join(para.split())
    m = re.search(r"(?<=[.!?])\s", para)
    sentence = para[: m.start()] if m else para
    if len(sentence) > max_chars:
        sentence = sentence[: max_chars - 1].rstrip() + "…"
    return sentence


# Claude's tokenizer averages roughly 3.3-4 characters per token on code and
# English prose. 3.3 errs on the side of over-estimating so budgets hold.
CHARS_PER_TOKEN = 3.3


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


_TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec|specs|testing|t)/|(^|/)test_[^/]*$|_test\.py$|(^|/)conftest\.py$")
_AUX_DIR = re.compile(r"(^|/)(examples?|samples?|demos?|benchmarks?|scripts|docs?|fixtures|migrations)/")


def is_test_path(path: str) -> bool:
    return bool(_TEST_FILE.search(path))


def is_auxiliary_path(path: str) -> bool:
    """Examples, demos, scripts, docs code: useful, but rarely where behaviour is implemented."""
    return bool(_AUX_DIR.search(path))


def segment_hash(lines: list[str], start: int, end: int) -> str:
    return sha1("\n".join(lines[start - 1:end]))

