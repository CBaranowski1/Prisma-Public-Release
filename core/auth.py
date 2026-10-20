"""
Authentication module — multi-user password hashing, session tokens, config persistence.
Config stored in data/auth.json. Uses bcrypt directly.
"""

import hashlib
import json
import os
import secrets
import sys
import threading
import time
import logging
from typing import Optional, Dict, Any, List

import bcrypt
import pyotp

logger = logging.getLogger(__name__)


from core.atomic_io import atomic_write_json as _atomic_write_json  # noqa: E402
from core.constants import DATA_DIR

DEFAULT_PRIVILEGES = {
    "can_use_agent": True,
    "can_use_browser": True,
    "can_use_bash": False,
    "can_use_documents": True,
    "can_use_research": True,
    "can_generate_images": True,
    "can_manage_memory": True,
    "can_manage_brain": False,
    "can_manage_models": False,
    "can_manage_data_sources": False,
    "max_messages_per_day": 0,
    "allowed_models": [],
}

# Admins get everything
ADMIN_PRIVILEGES = {k: (True if isinstance(v, bool) else (0 if isinstance(v, int) else [])) for k, v in DEFAULT_PRIVILEGES.items()}

DEFAULT_AUTH_PATH = os.path.join(DATA_DIR, "auth.json")
TOKEN_TTL = 60 * 60 * 24 * 7  # 7 days

# SEC-19: shared 2FA lockout (TOTP + backup codes) — 5 consecutive failures
# locks verification for 15 minutes, then auto-unlocks (D-05).
_TOTP_MAX_FAILURES = 5
_TOTP_LOCKOUT_SECONDS = 15 * 60

# Usernames the auth + middleware layer reserve as internal "synthetic owner"
# sentinels; they must never belong to a real account. The most dangerous is
# "internal-tool": `core.middleware.require_admin` treats any request whose
# `current_user == "internal-tool"` as the in-process tool loopback and grants
# admin, and because the cookie auth path sets `current_user` to the raw
# username, an account literally named "internal-tool" would be silently
# treated as an admin by every `require_admin`-gated route. "api" collides with
# the bearer-token owner-attribution sentinel. "demo"/"system" round out the
# synthetic-owner set the rest of the codebase already special-cases (see
# `_SYNTHETIC_OWNERS` in routes/assistant_routes.py and the matching guards in
# src/task_scheduler.py / routes/research_routes.py) — a real account with one
# of those names would be denied an assistant and inconsistently owner-scoped.
# Refuse to create or rename into any of them so the sentinels can't be
# impersonated. (Keep this in sync with that synthetic-owner set.)
RESERVED_USERNAMES = frozenset({"internal-tool", "api", "demo", "system"})

VALID_ROLES = ("admin", "coder", "user")

# Privilege overrides granted purely by role (merged over DEFAULT_PRIVILEGES).
_CODER_PRIVILEGES = {"can_manage_brain": True, "can_manage_models": True, "can_manage_data_sources": True}


def _role_of(user: Dict[str, Any]) -> str:
    """Resolve a user record's role. is_admin always wins; else stored role; else 'user'."""
    if user.get("is_admin"):
        return "admin"
    role = user.get("role")
    return role if role in VALID_ROLES else "user"


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


