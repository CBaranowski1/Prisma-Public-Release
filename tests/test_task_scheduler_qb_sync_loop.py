"""Regression coverage for removal of Query Builder background sync."""

import inspect

from src.task_scheduler import TaskScheduler


def test_query_builder_sync_loop_is_not_registered():
    assert not hasattr(TaskScheduler, "_qb_sync_loop")
    start_source = inspect.getsource(TaskScheduler.start)
    stop_source = inspect.getsource(TaskScheduler.stop)
    assert "_qb_sync" not in start_source
    assert "_qb_sync" not in stop_source
