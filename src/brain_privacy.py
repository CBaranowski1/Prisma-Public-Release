"""Fail-closed PII/secret gate for the shared-brain auto-learning write path.

Deliberately dependency-light (stdlib ``re`` only) so it can be imported and
unit-tested without dragging in the SQLAlchemy/httpx/settings import chain,
matching the ``src/secret_scrub.py`` / ``src/settings_scrub.py`` convention.
Called at the ONE sink ``src/brain_learn.py::add_learning()`` before any
content is written to the SHARED, multi-user-readable brain store.

Scope, stated honestly: this is a NARROW gate for secret-shaped tokens
(API-key-looking prefixes, PEM private key headers, JWT-shaped strings) plus
a small set of structurally-regular PII (email addresses, SSN-shaped digit
runs, Luhn-valid credit-card-shaped digit runs). It is explicitly NOT full
NLP PII detection — it does not catch free-text names, addresses, or any PII
that isn't shaped like one of the patterns below. See
``.planning/phases/31-recall-policy-privacy-degradation/31-RESEARCH.md``
("Don't Hand-Roll") for why broader NLP PII detection is out of scope.

Fail-closed contract: ``contains_pii_or_secret`` returns ``(True, reason)``
on ANY match. The caller (``add_learning``) treats ``True`` as WITHHOLD —
uncertainty never becomes a shared-brain write. This module never decides
to redact-and-continue; it only reports found-or-not, mirroring the
``(ok, reason)`` 2-tuple shape of ``src/brain_filter.py::acceptable``.
"""
import re

# Secret-shaped tokens: common API-key/credential prefixes and shapes.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),                     # OpenAI-style key
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),                     # GitHub PAT
    re.compile(r"AKIA[0-9A-Z]{16}"),                          # AWS access key id
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),        # PEM private key header
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT-shaped
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CC_RE = re.compile(r"\b\d{13,19}\b")  # candidate digit runs; gated by _luhn_ok below


def _luhn_ok(digits: str) -> bool:
    """Standard Luhn checksum over a digit-only string. Used to reduce false
    positives on long non-credit-card digit runs (order IDs, tracking
    numbers, phone numbers, etc.) before flagging a CC-shaped match."""
    if not digits or not digits.isdigit():
        return False
    total = 0
    reverse_digits = digits[::-1]
    for i, ch in enumerate(reverse_digits):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def contains_pii_or_secret(text: str) -> tuple[bool, str]:
    """Fail-closed scan of ``text`` for secret-shaped tokens or
    structurally-regular PII. Returns ``(True, reason)`` on the first match
    (reason is a short, non-user-facing class label used only for the
    withhold log line), or ``(False, "")`` when nothing matched — including
    empty/None input.

    Scan order: secret-shaped tokens, then email, then SSN-shaped digits,
    then Luhn-valid credit-card-shaped digit runs.
    """
    if not text:
        return False, ""

    for pat in _SECRET_PATTERNS:
        if pat.search(text):
            return True, "secret-shaped token"

    if _EMAIL_RE.search(text):
        return True, "email address"

    if _SSN_RE.search(text):
        return True, "ssn-shaped digits"

    for m in _CC_RE.finditer(text):
        if _luhn_ok(m.group(0)):
            return True, "credit-card-shaped digits (luhn-valid)"

    return False, ""


def redact_pii_and_secrets(text: str) -> tuple[str, list[str]]:
    """Return an index-safe copy of ``text`` and the redaction classes used.

    Conversation capture has a different requirement from shared lesson
    distillation: an eligible exchange must leave an index record even when the
    original contains a credential or structured PII. This helper replaces the
    narrow, deterministic shapes recognized by this module rather than dropping
    the entire exchange.
    """
    value = text or ""
    classes: list[str] = []

    for pattern in _SECRET_PATTERNS:
        value, count = pattern.subn("[REDACTED_SECRET]", value)
        if count:
            classes.append("secret")

    value, count = _EMAIL_RE.subn("[REDACTED_EMAIL]", value)
    if count:
        classes.append("email")

    value, count = _SSN_RE.subn("[REDACTED_SSN]", value)
    if count:
        classes.append("ssn")

    def _redact_luhn(match: re.Match) -> str:
        if _luhn_ok(match.group(0)):
            classes.append("credit_card")
            return "[REDACTED_CREDIT_CARD]"
        return match.group(0)

    value = _CC_RE.sub(_redact_luhn, value)
    return value, sorted(set(classes))
