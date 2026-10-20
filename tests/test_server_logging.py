import logging
import os


def test_configure_file_logging_writes_to_logs_dir(tmp_path):
    import app
    log_path = app.configure_file_logging(str(tmp_path))
    assert log_path == os.path.join(str(tmp_path), "logs", "server.log")
    logging.getLogger("prism.test").warning("hello-file-log")
    for h in logging.getLogger().handlers:
        h.flush()
    assert os.path.exists(log_path)
    assert "hello-file-log" in open(log_path, encoding="utf-8").read()


def test_configure_file_logging_is_idempotent(tmp_path):
    import app
    app.configure_file_logging(str(tmp_path))
    before = len(logging.getLogger().handlers)
    app.configure_file_logging(str(tmp_path))
    after = len(logging.getLogger().handlers)
    assert after == before, "must not add a second file handler"
