from datetime import datetime, timedelta, timezone

from pakt.config import Config, set_last_sync
from pakt.scheduler import SyncScheduler


def scheduler(hours: int = 24) -> SyncScheduler:
    """A scheduler set to the given interval, not started."""
    config = Config()
    config.scheduler.enabled = True
    config.scheduler.interval_hours = hours
    return SyncScheduler(config, sync_func=lambda: None, is_running_func=lambda: False)


def test_first_run_is_soon_without_a_past_sync():
    """With no sync on record, the first run is about a minute away."""
    first = scheduler()._first_run()
    assert timedelta(seconds=50) < first - datetime.now(timezone.utc) < timedelta(seconds=70)


def test_first_run_counts_from_the_last_sync():
    """A recent sync puts the first run one interval after it."""
    last = datetime.now(timezone.utc) - timedelta(hours=2)
    set_last_sync(last)
    assert scheduler(24)._first_run() == last + timedelta(hours=24)


def test_overdue_sync_runs_soon():
    """A sync older than the interval makes the first run about a minute away."""
    set_last_sync(datetime.now(timezone.utc) - timedelta(days=3))
    first = scheduler(24)._first_run()
    assert first - datetime.now(timezone.utc) < timedelta(seconds=70)
