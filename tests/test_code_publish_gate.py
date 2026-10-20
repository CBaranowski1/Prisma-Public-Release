import json

from src.code_publish_gate import evaluate_model_ide_proposal


def _proposal(**updates):
    value = {
        "summary": "Safe update",
        "replacement": None,
        "migration_sql": None,
        "migration_name": None,
    }
    value.update(updates)
    return json.dumps(value)


def test_valid_python_replacement_is_verified():
    proposal, report = evaluate_model_ide_proposal(
        _proposal(replacement="def add(a, b):\n    return a + b\n"),
        original="def add(a, b):\n    return None\n",
        language="python",
        workspace_path="src/math.py",
        request_text="Implement add correctly.",
    )
    assert report["passed"] is True
    assert report["verified"] is True
    assert proposal["replacement"].endswith("return a + b\n")


def test_invalid_python_is_withheld():
    proposal, report = evaluate_model_ide_proposal(
        _proposal(replacement="def broken(:\n    return 1\n"),
        language="python",
        workspace_path="broken.py",
    )
    assert report["passed"] is False
    assert proposal["replacement"] is None
    assert any("validation failed" in item for item in report["errors"])


def test_new_placeholder_is_blocked_unless_scaffold_requested():
    raw = _proposal(replacement="def later():\n    pass\n")
    blocked, report = evaluate_model_ide_proposal(
        raw,
        original="",
        language="python",
        request_text="Implement this function.",
    )
    assert report["passed"] is False
    assert blocked["replacement"] is None

    allowed, allowed_report = evaluate_model_ide_proposal(
        raw,
        original="",
        language="python",
        request_text="Create a scaffold stub for this function.",
    )
    assert allowed_report["passed"] is True
    assert allowed["replacement"] is not None


def test_secret_bearing_replacement_is_withheld():
    proposal, report = evaluate_model_ide_proposal(
        _proposal(
            replacement='API_KEY = "sk-FAKE1234567890ABCDEFGH1234"\n'
        ),
        language="python",
    )
    assert report["passed"] is False
    assert proposal["replacement"] is None
    assert any("secret-shaped" in item for item in report["errors"])


def test_unsafe_migration_filename_is_withheld():
    proposal, report = evaluate_model_ide_proposal(
        _proposal(
            migration_sql="CREATE TABLE safe_table (id INTEGER);",
            migration_name="../../escape.sql",
        ),
        language="python",
    )
    assert report["passed"] is False
    assert proposal["migration_sql"] is None
    assert any("safe .sql basename" in item for item in report["errors"])


def test_empty_migration_sql_cannot_carry_a_filename():
    proposal, report = evaluate_model_ide_proposal(
        _proposal(
            migration_sql="",
            migration_name="unnecessary.sql",
        ),
        language="python",
    )
    assert report["passed"] is False
    assert proposal["migration_sql"] is None
    assert proposal["migration_name"] is None
    assert any(
        "provided without migration_sql" in item
        for item in report["errors"]
    )


def test_invalid_response_json_is_blocked():
    proposal, report = evaluate_model_ide_proposal("not json", language="python")
    assert report["passed"] is False
    assert proposal["replacement"] is None


def test_measurable_learned_critique_blocks_regression():
    proposal, report = evaluate_model_ide_proposal(
        _proposal(
            replacement='cursor.execute(f"SELECT * FROM users WHERE id={user_id}")\n'
        ),
        language="python",
        learned_rules=["Database queries must use parameterized inputs."],
    )
    assert report["passed"] is False
    assert proposal["replacement"] is None
    assert report["critique_stick_rate"] == 0.0
    assert report["critique_rules"][0]["status"] == "failed"


def test_measurable_learned_critique_reports_stick_rate():
    _proposal_value, report = evaluate_model_ide_proposal(
        _proposal(
            replacement='cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))\n'
        ),
        language="python",
        learned_rules=["Database queries must use parameterized inputs."],
    )
    assert report["passed"] is True
    assert report["critique_stick_rate"] == 1.0
