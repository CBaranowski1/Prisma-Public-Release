"""SEC-31 (D-06): concurrent save() calls must not interleave and lose keys.

Models tests/test_upload_handler_atomicity.py's ThreadPoolExecutor
concurrent-writer pattern: N=20 distinct providers saved concurrently via
save(); after all complete, load() must return all 20 with correct values
(no lost update from an unlocked read-modify-write).
"""
import concurrent.futures

from src.api_key_manager import APIKeyManager

N_WRITERS = 20


def test_concurrent_saves_retain_all_keys(tmp_path):
    mgr = APIKeyManager(str(tmp_path))

    def write(idx: int) -> None:
        mgr.save(f"provider_{idx}", f"secret_{idx}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=N_WRITERS) as pool:
        list(pool.map(write, range(N_WRITERS)))

    loaded = mgr.load()
    missing = [
        f"provider_{i}"
        for i in range(N_WRITERS)
        if loaded.get(f"provider_{i}") != f"secret_{i}"
    ]
    assert not missing, f"Concurrent save() lost {len(missing)}/{N_WRITERS} keys: {missing}"
