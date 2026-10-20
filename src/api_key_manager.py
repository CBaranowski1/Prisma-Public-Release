import os
import json
import logging
import threading
from typing import Dict
from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from core.atomic_io import atomic_write_json
from core.platform_compat import safe_chmod

logger = logging.getLogger(__name__)

class APIKeyManager:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.api_keys_file = os.path.join(data_dir, "api_keys.json")
        self.key_file = os.path.join(data_dir, ".key")
        # RLock (not Lock): a future guarded mutator (e.g. rotate_key) may
        # call another guarded method on the same thread, which would
        # deadlock under a plain Lock. Mirrors core/auth.py:106.
        self._lock = threading.RLock()
        # SEC-30/D-04: surfaced decrypt-failure side-channel. Reset at the
        # start of every load() call; holds the provider names that failed
        # to decrypt on the most recent load() (e.g. after a key rotation
        # or on-disk corruption). Initialized here so the attribute exists
        # even before the first load() is called.
        self.last_decrypt_failures = []

    def get_or_create_key(self) -> bytes:
        """Get or create encryption key for API keys.

        Mirrors src/secret_storage.py:37-59 (SEC-30/D-01): the key is created
        atomically at mode 0o600 (os.open O_EXCL so it is never briefly
        world-readable), and an already-existing legacy key is tightened to
        0o600 in place on every load — never regenerated (that would orphan
        existing ciphertext).

        SEC-30/D-02: if PRISM_MASTER_KEY is set, it is validated via Fernet's
        own constructor and used in place of the on-disk key — disk is never
        read or written in that case, so a read of data/ alone yields
        ciphertext but not the key. An invalid env key FAILS CLOSED (raises)
        rather than silently generating a new key and orphaning existing
        ciphertext. Unset falls through to the on-disk logic unchanged.
        """
        env_key = os.environ.get("PRISM_MASTER_KEY")
        if env_key:
            key_bytes = env_key.encode() if isinstance(env_key, str) else env_key
            try:
                Fernet(key_bytes)  # validate only; raises ValueError/TypeError if malformed
            except (ValueError, TypeError) as e:
                raise RuntimeError(
                    "PRISM_MASTER_KEY is set but is not a valid Fernet key; "
                    "refusing to start rather than silently generating a new key "
                    "and orphaning existing ciphertext"
                ) from e
            return key_bytes
        if os.path.exists(self.key_file):
            # Idempotent tighten-in-place: a key created by the OLD (pre-
            # hardening) code may still be world/group-readable. Re-assert
            # 0o600 on every load — cheap, and closes the gap for
            # already-deployed keys. No-op on Windows.
            safe_chmod(self.key_file, 0o600)
            with open(self.key_file, 'rb') as f:
                return f.read()
        key = Fernet.generate_key()
        try:
            fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # Lost a creation race to another thread/process — read what it wrote.
            with open(self.key_file, 'rb') as f:
                return f.read()
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        # Windows ignores the os.open mode bits; safe_chmod re-asserts 0o600
        # on POSIX and no-ops elsewhere.
        safe_chmod(self.key_file, 0o600)
        return key
    
    def encrypt_api_key(self, api_key: str) -> str:
        """Encrypt an API key"""
        if not api_key:
            return ""
        f = Fernet(self.get_or_create_key())
        return f.encrypt(api_key.encode()).decode()

    def _get_fernet_for_decrypt(self):
        """Build the Fernet/MultiFernet used for DECRYPT only (SEC-30/D-03,
        RESEARCH Pattern 4).

        A rotation can crash between re-encrypting api_keys.json under a new
        key (rotate_key step 3) and promoting that key to canonical
        (rotate_key step 4). In that window the canonical .key file still
        holds the OLD key, but on-disk ciphertext is already encrypted under
        the NEW key -- which exists only in the `.key.new` sidecar. Trying
        the sidecar key FIRST (then falling back to canonical) via
        MultiFernet means decrypt succeeds regardless of which side of the
        crash window a given token was written on. encrypt/save always use
        get_or_create_key() (the canonical key) directly and are unaffected.
        """
        canonical = Fernet(self.get_or_create_key())
        sidecar = self.key_file + ".new"
        if os.path.exists(sidecar):
            with open(sidecar, "rb") as f:
                new_key = f.read()
            return MultiFernet([Fernet(new_key), canonical])
        return canonical

    def decrypt_api_key(self, encrypted_key: str) -> str:
        """Decrypt an API key"""
        if not encrypted_key:
            return ""
        f = self._get_fernet_for_decrypt()
        return f.decrypt(encrypted_key.encode()).decode()
    
    def _load_raw(self) -> Dict[str, str]:
        """Load the raw, still-encrypted keys dict from disk.

        Tolerates a missing/corrupt/wrong-shaped file by returning {} — the
        same robustness load() relies on at startup.
        """
        if not os.path.exists(self.api_keys_file):
            return {}
        try:
            with open(self.api_keys_file, 'r', encoding="utf-8") as f:
                encrypted_keys = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            # A corrupt/truncated api_keys.json must not crash load() (called on
            # startup via app_initializer) — treat it as no stored keys.
            logger.warning("Failed to read API keys file: %s", e)
            return {}
        if not isinstance(encrypted_keys, dict):
            # Legacy/wrong shape (e.g. a list) — .items() would raise. Ignore it.
            logger.warning("API keys file has unexpected shape (%s); ignoring", type(encrypted_keys).__name__)
            return {}
        return encrypted_keys

    def save(self, provider: str, api_key: str):
        """Save encrypted API key to file.

        Operates on the raw (still-encrypted) on-disk dict so other providers'
        keys stay encrypted. Loading via load() first would decrypt them and
        write them back as plaintext, which then fails to decrypt on the next
        load() and silently drops those providers.

        The full read-modify-write is guarded by self._lock (SEC-31/D-06) so
        concurrent save() calls can't interleave and lose keys, and persisted
        via atomic_write_json (SEC-31/D-05, core/atomic_io.py: temp+fsync+
        os.replace) so a crash mid-write leaves the prior file intact instead
        of truncating it. Mirrors src/integrations.py:212-216 + core/auth.py's
        _config_lock idiom.
        """
        with self._lock:
            keys = self._load_raw()
            keys[provider] = self.encrypt_api_key(api_key)
            atomic_write_json(self.api_keys_file, keys)
            safe_chmod(self.api_keys_file, 0o600)

    def load(self) -> Dict[str, str]:
        """Load and decrypt API keys.

        SEC-30/D-04: a per-provider decrypt failure (rotated/corrupt key) is
        SURFACED rather than silently dropped at warning level: it is logged
        at ERROR and the provider name is retained on
        self.last_decrypt_failures (reset at the start of every call) so an
        operator/rotate_key() can tell a partial result is due to a bad key
        instead of it looking like a silently-complete view. The RETURN
        VALUE and no-raise resilience are unchanged (Pitfall 4): the failed
        provider is still just skipped, never added to the returned dict.
        """
        encrypted_keys = self._load_raw()
        decrypted = {}
        self.last_decrypt_failures = []
        for provider, key in encrypted_keys.items():
            try:
                decrypted[provider] = self.decrypt_api_key(key)
            except (InvalidToken, ValueError) as e:
                logger.error("Failed to decrypt API key for %s: %s", provider, e)
                self.last_decrypt_failures.append(provider)
        return decrypted

    def _complete_interrupted_rotation(self, sidecar: str) -> None:
        """Resolve a `.key.new` sidecar left behind by a rotation that
        crashed before this method was reached (SEC-30/D-03, RESEARCH
        Pattern 3 step 0).

        Determine which side of the crash window the interruption happened
        on by testing which key the ON-DISK ciphertext actually decrypts
        under:
          - if the CURRENT canonical key still decrypts everything, the
            crash happened before the re-encrypted json was written (step
            3) -- the sidecar's new key was never used; discard the stale
            sidecar.
          - if the sidecar's new key decrypts everything instead, the crash
            happened after step 3 but before promotion (step 4) -- finish
            the promotion.
          - if NEITHER key decrypts everything, refuse to guess and raise so
            an operator can intervene (should not happen in normal use).
        """
        with open(sidecar, "rb") as f:
            new_key = f.read()
        raw = self._load_raw()

        def _all_decrypt(key: bytes) -> bool:
            fernet = Fernet(key)
            for token in raw.values():
                try:
                    fernet.decrypt(token.encode())
                except (InvalidToken, ValueError):
                    return False
            return True

        canonical_key = self.get_or_create_key()
        if _all_decrypt(canonical_key):
            os.remove(sidecar)
            logger.info("rotate_key: discarded a stale interrupted-rotation sidecar (pre-json-write crash)")
            return
        if _all_decrypt(new_key):
            os.replace(sidecar, self.key_file)
            safe_chmod(self.key_file, 0o600)
            logger.info("rotate_key: completed an interrupted rotation (post-json-write crash)")
            return
        raise RuntimeError(
            "rotate_key() found an interrupted rotation sidecar (.key.new) but "
            "neither the current key nor the sidecar key decrypts all stored "
            "entries; manual intervention is required before rotating again."
        )

    def rotate_key(self) -> None:
        """Crash-safe key rotation (SEC-30/D-03).

        Under self._lock: (0) self-heal any pre-existing `.key.new` sidecar
        from a prior interrupted rotation; (1) decrypt ALL providers with
        the OLD key, aborting (raising, writing nothing) if ANY fail --
        rotating away the only key that could ever read a stuck entry would
        make it permanently unrecoverable (RESEARCH Pitfall 1); (2) generate
        a new key and persist it to the `.key.new` sidecar BEFORE using it
        for anything; (3) re-encrypt everything under the new key and
        atomically write api_keys.json; (4) os.replace the sidecar onto the
        canonical `.key` to promote.

        Both naive orderings (new key first, or json first with no sidecar)
        have a data-loss crash window (RESEARCH Pitfall 2) -- the sidecar
        makes the new key durable before it's used, and the read path
        (_get_fernet_for_decrypt) checks the sidecar too, so a crash between
        steps 3 and 4 still decrypts everything via a fresh manager.

        Refuses (raises) when PRISM_MASTER_KEY is set: there is no on-disk
        key to rotate, and writing one alongside an active env override
        would create a confusing, never-read dual-key state (Pitfall 5).
        """
        if os.environ.get("PRISM_MASTER_KEY"):
            raise RuntimeError(
                "rotate_key() is not supported while PRISM_MASTER_KEY is set -- "
                "there is no on-disk key to rotate. Rotate the env-managed key "
                "externally and re-encrypt stored values manually."
            )
        with self._lock:
            sidecar = self.key_file + ".new"
            if os.path.exists(sidecar):
                self._complete_interrupted_rotation(sidecar)

            old_key = self.get_or_create_key()
            raw = self._load_raw()

            # 1. Decrypt everything with the OLD key first. Abort (no
            #    writes) if anything fails -- see Pitfall 1.
            old_fernet = Fernet(old_key)
            self.last_decrypt_failures = []
            plaintext = {}
            for provider, token in raw.items():
                try:
                    plaintext[provider] = old_fernet.decrypt(token.encode()).decode()
                except (InvalidToken, ValueError) as e:
                    logger.error("rotate_key: provider %s failed to decrypt with current key: %s", provider, e)
                    self.last_decrypt_failures.append(provider)
            if self.last_decrypt_failures:
                raise RuntimeError(
                    "rotate_key() aborted: the following providers failed to decrypt "
                    "with the current key and would be permanently lost if rotation "
                    "proceeded: " + ", ".join(self.last_decrypt_failures) +
                    ". Fix or remove these entries before retrying rotation."
                )

            # 2. Generate the new key and persist it to the sidecar BEFORE
            #    using it for anything.
            new_key = Fernet.generate_key()
            fd = os.open(sidecar, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, new_key)
            finally:
                os.close(fd)
            safe_chmod(sidecar, 0o600)

            # 3. Re-encrypt everything under the new key and persist
            #    atomically. If the process dies here, .key (canonical)
            #    still holds the OLD key, but the sidecar holds the NEW key
            #    -- the read path also tries the sidecar, so nothing is lost.
            new_fernet = Fernet(new_key)
            new_raw = {p: new_fernet.encrypt(v.encode()).decode() for p, v in plaintext.items()}
            atomic_write_json(self.api_keys_file, new_raw)
            safe_chmod(self.api_keys_file, 0o600)

            # 4. Promote: atomic replace makes the new key canonical. Step 3
            #    already made the crash window safe; this is only cleanup.
            os.replace(sidecar, self.key_file)
            safe_chmod(self.key_file, 0o600)
            logger.info("rotate_key: rotation complete for %d provider(s)", len(new_raw))

