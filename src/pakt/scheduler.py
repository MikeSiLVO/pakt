"""Scheduler for automatic sync operations."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from pakt.config import get_last_sync

if TYPE_CHECKING:
    from pakt.config import Config

logger = logging.getLogger(__name__)


class SyncScheduler:
    """Interval scheduler that runs the sync from inside pakt serve."""

    def __init__(
        self,
        config: Config,
        sync_func: Callable[[], Any],
        is_running_func: Callable[[], bool],
    ) -> None:
        self.config = config
        self.sync_func = sync_func
        self.is_running_func = is_running_func
        self.scheduler = AsyncIOScheduler()
        self._job_id = "pakt_sync"

    def start(self) -> None:
        """Start the scheduler if config enables it with a positive interval."""
        if not self.config.scheduler.enabled:
            logger.info("Scheduler off")
            return

        if self.config.scheduler.interval_hours <= 0:
            logger.warning("Scheduler off: its interval must be at least 1 hour")
            return

        self._add_job()
        self.scheduler.start()
        self._log_on()

    def stop(self) -> None:
        """Stop the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    def _first_run(self) -> datetime:
        """When the next sync is due: an interval after the last one, at least a minute away."""
        soon = datetime.now(timezone.utc) + timedelta(minutes=1)
        last = get_last_sync()
        if last is None:
            return soon
        return max(soon, last + timedelta(hours=self.config.scheduler.interval_hours))

    def _add_job(self) -> None:
        """Add or update the sync job."""
        trigger = IntervalTrigger(hours=self.config.scheduler.interval_hours, start_date=self._first_run())

        if self.scheduler.get_job(self._job_id):
            self.scheduler.reschedule_job(self._job_id, trigger=trigger)
        else:
            self.scheduler.add_job(
                self._run_sync,
                trigger=trigger,
                id=self._job_id,
                name="Pakt Sync",
                replace_existing=True,
                # a sync can outlast APScheduler's 1s default grace, which would drop that run
                misfire_grace_time=None,
                coalesce=True,
                max_instances=1,
            )

    async def _run_sync(self) -> None:
        """Run the sync unless one is already running, logging a failure instead of raising."""
        if self.is_running_func():
            logger.info("Scheduled sync skipped, a sync is already running")
            return

        try:
            await self.sync_func()
        except Exception as e:
            logger.exception(f"Scheduled sync failed: {type(e).__name__}: {e}")

    def update_config(self, enabled: bool, interval_hours: int) -> None:
        """Update scheduler configuration, dropping the job when disabled or the interval is bad."""
        self.config.scheduler.enabled = enabled
        self.config.scheduler.interval_hours = interval_hours

        if not enabled:
            if self.scheduler.get_job(self._job_id):
                self.scheduler.remove_job(self._job_id)
            logger.info("Scheduler off")
            return

        if interval_hours <= 0:
            # a leftover job would keep firing while config and UI report the scheduler off
            if self.scheduler.get_job(self._job_id):
                self.scheduler.remove_job(self._job_id)
            logger.warning("Scheduler off: its interval must be at least 1 hour")
            return

        if not self.scheduler.running:
            self._add_job()
            self.scheduler.start()
        else:
            self._add_job()

        self._log_on()

    def _log_on(self) -> None:
        """Log that the scheduler is on, with its interval and when the next sync is due."""
        next_run = self.next_run
        due = f", next sync {next_run.astimezone():%Y-%m-%d %H:%M}" if next_run else ""
        logger.info(f"Scheduler on: every {self.config.scheduler.interval_hours}h{due}")

    @property
    def next_run(self) -> datetime | None:
        """Get the next scheduled run time; a job queued before start() has none yet."""
        job = self.scheduler.get_job(self._job_id)
        return getattr(job, "next_run_time", None)

    @property
    def is_enabled(self) -> bool:
        """Whether the scheduler is running with a job queued."""
        return self.scheduler.running and self.scheduler.get_job(self._job_id) is not None

    def get_status(self) -> dict[str, Any]:
        """Get the scheduler's state, interval, and next and last run times."""
        return {
            "enabled": self.is_enabled,
            "interval_hours": self.config.scheduler.interval_hours,
            "next_run": self.next_run.isoformat() if self.next_run else None,
            "last_run": last.isoformat() if (last := get_last_sync()) else None,
        }
