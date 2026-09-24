from pathlib import Path

from typer.testing import CliRunner

from contextgraph.cli import app
from contextgraph.memory import load_jsonl
from contextgraph.store import Store

runner = CliRunner()
SKILL = Path(__file__).resolve().parents[1] / "skills" / "contextgraph" / "SKILL.md"


def cli(repo, *args):
    result = runner.invoke(app, ["--repo", str(repo), *args])
    assert result.exit_code == 0, result.output
    return result.output


def test_remember_links_and_survives_rebuild(repo):
    out = cli(repo, "remember", "--topic", "worker-readiness",
              "--text", "Workers count as ready only after mark_ready runs at the end of model init.",
              "--files", "src/fleet/workers/manager.py")
    assert "Worker.mark_ready" in out
    mems = load_jsonl(repo / ".contextgraph" / "memories.jsonl")
    assert len(mems) == 1 and mems[0][1]  # anchors recorded
    # The DB is a cache: deleting it and re-indexing restores the memory.
    for p in (repo / ".contextgraph").glob("contextgraph.db*"):
        p.unlink()
    cli(repo, "index", "--quiet")
    out = cli(repo, "memories", "why is a worker not ready yet")
    assert "mark_ready" in out and "outdated" not in out
    out = cli(repo, "expand", "Worker.mark_ready")
    assert "MEMORIES" in out and "model init" in out


def test_memory_goes_stale_when_code_changes(repo):
    cli(repo, "remember", "--text", "Autoscaler.desired rounds queue depth up per worker.",
        "--symbols", "Autoscaler.desired")
    f = repo / "src/fleet/scaler/autoscaler.py"
    f.write_text(f.read_text().replace("want = -(-depth", "want = (depth"))
    cli(repo, "index", "--quiet")
    s = Store(repo / ".contextgraph" / "contextgraph.db")
    [m] = s.all_memories()
    assert m.stale
    s.close()
    assert "may be outdated" in cli(repo, "memories")


def test_forget(repo):
    out = cli(repo, "remember", "--text", "Temporary fact about EndpointConfig bounds.")
    uid = out.split("(")[1].split(")")[0]
    cli(repo, "forget", uid)
    assert "No memories" in cli(repo, "memories")


def test_map_and_stats(repo):
    assert "src/fleet/workers/manager.py" in cli(repo, "map", "--budget", "300")
    out = cli(repo, "stats")
    assert "Symbols" in out and "python" in out


def test_expand_ambiguous_and_path_line(repo):
    assert "OTHER MATCHES" in cli(repo, "expand", "reconcile")
    out = cli(repo, "expand", "src/fleet/workers/manager.py:27")
    assert out.startswith("fleet.workers.manager.WorkerManager.scale (method)")


def test_index_from_subfolder_reuses_root_index(repo, monkeypatch):
    monkeypatch.chdir(repo / "src" / "fleet")
    result = runner.invoke(app, ["index", "-q"])
    assert result.exit_code == 0, result.output
    assert "0 parsed" in result.output  # the existing root index is up to date
    assert not (repo / "src" / "fleet" / ".contextgraph").exists()


def test_install_skill(repo):
    cli(repo, "install-skill")
    for d in (".claude", ".agents"):
        assert (repo / d / "skills" / "contextgraph" / "SKILL.md").read_text() == SKILL.read_text()