def _hash_token(token: str) -> str:
    """Map a raw session token to its at-rest identifier.

    SECURITY: the in-memory session map and sessions.json are keyed by this
    SHA-256 digest, never by the raw bearer token. A read of sessions.json
    (stray backup, host access, file-disclosure bug) therefore yields no
    usable credential — an attacker cannot reverse the digest into a token
    that would pass validate_token(). Existing plaintext-keyed sessions from
    before this change simply fail to match and are treated as logged-out.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthManager:
    """Manages multi-user password + session-token auth system."""

    def __init__(self, auth_path: str = DEFAULT_AUTH_PATH):
        self.auth_path = auth_path
        self._sessions_path = os.path.join(os.path.dirname(auth_path), "sessions.json")
        self._config: Dict[str, Any] = {}
        self._sessions: Dict[str, Dict[str, Any]] = {}  # sha256(token) -> {username, expiry}
        # Guards mutations of self._sessions and the on-disk sessions.json.
        # Validate/create/revoke run concurrently from the FastAPI threadpool.
        self._sessions_lock = threading.RLock()
        # Guards the first-run setup check-and-write so concurrent requests
        # cannot both observe is_configured==False and both create admin accounts.
        self._setup_lock = threading.Lock()
        # Guards every read-modify-_save() critical section on self._config (users,
        # roles, privileges, TOTP/backup-code state, must_change_password, recovery
        # fields). RLock (not Lock) so a guarded method can safely call another
        # guarded method on the same thread without deadlocking.
        self._config_lock = threading.RLock()
        self._load()
        self._load_sessions()
        self._migrate_single_user()
        self._migrate_legacy_admin_role()
        self._migrate_roles()
        self._migrate_totp_plaintext_backup_codes()

    def _load(self):
        try:
            if os.path.exists(self.auth_path):
                with open(self.auth_path, "r", encoding="utf-8") as f:
                    self._config = json.load(f)
                # Normalize all stored usernames to lowercase so they match
                # the .strip().lower() applied at login/verify time. Fixes
                # "Invalid credentials" when auth.json was written with
                # mixed-case keys (e.g. via manual edit or a future migration).
                if "users" in self._config:
                    self._config["users"] = {
                        k.strip().lower(): v
                        for k, v in self._config["users"].items()
                    }
                logger.info("Auth config loaded")
            else:
                self._config = {}
                logger.info("No auth config found — first-run setup required")
        except Exception as e:
            logger.error(f"Failed to load auth config: {e}")
            self._config = {}

    def _load_sessions(self):
        """Load persisted session tokens from disk, pruning expired ones."""
        try:
            if os.path.exists(self._sessions_path):
                with open(self._sessions_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                now = time.time()
                self._sessions = {k: v for k, v in data.items() if v.get("expiry", 0) > now}
                pruned = len(data) - len(self._sessions)
                if pruned > 0:
                    self._save_sessions()
                logger.info(f"Loaded {len(self._sessions)} session(s) from disk")
        except Exception as e:
            logger.error(f"Failed to load sessions: {e}")
            self._sessions = {}

    def _save_sessions(self):
        """Persist session tokens to disk (atomic, lock-guarded)."""
        try:
            with self._sessions_lock:
                snapshot = dict(self._sessions)
            _atomic_write_json(self._sessions_path, snapshot)
        except Exception as e:
            logger.error(f"Failed to save sessions: {e}")

    def _migrate_single_user(self):
        """Migrate old single-user format to multi-user format."""
        if "password_hash" in self._config and "users" not in self._config:
            old_user = self._config.get("username", "admin")
            old_hash = self._config["password_hash"]
            self._config = {
                "users": {
                    old_user: {
                        "password_hash": old_hash,
                        "created": time.time(),
                        "is_admin": True,
                    }
                }
            }
            self._save()
            logger.info(f"Migrated single-user auth to multi-user (admin: {old_user})")

    def _migrate_legacy_admin_role(self):
        """Normalize setup.py's old role='admin' marker to is_admin=True."""
        changed = False
        for username, user in self.users.items():
            if user.get("role") == "admin" and "is_admin" not in user:
                user["is_admin"] = True
                changed = True
                logger.info(f"Migrated legacy admin role for '{username}'")
        if changed:
            self._save()

    def _migrate_roles(self):
        """Ensure every user record has an explicit 'role' field."""
        changed = 0
        for username, user in self.users.items():
            desired = _role_of(user)
            if user.get("role") != desired:
                user["role"] = desired
                changed += 1
        if changed:
            self._save()
            logger.info(f"Migrated role field for {changed} user(s)")

    def _migrate_totp_plaintext_backup_codes(self):
        """One-time invalidation of legacy plaintext `totp_backup_codes`
        (SEC-19 D-02 migration). A user enabled 2FA before this hardening
        landed if they have `totp_backup_codes` but no
        `totp_backup_code_hashes` yet. Never re-hash the old plaintext
        values -- silently accepting a code from a stale auth.json backup
        (or a copy anyone with host access has read) would defeat the
        point of hashing. Drop the field and require regeneration."""
        changed = False
        for username, user in self.users.items():
            if (user.get("totp_enabled")
                    and "totp_backup_codes" in user
                    and "totp_backup_code_hashes" not in user):
                user.pop("totp_backup_codes", None)
                changed = True
                logger.warning(
                    "Invalidated legacy plaintext 2FA backup codes for '%s' -- "
                    "regenerate via POST /2fa/backup-codes/regenerate",
                    username,
                )
        if changed:
            self._save()

    def _save(self):
        _atomic_write_json(self.auth_path, self._config, indent=2)

    @property
    def users(self) -> Dict[str, Any]:
        return self._config.get("users", {})

    @property
    def signup_enabled(self) -> bool:
        return self._config.get("signup_enabled", False)

    @signup_enabled.setter
    def signup_enabled(self, value: bool):
        self._config["signup_enabled"] = value
        self._save()

    @property
    def is_configured(self) -> bool:
        return len(self.users) > 0

    # ------------------------------------------------------------------
    # Account management
    # ------------------------------------------------------------------

    def setup(self, username: str, password: str) -> bool:
        """First-run admin setup. Only works if no users exist."""
        with self._setup_lock:
            if self.is_configured:
                return False
            return self.create_user(username, password, is_admin=True)

    def create_user(self, username: str, password: str, is_admin: bool = False, role: Optional[str] = None) -> bool:
        """Create a new user account. If `role` is given it is authoritative and
        `is_admin` is derived from it (role='admin' => is_admin=True); otherwise
        `is_admin` selects admin vs user."""
        username = username.strip().lower()
        if not username:
            return False
        if username in RESERVED_USERNAMES:
            logger.warning("Refused to create reserved username '%s'", username)
            return False
        with self._config_lock:
            if username in self.users:
                return False
            if role is not None and role not in VALID_ROLES:
                return False
            if role is None:
                role = "admin" if is_admin else "user"
            is_admin = (role == "admin")
            if "users" not in self._config:
                self._config["users"] = {}
            privileges = dict(ADMIN_PRIVILEGES if is_admin else DEFAULT_PRIVILEGES)
            if role == "coder":
                privileges.update(_CODER_PRIVILEGES)
            self._config["users"][username] = {
                "password_hash": _hash_password(password),
                "created": time.time(),
                "is_admin": is_admin,
                "role": role,
                "privileges": privileges,
            }
            self._save()
        logger.info(f"Created user '{username}' (role={role})")
        return True

    def delete_user(self, username: str, requesting_user: str) -> bool:
        """Delete a user. Only admins can delete, and can't delete themselves.

        SECURITY: also revoke every active session token belonging to this
        user so any open browser tab they have gets kicked back to /login
        on the next request. Without this the user kept full access until
        their cookie expired naturally (default ~30 days).
        """
        username = username.strip().lower()
        with self._config_lock:
            if username not in self.users:
                return False
            if username == requesting_user:
                return False
            if not self.users.get(requesting_user, {}).get("is_admin"):
                return False
            del self._config["users"][username]
            self._save()
        # Purge all sessions belonging to this user. validate_token doesn't
        # cross-check `self.users`, so without this step a deleted user's
        # cookie keeps authenticating.
        revoked = 0
        with self._sessions_lock:
            to_drop = [tok for tok, sess in self._sessions.items()
                       if (sess or {}).get("username") == username]
            for tok in to_drop:
                self._sessions.pop(tok, None)
                revoked += 1
        if revoked:
            self._save_sessions()
        # Also revoke API bearer tokens owned by this user. The bearer auth
        # path authenticates straight against ApiToken rows and never
        # re-checks that the owner still exists, so leaving the rows behind
        # would let a deleted user keep full API access indefinitely.
        try:
            from core.database import get_db_session, ApiToken
            with get_db_session() as db:
                removed = db.query(ApiToken).filter(ApiToken.owner == username).delete()
            if removed:
                logger.info(f"Revoked {removed} API token(s) owned by deleted user '{username}'")
        except Exception:
            logger.warning(f"Failed to revoke API tokens for deleted user '{username}'")
        logger.info(f"Deleted user '{username}' (by {requesting_user}); revoked {revoked} active session(s)")
        return True

    def rename_user(self, old_username: str, new_username: str, requesting_user: str) -> bool:
        """Rename a user in auth config and active sessions. Admin only."""
        old_username = old_username.strip().lower()
        new_username = new_username.strip().lower()
        requesting_user = (requesting_user or "").strip().lower()
        if not old_username or not new_username:
            return False
        if new_username in RESERVED_USERNAMES:
            logger.warning("Refused to rename '%s' into reserved username '%s'", old_username, new_username)
            return False
        with self._config_lock:
            if old_username not in self.users:
                return False
            if new_username in self.users:
                return False
            if not self.users.get(requesting_user, {}).get("is_admin"):
                return False
            self._config.setdefault("users", {})[new_username] = self._config["users"].pop(old_username)
            self._save()

        renamed_sessions = 0
        with self._sessions_lock:
            for sess in self._sessions.values():
                sess_user = str((sess or {}).get("username") or "").strip().lower()
                if sess_user == old_username:
                    sess["username"] = new_username
                    renamed_sessions += 1
        if renamed_sessions:
            self._save_sessions()
        logger.info(
            "Renamed user '%s' -> '%s' (by %s); updated %d active session(s)",
            old_username, new_username, requesting_user, renamed_sessions,
        )
        return True

    def is_admin(self, username: str) -> bool:
        return self.users.get(username, {}).get("is_admin", False)

    def list_users(self) -> List[Dict[str, Any]]:
        return [
            {"username": u, "is_admin": d.get("is_admin", False),
             "role": _role_of(d), "privileges": self.get_privileges(u)}
            for u, d in self.users.items()
        ]

    def get_privileges(self, username: str) -> Dict[str, Any]:
        """Get privileges for a user. Admins get all privileges."""
        user = self.users.get(username, {})
        if user.get("is_admin"):
            return dict(ADMIN_PRIVILEGES)
        base = dict(DEFAULT_PRIVILEGES)
        if _role_of(user) == "coder":
            base.update(_CODER_PRIVILEGES)
        stored = user.get("privileges", {})
        return {**base, **stored}

    def set_privileges(self, username: str, privileges: Dict[str, Any]) -> bool:
        """Update privileges for a user. Can't modify admin privileges."""
        username = username.strip().lower()
        with self._config_lock:
            if username not in self.users:
                return False
            if self.users[username].get("is_admin"):
                return False  # admins always have full access
            # Only allow known privilege keys
            current = self.get_privileges(username)
            for k, v in privileges.items():
                if k in DEFAULT_PRIVILEGES:
                    # Privileges are boolean flags; coerce so a truthy non-bool
                    # (e.g. a JSON string) can't slip through as a privilege value.
                    current[k] = bool(v)
            self._config["users"][username]["privileges"] = current
            self._save()
        logger.info(f"Updated privileges for '{username}': {current}")
        return True

    def set_role(self, username: str, role: str, requesting_user: str) -> bool:
        """Change a user's role (admin only). Cannot demote the last admin."""
        username = username.strip().lower()
        requesting_user = (requesting_user or "").strip().lower()
        if role not in VALID_ROLES:
            return False
        with self._config_lock:
            if username not in self.users:
                return False
            if not self.users.get(requesting_user, {}).get("is_admin"):
                return False
            # Guard the last admin from self/other demotion.
            if self.users[username].get("is_admin") and role != "admin":
                admins = [u for u, d in self.users.items() if d.get("is_admin")]
                if len(admins) <= 1:
                    return False
            user = self._config["users"][username]
            user["role"] = role
            user["is_admin"] = (role == "admin")
            # Reset privilege baseline to the new role's defaults.
            if role == "admin":
                user["privileges"] = dict(ADMIN_PRIVILEGES)
            else:
                privs = dict(DEFAULT_PRIVILEGES)
                if role == "coder":
                    privs.update(_CODER_PRIVILEGES)
                user["privileges"] = privs
            self._save()
        logger.info(f"Set role for '{username}' -> {role} (by {requesting_user})")
        return True

    def change_password(self, username: str, current_password: str, new_password: str) -> bool:
        username = username.strip().lower()
        with self._config_lock:
            if username not in self.users:
                return False
            if not _verify_password(current_password, self.users[username]["password_hash"]):
                return False
            self._config["users"][username]["password_hash"] = _hash_password(new_password)
            # SEC-20 D-09: a successful voluntary password change clears any
            # forced-rotation flag set by admin_reset_password/cli_reset_password
            # -- otherwise a temp password could silently become permanent.
            self._config["users"][username]["must_change_password"] = False
            self._save()
        return True

    # ------------------------------------------------------------------
    # SEC-20: account recovery (admin reset-other-user + offline CLI break-glass)
    # ------------------------------------------------------------------

    def admin_reset_password(self, username: str, new_password: str, requesting_user: str) -> bool:
        """Admin-only web path (D-06). Sets the target's password to
        `new_password` and flags `must_change_password` so a temp password
        can't silently become permanent. Revokes the target's active
        sessions AFTER the _config_lock block exits (Pitfall 3 -- never nest
        _sessions_lock inside _config_lock)."""
        username = username.strip().lower()
        requesting_user = (requesting_user or "").strip().lower()
        with self._config_lock:
            if username not in self.users:
                return False
            if not self.users.get(requesting_user, {}).get("is_admin"):
                return False
            self._config["users"][username]["password_hash"] = _hash_password(new_password)
            self._config["users"][username]["must_change_password"] = True
            self._save()
        self.revoke_user_sessions(username)
        logger.info(f"Admin '{requesting_user}' reset password for '{username}' (must_change_password=True)")
        return True

    def cli_reset_password(self, username: str, new_password: str) -> bool:
        """Offline host-CLI break-glass recovery (D-07). Deliberately has NO
        requesting_user/admin check -- the caller already has host
        filesystem access to auth.json, which IS the trust boundary
        (identical to reading auth.json directly). This method must NEVER be
        imported or called from routes/*.py, src/tool_implementations.py, or
        any other HTTP-reachable code path (Pitfall 7) -- only from this
        module's own `if __name__ == "__main__":` block, invoked by a human
        with a shell on the host machine."""
        username = username.strip().lower()
        with self._config_lock:
            if username not in self.users:
                return False
            self._config["users"][username]["password_hash"] = _hash_password(new_password)
            self._config["users"][username]["must_change_password"] = True
            self._save()
        self.revoke_user_sessions(username)
        logger.warning(f"CLI break-glass password reset for '{username}'")
        return True

    # ------------------------------------------------------------------
    # TOTP two-factor authentication
    # ------------------------------------------------------------------

    def totp_enabled(self, username: str) -> bool:
        """Check if 2FA is enabled for a user."""
        user = self.users.get(username.strip().lower(), {})
        return bool(user.get("totp_enabled"))

    def totp_generate_secret(self, username: str) -> Optional[str]:
        """Generate a new TOTP secret for a user. Returns the secret (not yet enabled)."""
        username = username.strip().lower()
        if username not in self.users:
            return None
        secret = pyotp.random_base32()
        self._config["users"][username]["totp_secret_pending"] = secret
        self._save()
        return secret

    def totp_get_provisioning_uri(self, username: str, secret: str) -> str:
        """Get the otpauth:// URI for QR code generation."""
        totp = pyotp.TOTP(secret)
        return totp.provisioning_uri(name=username, issuer_name="Prisma")

    def _generate_backup_codes(self, username: str) -> List[str]:
        """Generate 10 fresh backup codes, persist only their bcrypt hashes
        (`totp_backup_code_hashes`, D-02/D-03), and return the PLAINTEXT
        codes for one-time display -- they are never stored or retrievable
        again after this call returns. Caller MUST already hold
        `_config_lock` (or be inside a `_config_lock`-guarded method)."""
        codes = [secrets.token_hex(8) for _ in range(10)]  # 64-bit entropy each
        self._config["users"][username]["totp_backup_code_hashes"] = [
            _hash_password(c) for c in codes
        ]
        return codes

    def regenerate_backup_codes(self, username: str) -> List[str]:
        """Invalidate the current backup-code set and issue a fresh one
        (D-04). Callers (the route layer) must password-gate this first."""
        username = username.strip().lower()
        with self._config_lock:
            codes = self._generate_backup_codes(username)
            self._save()
        logger.info(f"Backup codes regenerated for '{username}'")
        return codes

    def totp_confirm_enable(self, username: str, code: str) -> Optional[List[str]]:
        """Verify a TOTP code against the pending secret, then enable 2FA.
        Returns the freshly generated PLAINTEXT backup codes (shown once)
        on success, or None on failure."""
        username = username.strip().lower()
        with self._config_lock:
            user = self.users.get(username, {})
            secret = user.get("totp_secret_pending")
            if not secret:
                return None
            totp = pyotp.TOTP(secret)
            if not totp.verify(code, valid_window=1):
                return None
            # Enable 2FA
            self._config["users"][username]["totp_secret"] = secret
            self._config["users"][username]["totp_enabled"] = True
            self._config["users"][username].pop("totp_secret_pending", None)
            codes = self._generate_backup_codes(username)
            self._save()
        logger.info(f"2FA enabled for '{username}'")
        return codes

    def totp_verify(self, username: str, code: str) -> bool:
        """Verify a TOTP or backup code for login. Gated behind a shared
        5-failure/15-minute auto-unlocking lockout covering both TOTP and
        backup-code attempts (SEC-19 D-05); backup codes are single-use
        bcrypt hashes (D-02)."""
        username = username.strip().lower()
        with self._config_lock:
            user = self.users.get(username, {})
            if not user.get("totp_enabled"):
                return True  # 2FA not enabled, always pass
            locked_until = user.get("totp_locked_until", 0)
            if time.time() < locked_until:
                return False  # locked out -- do not even attempt verification
            secret = user.get("totp_secret")
            if not secret:
                # 2FA is enabled but no secret is stored (corrupt/partially-
                # written auth.json). Fail closed -- returning True here
                # bypassed the second factor entirely.
                return False
            ok = False
            # Check backup codes first (existing order), now hash-based (D-02)
            hashes = list(user.get("totp_backup_code_hashes", []))
            for h in hashes:
                if _verify_password(code, h):
                    hashes.remove(h)
                    self._config["users"][username]["totp_backup_code_hashes"] = hashes
                    ok = True
                    logger.info(f"Backup code consumed for '{username}' ({len(hashes)} remaining)")
                    break
            if not ok:
                totp = pyotp.TOTP(secret)
                ok = totp.verify(code, valid_window=1)
            if ok:
                self._config["users"][username]["totp_failed_attempts"] = 0
                self._config["users"][username].pop("totp_locked_until", None)
            else:
                attempts = user.get("totp_failed_attempts", 0) + 1
                self._config["users"][username]["totp_failed_attempts"] = attempts
                if attempts >= _TOTP_MAX_FAILURES:
                    self._config["users"][username]["totp_locked_until"] = (
                        time.time() + _TOTP_LOCKOUT_SECONDS
                    )
            self._save()
            return ok

    def totp_disable(self, username: str, password: str) -> bool:
        """Disable 2FA for a user. Requires password confirmation."""
        username = username.strip().lower()
        if not self.verify_password(username, password):
            return False
        with self._config_lock:
            self._config["users"][username].pop("totp_secret", None)
            self._config["users"][username].pop("totp_secret_pending", None)
            self._config["users"][username].pop("totp_backup_codes", None)
            self._config["users"][username].pop("totp_backup_code_hashes", None)
            self._config["users"][username].pop("totp_failed_attempts", None)
            self._config["users"][username].pop("totp_locked_until", None)
            self._config["users"][username]["totp_enabled"] = False
            self._save()
        logger.info(f"2FA disabled for '{username}'")
        return True

    # ------------------------------------------------------------------
    # Login / logout / session tokens
    # ------------------------------------------------------------------

    def verify_password(self, username: str, password: str) -> bool:
        username = username.strip().lower()
        if username not in self.users:
            return False
        return _verify_password(password, self.users[username]["password_hash"])

    def create_session(self, username: str, password: str) -> Optional[str]:
        """Verify credentials and return a session token, or None."""
        username = username.strip().lower()
        if not self.verify_password(username, password):
            return None
        token = secrets.token_hex(32)
        with self._sessions_lock:
            self._sessions[_hash_token(token)] = {
                "username": username,
                "expiry": time.time() + TOKEN_TTL,
            }
        self._save_sessions()
        return token

    def ensure_dev_accounts(self, password: str = "password") -> int:
        """Idempotently create admin/coder/user demo accounts. Returns count created.
        DEV ONLY — gate the caller behind PRISMA_DEV_SWITCHER."""
        seeds = (("admin", "admin"), ("coder", "coder"), ("user", "user"))
        created = 0
        for username, role in seeds:
            if username not in self.users:
                if self.create_user(username, password, role=role):
                    created += 1
        return created

    def create_session_for(self, username: str) -> Optional[str]:
        """Issue a session token for a user WITHOUT a password check.
        DEV/admin impersonation only — never call from a password-auth path."""
        username = username.strip().lower()
        if username not in self.users:
            return None
        token = secrets.token_hex(32)
        with self._sessions_lock:
            self._sessions[_hash_token(token)] = {"username": username, "expiry": time.time() + TOKEN_TTL}
        self._save_sessions()
        return token

    def validate_token(self, token: Optional[str]) -> bool:
        if not token:
            return False
        expired = False
        deleted_user = False
        key = _hash_token(token)
        with self._sessions_lock:
            session = self._sessions.get(key)
            if session is None:
                return False
            if time.time() > session["expiry"]:
                self._sessions.pop(key, None)
                expired = True
            else:
                # SECURITY: if the user record has since been removed (admin
                # deleted them while their cookie was still valid), drop the
                # session so the next request kicks them out instead of
                # silently authenticating against a non-existent account.
                if session.get("username") not in self.users:
                    self._sessions.pop(key, None)
                    deleted_user = True
        if expired or deleted_user:
            self._save_sessions()
            return False
        return True

    def get_username_for_token(self, token: Optional[str]) -> Optional[str]:
        """Return the username associated with a valid token."""
        if not token:
            return None
        expired = False
        deleted_user = False
        key = _hash_token(token)
        with self._sessions_lock:
            session = self._sessions.get(key)
            if session is None:
                return None
            if time.time() > session["expiry"]:
                self._sessions.pop(key, None)
                expired = True
            else:
                _u = session["username"]
                # SECURITY: orphan check — same rationale as validate_token.
                if _u not in self.users:
                    self._sessions.pop(key, None)
                    deleted_user = True
                else:
                    return _u
        if expired or deleted_user:
            self._save_sessions()
        return None

    def revoke_token(self, token: str):
        with self._sessions_lock:
            self._sessions.pop(_hash_token(token), None)
        self._save_sessions()

    def revoke_user_sessions(self, username: str, except_token: Optional[str] = None) -> int:
        """Revoke active browser sessions for a user, optionally preserving one."""
        username = username.strip().lower()
        except_key = _hash_token(except_token) if except_token else None
        revoked = 0
        with self._sessions_lock:
            # Keys are sha256(token) digests; compare against the hashed
            # except_token rather than the raw value.
            to_drop = [
                key for key, session in self._sessions.items()
                if key != except_key and (session or {}).get("username") == username
            ]
            for key in to_drop:
                self._sessions.pop(key, None)
                revoked += 1
            if revoked:
                self._save_sessions()
        return revoked

    def status(self, token: Optional[str]) -> Dict[str, Any]:
        username = self.get_username_for_token(token)
        authenticated = username is not None
        result = {
            "configured": self.is_configured,
            "authenticated": authenticated,
            "username": username,
            "is_admin": self.is_admin(username) if username else False,
            "role": _role_of(self.users.get(username, {})) if username else None,
        }
        if authenticated:
            result["privileges"] = self.get_privileges(username)
        return result


