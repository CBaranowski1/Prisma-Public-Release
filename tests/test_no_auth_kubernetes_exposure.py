from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_no_login_kubernetes_base_does_not_allow_ingress_controller():
    policy = (ROOT / "deploy" / "kubernetes" / "networkpolicy.yaml").read_text(
        "utf-8"
    )
    assert "ingress-nginx" not in policy


def test_no_login_ingress_example_is_not_deployable():
    source = (ROOT / "deploy" / "kubernetes" / "ingress.example.yaml").read_text(
        "utf-8"
    )
    documents = [item for item in yaml.safe_load_all(source) if item]
    assert documents == []
