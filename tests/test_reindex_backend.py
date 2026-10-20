"""Hermetic honesty test for scripts/reindex_brain.py (GRAY-BRN-04).

Proves the reindex tool's advertised backend/dimension equals what
build_index() actually produces:

  1. The default (no --nomic) path pins fastembed, calls build_index()
     exactly once, and reports the dim build_index() returns.
  2. --nomic is rejected honestly: no nomic env is written, build_index()
     is never called, and the printed message conveys "unsupported"
     rather than lying about a nomic/768-d rebuild.
  3. --nomic never writes a nomic-embed-text line to .env.

No real fastembed, no real network, no real Ollama - src.brain_index.build_index
is monkeypatched at its source so scripts.reindex_brain's lazy `from
src.brain_index import build_index` (imported inside main()) picks up the fake.
"""
import scripts.reindex_brain as rb


def _fake_build_index(calls):
    def _build_index(vault, out, *, batch=256, progress=None):
        calls.append({"vault": vault, "out": out, "batch": batch})
        return (3, 384)
    return _build_index


def test_default_path_builds_and_reports_actual_dim(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr("src.brain_index.build_index", _fake_build_index(calls))

    env_file = tmp_path / ".env"
    monkeypatch.setattr(rb, "ENV", str(env_file))
    monkeypatch.setattr(rb, "_vault", lambda: str(tmp_path / "vault"))

    set_env_calls = []
    real_set_env = rb._set_env

    def _spy_set_env(kv):
        set_env_calls.append(dict(kv))
        real_set_env(kv)

    monkeypatch.setattr(rb, "_set_env", _spy_set_env)
    monkeypatch.setattr("sys.argv", ["reindex_brain.py"])

    rc = rb.main()

    assert rc == 0
    assert len(calls) == 1
    assert any(kv.get("EMBEDDING_BACKEND") == "fastembed" for kv in set_env_calls)
    out = capsys.readouterr().out
    assert "384" in out  # reports build_index's ACTUAL returned dim, not a hardcoded claim


def test_nomic_flag_is_rejected_without_lying(tmp_path, monkeypatch, capsys):
    # The dead helper must be gone entirely - not just unused.
    assert not hasattr(rb, "_nomic_live")

    calls = []
    monkeypatch.setattr("src.brain_index.build_index", _fake_build_index(calls))

    env_file = tmp_path / ".env"
    monkeypatch.setattr(rb, "ENV", str(env_file))
    monkeypatch.setattr(rb, "_vault", lambda: str(tmp_path / "vault"))

    set_env_calls = []
    monkeypatch.setattr(rb, "_set_env", lambda kv: set_env_calls.append(dict(kv)))
    monkeypatch.setattr("sys.argv", ["reindex_brain.py", "--nomic"])

    rc = rb.main()

    assert rc != 0
    assert calls == []  # build_index never invoked
    assert not any(
        kv.get("EMBEDDING_MODEL") == "nomic-embed-text" for kv in set_env_calls
    )
    out = capsys.readouterr().out
    assert "supported" in out or "FastEmbed" in out
    # Must never repeat the old false dimension claim.
    assert "768" not in out


def test_nomic_flag_does_not_write_env(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("src.brain_index.build_index", _fake_build_index(calls))

    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING=1\n", encoding="utf-8")
    monkeypatch.setattr(rb, "ENV", str(env_file))
    monkeypatch.setattr(rb, "_vault", lambda: str(tmp_path / "vault"))
    monkeypatch.setattr("sys.argv", ["reindex_brain.py", "--nomic"])

    rc = rb.main()

    assert rc != 0
    contents = env_file.read_text(encoding="utf-8")
    assert "nomic-embed-text" not in contents
    assert "EXISTING=1" in contents  # untouched by the rejected --nomic path