# ----------------------------------------------------------------------------
# SEC-27c / D-07c: process-level shared AuthManager accessor.
#
# Production call sites that previously constructed `AuthManager()` fresh on
# every check (re-reading auth.json + re-running all migrations each time)
# should call `get_auth_manager()` instead, so tool gating and HTTP-route auth
# read the SAME in-memory config under the SAME `_config_lock`. Direct
# `AuthManager(path)` construction (as every existing test already does)
# remains fully supported and unaffected -- this is an ADDITIONAL accessor,
# not a replacement for the class.
#
# Test isolation (Pitfall 2): construction happens lazily, at CALL time, via
# the module-level `AuthManager` name -- so a monkeypatch of
# `core.auth.AuthManager` set up before the first call (while the cache is
# empty) is honored exactly like today's per-call behavior. Tests must call
# `reset_auth_manager_cache()` between cases that rely on a fresh/stubbed
# instance -- an autouse fixture in tests/conftest.py does this before AND
# after every test.
# ----------------------------------------------------------------------------
_auth_manager_singleton: Optional["AuthManager"] = None
_auth_manager_singleton_lock = threading.Lock()


def get_auth_manager(auth_path: str = DEFAULT_AUTH_PATH) -> "AuthManager":
    """Return the process-wide shared AuthManager, constructing it lazily on
    first call. Double-checked locking ensures concurrent first-callers still
    get exactly one instance."""
    global _auth_manager_singleton
    if _auth_manager_singleton is None:
        with _auth_manager_singleton_lock:
            if _auth_manager_singleton is None:
                _auth_manager_singleton = AuthManager(auth_path)
    return _auth_manager_singleton


