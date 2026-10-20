import importlib
from pathlib import Path
from fastapi.testclient import TestClient


def _vault(tmp_path: Path) -> Path:
    k = tmp_path / "knowledge"
    (k / "security").mkdir(parents=True)
    (k / "python").mkdir(parents=True)
    (k / "security" / "sql-injection.md").write_text(
        "# SQL Injection\nBeware untrusted input. See [[input-validation]].\n", encoding="utf-8")
    (k / "python" / "input-validation.md").write_text(
        "# Input Validation\nValidate everything.\n", encoding="utf-8")
    return tmp_path


def _client(vault, monkeypatch) -> TestClient:
    # Make the env var authoritative: neutralize any saved `brain_vault_path`
    # setting (which `_vault_path()` reads BEFORE the env) so the tests don't
    # silently run against a real vault on a dev's machine. monkeypatch
    # auto-restores env + the patched attr after each test.
    monkeypatch.setenv("PRISMA_BRAIN_VAULT", str(vault))
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr("src.settings.get_setting", lambda key, default="": "", raising=False)
    from fastapi import FastAPI
    import routes.brain_graph_routes as bg
    importlib.reload(bg)
    app = FastAPI()
    app.include_router(bg.router)
    return TestClient(app)


def test_brain_graph_shape_and_links(tmp_path, monkeypatch):
    c = _client(_vault(tmp_path), monkeypatch)
    j = c.get("/api/brain-graph").json()
    ids = {n["id"] for n in j["nodes"]}
    assert "sql-injection" in ids and "input-validation" in ids
    assert any(l["source"] == "sql-injection" and l["target"] == "input-validation"
               for l in j["links"])
    assert j["stats"]["counts"].get("security") == 1
    assert j["stats"]["nodes"] >= 2


def test_brain_graph_discovers_legacy_categories(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    legacy = vault / "knowledge" / "cc_solutions"
    legacy.mkdir()
    (legacy / "recovered-solution.md").write_text(
        "# Recovered Solution\nSee [[input-validation]].\n", encoding="utf-8")

    j = _client(vault, monkeypatch).get("/api/brain-graph").json()
    recovered = next(n for n in j["nodes"] if n["id"] == "recovered-solution")

    assert recovered["type"] == "cc_solutions"
    assert j["stats"]["counts"]["cc_solutions"] == 1
    assert any(
        link["source"] == "recovered-solution"
        and link["target"] == "input-validation"
        for link in j["links"]
    )


def test_duplicate_slugs_get_unique_ids_and_open_by_cluster(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    (vault / "knowledge" / "security" / "shared.md").write_text(
        "# Security Variant\n", encoding="utf-8")
    (vault / "knowledge" / "python" / "shared.md").write_text(
        "# Python Variant\n", encoding="utf-8")

    client = _client(vault, monkeypatch)
    graph = client.get("/api/brain-graph").json()
    shared = [node for node in graph["nodes"] if node.get("slug") == "shared"]

    assert len(shared) == 2
    assert len({node["id"] for node in shared}) == 2
    assert {node["cluster"] for node in shared} == {"security", "python"}
    note = client.get(
        "/api/brain-note", params={"slug": "shared", "cluster": "python"}
    ).json()
    assert note["title"] == "Python Variant"


def test_brain_note_ok_and_safe(tmp_path, monkeypatch):
    c = _client(_vault(tmp_path), monkeypatch)
    ok = c.get("/api/brain-note", params={"slug": "sql-injection"}).json()
    assert ok["slug"] == "sql-injection" and "SQL Injection" in ok["text"]
    assert c.get("/api/brain-note", params={"slug": "../../etc/passwd"}).json().get("error")
    assert c.get("/api/brain-note", params={"slug": "nope"}).json().get("error") == "not found"


def test_no_vault_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISMA_BRAIN_VAULT", "")
    # Brain-graph reads are now gated by can_manage_brain (audit P3); run this
    # vault-logic check in the same auth-disabled mode as the _client helper.
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr("src.settings.get_setting", lambda key, default="": "", raising=False)
    import routes.brain_graph_routes as bg
    importlib.reload(bg)
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(bg.router)
    j = TestClient(app).get("/api/brain-graph").json()
    assert j["nodes"] == [] and j["stats"]["error"] == "no brain vault configured"


def test_wikilink_extraction_requires_closed_slug_links():
    from routes.brain_graph_routes import _extract_wikilinks

    text = """
    See [[valid-link]], [[section-link#details]], and [[alias-link|Label]].
    Ignore an unfinished [[cross-pro and a sentence [[not a real link]].
    Ignore `[[inline-code]]` and:
    ```
    value = [[array-notation]]
    ```
    """

    assert _extract_wikilinks(text) == [
        "valid-link",
        "section-link",
        "alias-link",
    ]


def test_visual_graph_preserves_full_counts_and_balances_categories():
    from routes.brain_graph_routes import _visual_graph

    nodes = [
        {"id": "agent:a", "type": "agent"},
        {"id": "reflection:r", "type": "reflection"},
    ]
    nodes.extend(
        {"id": f"python:{i:03}", "type": "python"} for i in range(100)
    )
    nodes.extend(
        {"id": f"security:{i:03}", "type": "security"} for i in range(20)
    )
    links = [
        {"source": "agent:a", "target": node["id"]}
        for node in nodes if node["type"] != "agent"
    ]
    graph = {
        "nodes": nodes,
        "links": links,
        "events": [],
        "stats": {
            "counts": {"agent": 1, "reflection": 1, "python": 100, "security": 20},
            "nodes": len(nodes),
            "links": len(links),
        },
    }

    visual = _visual_graph(graph, node_cap=22, link_cap=12)
    visual_ids = {node["id"] for node in visual["nodes"]}
    visual_types = {node["type"] for node in visual["nodes"]}

    assert len(visual["nodes"]) == 22
    assert {"agent:a", "reflection:r"} <= visual_ids
    assert {"python", "security"} <= visual_types
    assert visual["stats"]["nodes"] == len(nodes)
    assert visual["stats"]["visual_nodes"] == 22
    assert visual["stats"]["visual_sample"] is True
    assert all(
        link["source"] in visual_ids and link["target"] in visual_ids
        for link in visual["links"]
    )
