from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_image_defaults_to_non_root_without_writable_source_tree():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text(encoding="utf-8")

    assert "USER ${APP_UID}:${APP_GID}" in dockerfile
    assert "ENV PRISMA_SKIP_ENV_FILE=1" in dockerfile
    assert '"pip>=26.1.2"' in dockerfile
    assert '"setuptools>=83.0.0"' in dockerfile
    assert dockerfile.index('"pip>=26.1.2"') < dockerfile.index("-r requirements.txt")
    assert "for dir in /app /app/data" not in entrypoint
    assert "/app/services/cache" not in entrypoint
    assert 'if [ "$CURRENT_UID" = "0" ]; then' in entrypoint


def test_compose_applies_container_security_defaults():
    compose = yaml.safe_load(
        (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    )
    service = compose["services"]["prisma"]

    assert service["user"] == "${PUID:-1000}:${PGID:-1000}"
    assert service["init"] is True
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["cap_drop"] == ["ALL"]
    assert service["build"]["args"] == {
        "APP_UID": "${PUID:-1000}",
        "APP_GID": "${PGID:-1000}",
    }
