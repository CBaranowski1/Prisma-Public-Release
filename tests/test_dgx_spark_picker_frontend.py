from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_picker_prefers_deployment_model_after_discovery():
    picker = (ROOT / "static" / "js" / "modelPicker.js").read_text(
        encoding="utf-8"
    )
    app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "item.deployment_default" in picker
    assert "models.includes(first.current_model)" in picker
    assert "sessionModule.updateModelPicker()" in app


def test_virtual_spark_selection_is_resolved_server_side():
    sessions = (ROOT / "routes" / "session_routes.py").read_text(
        encoding="utf-8"
    )

    assert "_resolve_dgx_spark_selection(endpoint_id, model)" in sessions
    assert "deployment.api_key" in sessions
    assert "Selected model is not the active DGX Spark model" in sessions
