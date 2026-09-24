from contextgraph.parse_python import PythonParser, module_name_for


def test_module_names():
    assert module_name_for("src/fleet/scaler/autoscaler.py") == "fleet.scaler.autoscaler"
    assert module_name_for("src/fleet/__init__.py") == "fleet"
    assert module_name_for("tests/test_x.py") == "tests.test_x"
    assert module_name_for("app.py") == "app"


def test_extracts_symbols_and_refs():
    src = '''
"""Mod doc."""
from .base import Base
import os.path as osp

class Thing(Base):
    """A thing."""
    helper: "Helper"

    def __init__(self, mgr: "Manager"):
        self.mgr = mgr
        self.cache = Cache()

    @property
    def size(self) -> int:
        return self.cache.count() + self.mgr.total() + self.helper.run_it()

def free(x: Thing | None = None) -> Thing:
    t = Thing(None)
    t.size
    return osp.join("a")
'''
    r = PythonParser().parse("pkg/mod.py", src)
    by_q = {n.qualname: n for n in r.nodes}
    assert by_q["pkg.mod"].type == "module" and by_q["pkg.mod"].docstring == "Mod doc."
    assert by_q["pkg.mod.Thing"].type == "class"
    assert by_q["pkg.mod.Thing.size"].type == "method"
    assert by_q["pkg.mod.Thing.size"].signature.startswith("@property def size(self)")
    assert by_q["pkg.mod.free"].type == "function"
    refs = {(r_.kind, r_.name, r_.target) for r_ in r.refs}
    assert ("import", "Base", "pkg.base.Base") in refs
    assert ("import", "osp", "os.path") in refs
    assert ("inherit", "Base", None) in refs
    calls = {r_.name for r_ in r.refs if r_.kind == "call"}
    # self.attr types come from __init__ assignments, annotated params and class annotations
    assert {"Cache.count", "Manager.total", "Helper.run_it", "Thing", "osp.join"} <= calls
    assert ("reference", "Thing", None) in refs


def test_syntax_error_still_yields_module():
    r = PythonParser().parse("bad.py", "def (:\n")
    assert [n.type for n in r.nodes] == ["module"]


def test_test_functions_are_typed_as_tests():
    r = PythonParser().parse("tests/test_a.py", "def test_x():\n    pass\n\ndef helper():\n    pass\n")
    types = {n.name: n.type for n in r.nodes}
    assert types["test_x"] == "test" and types["helper"] == "function"
