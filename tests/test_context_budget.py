import pytest

from contextgraph.config import Config
from contextgraph.context import build_context
from contextgraph.indexer import index_repo
from contextgraph.retrieval import retrieve
from contextgraph.store import Store
from contextgraph.util import estimate_tokens


@pytest.mark.parametrize("budget", [300, 800, 1500, 5000])
def test_context_respects_budget(repo, store, budget):
    out = build_context(store, repo, Config(), "how does autoscaling decide the worker count", budget)
    assert estimate_tokens(out) <= budget
    assert "QUERY" in out and f"/ {budget}" in out


def test_context_contains_key_sections(repo, store):
    out = build_context(store, repo, Config(), "how does autoscaling decide the worker count", 3000)
    for section in ("RELEVANT SYMBOLS", "RELATIONSHIPS", "SOURCE", "TESTS"):
        assert section in out
    assert "src/fleet/scaler/autoscaler.py:" in out


def test_tests_that_do_not_fit_are_listed_under_more(repo):
    body = "".join(f"def test_reconcile_case_{i}():\n    Autoscaler(None, None).reconcile()\n\n" for i in range(20))
    (repo / "tests" / "test_reconcile_many.py").write_text("from fleet.scaler.autoscaler import Autoscaler\n\n" + body)
    index_repo(repo)
    s = Store(repo / ".contextgraph" / "contextgraph.db")
    query = "reconcile case tests"
    out = build_context(s, repo, Config(), query, 4000)
    ranked_tests = [r.node.name for r in retrieve(s, Config(), query).ranked if r.node.type == "test"]
    s.close()
    assert len(ranked_tests) > 12  # more than the TESTS section shows
    tests_and_more = out.split("\nTESTS\n", 1)[1].split("\nDOCS\n")[0] + out.split("\nMORE (", 1)[1]
    for name in ranked_tests:
        assert name in tests_and_more, f"{name} is neither in TESTS nor in MORE"
