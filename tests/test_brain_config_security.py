"""Security invariants for shipped modular-brain configuration."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRAINS_ROOT = ROOT / "brains"
_CREDENTIAL_URL = re.compile(
    r"[a-z][a-z0-9+.-]*://[^/:\s\"']+:[^/@\s\"']+@",
    re.IGNORECASE,
)
_WILDCARD_HOST = re.compile(
    r"(?m)^\s*host:\s*[\"']?(?:0\.0\.0\.0|::)[\"']?\s*(?:#.*)?$",
)


def test_shipped_brain_configs_do_not_embed_url_credentials():
    configs = sorted(BRAINS_ROOT.glob("*/config/**/*.yaml"))
    assert configs, "expected at least one shipped brain configuration"

    unsafe = [
        str(path.relative_to(ROOT))
        for path in configs
        if _CREDENTIAL_URL.search(path.read_text(encoding="utf-8"))
    ]
    assert unsafe == []


def test_shipped_production_brain_profiles_default_to_non_wildcard_bind():
    profiles = sorted(BRAINS_ROOT.glob("*/config/profiles/production.yaml"))
    assert profiles, "expected at least one shipped production brain profile"

    unsafe = [
        str(path.relative_to(ROOT))
        for path in profiles
        if _WILDCARD_HOST.search(path.read_text(encoding="utf-8"))
    ]
    assert unsafe == []