def reset_auth_manager_cache() -> None:
    """Test-only hook. Clears the cached singleton so the next
    get_auth_manager() call re-resolves the (possibly monkeypatched)
    `AuthManager` name and re-reads auth.json from disk."""
    global _auth_manager_singleton
    _auth_manager_singleton = None


# ----------------------------------------------------------------------------
# SEC-20 D-07: offline host-CLI break-glass recovery entry point.
#
#     python -m core.auth reset-password <username> [--password <pw>]
#
# Runs AuthManager() directly against DEFAULT_AUTH_PATH (or PRISM_DATA_DIR, if
# set) with NO HTTP server involved and NO admin session required -- host
# filesystem access to auth.json IS the trust boundary. Never wire this into
# any route or agent-tool path (see cli_reset_password's docstring, Pitfall 7).
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m core.auth",
        description="Offline auth break-glass recovery (host-only, no HTTP surface).",
    )
    parser.add_argument("action", choices=["reset-password"])
    parser.add_argument("username")
    parser.add_argument("--password", default=None, help="omit to auto-generate a temporary password")
    args = parser.parse_args()

    mgr = AuthManager()
    pw = args.password or secrets.token_urlsafe(18)
    ok = mgr.cli_reset_password(args.username, pw)
    if ok:
        print(f"Password reset for '{args.username}'.")
        if not args.password:
            print(f"Temporary password: {pw}")
        print("must_change_password is now set -- the user must change this password on next login.")
    else:
        print(f"No such user: '{args.username}'", file=sys.stderr)
        raise SystemExit(1)
