from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
K8S = ROOT / "deploy" / "kubernetes"


def _load(name):
    return yaml.safe_load((K8S / name).read_text(encoding="utf-8"))


def test_prisma_pod_uses_restricted_non_root_security_context():
    deployment = _load("deployment.yaml")
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    security = container["securityContext"]

    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert security["allowPrivilegeEscalation"] is False
    assert security["readOnlyRootFilesystem"] is True
    assert security["capabilities"]["drop"] == ["ALL"]


def test_prisma_service_is_not_public_and_health_probes_are_defined():
    service = _load("service.yaml")
    deployment = _load("deployment.yaml")
    container = deployment["spec"]["template"]["spec"]["containers"][0]

    assert service["spec"]["type"] == "ClusterIP"
    assert container["livenessProbe"]["httpGet"]["path"] == "/api/health"
    assert container["readinessProbe"]["httpGet"]["path"] == "/api/ready"
    assert container["startupProbe"]["httpGet"]["path"] == "/api/ready"


def test_kustomization_excludes_example_secret():
    kustomization = _load("kustomization.yaml")

    assert "secret.example.yaml" not in kustomization["resources"]
    assert "networkpolicy.yaml" in kustomization["resources"]


def test_namespace_enforces_restricted_pod_security():
    namespace = _load("namespace.yaml")
    labels = namespace["metadata"]["labels"]

    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"
