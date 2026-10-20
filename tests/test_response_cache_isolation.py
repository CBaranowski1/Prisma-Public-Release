"""GRAY-BRN-11 (T-32-xowner): hermetic cross-owner cache isolation tests.

`_get_cache_key` used to hash only (url, model, messages, temp, max_tokens) —
no owner — so the process-wide `_response_cache` in src/llm_core.py could
serve owner A's cached completion to owner B on identical/templated input.
These tests prove the owner salt partitions the cache without a live LLM or
network call: `httpx.post` is monkeypatched to a fake, counting responder.
"""

import src.llm_core as lc


class _FakeResponse:
    """Minimal stand-in for an httpx.Response, OpenAI-compatible shape."""

    def __init__(self, content: str):
        self.is_success = True
        self.status_code = 200
        self.text = ""
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


_SAME_MESSAGES = [{"role": "user", "content": "same text"}]
_FAKE_URL = "http://fake-llm.test/v1/chat/completions"


def test_cache_key_distinguishes_owners():
    """Same (url, model, messages, temp, max_tokens); different owner -> different key.
    Same owner, called twice -> stable (identical) key."""
    k_alice = lc._get_cache_key(_FAKE_URL, "m", _SAME_MESSAGES, 1.0, 100, owner="alice")
    k_alice_again = lc._get_cache_key(_FAKE_URL, "m", _SAME_MESSAGES, 1.0, 100, owner="alice")
    k_bob = lc._get_cache_key(_FAKE_URL, "m", _SAME_MESSAGES, 1.0, 100, owner="bob")

    assert k_alice == k_alice_again
    assert k_alice != k_bob


def test_llm_call_no_cross_owner_hit(monkeypatch):
    """Two owners, identical messages: each gets their OWN upstream response,
    never the other owner's cached one, and two distinct cache entries exist."""
    lc._response_cache.clear()
    responses = ["A", "B"]
    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        idx = calls["n"]
        calls["n"] += 1
        return _FakeResponse(responses[idx])

    monkeypatch.setattr(lc.httpx, "post", fake_post)

    alice_result = lc.llm_call(_FAKE_URL, "m", _SAME_MESSAGES, owner="alice")
    bob_result = lc.llm_call(_FAKE_URL, "m", _SAME_MESSAGES, owner="bob")

    assert alice_result == "A"
    assert bob_result == "B"  # NOT "A" -- bob must never see alice's cached response
    assert calls["n"] == 2
    assert len(lc._response_cache) == 2  # two distinct owner-salted entries


def test_same_owner_is_cached(monkeypatch):
    """Same owner, identical messages, called twice: second call is a cache
    hit -- upstream (httpx.post) is invoked only once."""
    lc._response_cache.clear()
    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        return _FakeResponse("A")

    monkeypatch.setattr(lc.httpx, "post", fake_post)

    first = lc.llm_call(_FAKE_URL, "m", _SAME_MESSAGES, owner="alice")
    second = lc.llm_call(_FAKE_URL, "m", _SAME_MESSAGES, owner="alice")

    assert first == "A"
    assert second == "A"
    assert calls["n"] == 1  # only one upstream call -- second was a cache hit


def test_backward_compat_no_owner_bucket(monkeypatch):
    """Owner-less callers (owner=None, the default) keep today's behavior:
    a single shared bucket, so identical repeat calls still hit the cache."""
    lc._response_cache.clear()
    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        return _FakeResponse("A")

    monkeypatch.setattr(lc.httpx, "post", fake_post)

    first = lc.llm_call(_FAKE_URL, "m", _SAME_MESSAGES)
    second = lc.llm_call(_FAKE_URL, "m", _SAME_MESSAGES)

    assert first == "A"
    assert second == "A"
    assert calls["n"] == 1  # no-owner bucket still collapses identical repeats
