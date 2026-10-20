"""Pins google_pse_search()'s `cx` (Programmable Search Engine ID) precedence
to the uniform env -> settings -> default order (WR-02 review-fix).

Before the fix, `cx` resolved settings-before-env -- the opposite of the API
key resolved two lines above it (which already goes through
resolve_config_value / GRAY-INT-07's env-wins order). This pins that both
values now follow the same precedence, and that env truly wins when both a
`GOOGLE_PSE_CX` env var and a `google_pse_cx` settings value are present.

No real network call: httpx.get is monkeypatched to capture the outgoing
`cx` param and return an empty result set.
"""
from services.search import providers


class _FakeResponse:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"items": []}


def _capture_get(monkeypatch):
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured.update(params or {})
        return _FakeResponse()

    monkeypatch.setattr(providers.httpx, "get", fake_get)
    return captured


def test_google_pse_cx_env_wins_over_settings(monkeypatch):
    settings = {
        "search_provider": "google_pse",
        "google_pse_cx": "settings-cx",
    }
    monkeypatch.setattr(providers, "_get_search_settings", lambda: settings)
    monkeypatch.setenv("GOOGLE_API_KEY", "env-api-key")
    monkeypatch.setenv("GOOGLE_PSE_CX", "env-cx")
    captured = _capture_get(monkeypatch)

    providers.google_pse_search("test query")

    assert captured["cx"] == "env-cx"


def test_google_pse_cx_falls_back_to_settings_when_env_unset(monkeypatch):
    settings = {
        "search_provider": "google_pse",
        "google_pse_cx": "settings-cx",
    }
    monkeypatch.setattr(providers, "_get_search_settings", lambda: settings)
    monkeypatch.setenv("GOOGLE_API_KEY", "env-api-key")
    monkeypatch.delenv("GOOGLE_PSE_CX", raising=False)
    captured = _capture_get(monkeypatch)

    providers.google_pse_search("test query")

    assert captured["cx"] == "settings-cx"


def test_google_pse_search_returns_empty_when_cx_missing(monkeypatch):
    settings = {"search_provider": "google_pse"}
    monkeypatch.setattr(providers, "_get_search_settings", lambda: settings)
    monkeypatch.setenv("GOOGLE_API_KEY", "env-api-key")
    monkeypatch.delenv("GOOGLE_PSE_CX", raising=False)

    result = providers.google_pse_search("test query")

    assert result == []
