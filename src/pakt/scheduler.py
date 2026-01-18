"""Scheduler for automatic sync operations."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

if TYPE_CHECKING:
    from pakt.config import Config

logger = logging.getLogger(__name__)


class SyncScheduler:
    """Manages scheduled sync operations."""

    def __init__(
        self,
        config: Config,
        sync_func: Callable[[], Any],
        is_running_func: Callable[[], bool],
    ) -> None:
        """Initialize the scheduler.

        Args:
            config: Application configuration
            sync_func: Async function to run sync
            is_running_func: Function to check if sync is currently running
        """
        self.config = config
        self.sync_func = sync_func
        self.is_running_func = is_running_func
        self.scheduler = AsyncIOScheduler()
        self._job_id = "pakt_sync"
        self._last_run: datetime | None = None
        self._next_run: datetime | None = None

    def start(self) -> None:
        """Start the scheduler if enabled in config."""
        if not self.config.scheduler.enabled:
            logger.info("Scheduler disabled in config")
            return

        if self.config.scheduler.interval_hours <= 0:
            logger.warning("Scheduler interval must be > 0, scheduler disabled")
            return

        self._add_job()
        self.scheduler.start()
        logger.info(
            f"Scheduler started with {self.config.scheduler.interval_hours}h interval"
        )

    def stop(self) -> None:
        """Stop the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    def _add_job(self) -> None:
        """Add or update the sync job."""
        trigger = IntervalTrigger(hours=self.config.scheduler.interval_hours)

        if self.scheduler.get_job(self._job_id):
            self.scheduler.reschedule_job(self._job_id, trigger=trigger)
        else:
            self.scheduler.add_job(
                self._run_sync,
                trigger=trigger,
                id=self._job_id,
                name="Pakt Sync",
                replace_existing=True,
            )

        job = self.scheduler.get_job(self._job_id)
        if job:
            self._next_run = job.next_run_time

    async def _run_sync(self) -> None:
        """Execute the sync if not already running."""
        if self.is_running_func():
            logger.info("Scheduled sync skipped - sync already running")
            return

        logger.info("Starting scheduled sync")
        self._last_run = datetime.now()

        try:
            await self.sync_func()
        except Exception as e:
            logger.error(f"Scheduled sync failed: {e}")
        finally:
            job = self.scheduler.get_job(self._job_id)
            if job:
                self._next_run = job.next_run_time

    def update_config(self, enabled: bool, interval_hours: int) -> None:
        """Update scheduler configuration.

        Args:
            enabled: Whether scheduler should be enabled
            interval_hours: Hours between syncs
        """
        self.config.scheduler.enabled = enabled
        self.config.scheduler.interval_hours = interval_hours

        if not enabled:
            if self.scheduler.get_job(self._job_id):
                self.scheduler.remove_job(self._job_id)
                self._next_run = None
            logger.info("Scheduler disabled")
            return

        if interval_hours <= 0:
            logger.warning("Invalid interval, scheduler disabled")
            return

        if not self.scheduler.running:
            self._add_job()
            self.scheduler.start()
        else:
            self._add_job()

        logger.info(f"Scheduler updated: {interval_hours}h interval")

    @property
    def next_run(self) -> datetime | None:
        """Get the next scheduled run time."""
        job = self.scheduler.get_job(self._job_id)
        return job.next_run_time if job else None

    @property
    def last_run(self) -> datetime | None:
        """Get the last run time."""
        return self._last_run

    @property
    def is_enabled(self) -> bool:
        """Check if scheduler is enabled and running."""
        return self.scheduler.running and self.scheduler.get_job(self._job_id) is not None

    def get_status(self) -> dict[str, Any]:
        """Get scheduler status."""
        return {
            "enabled": self.is_enabled,
            "interval_hours": self.config.scheduler.interval_hours,
            "next_run": self.next_run.isoformat() if self.next_run else None,
            "last_run": self._last_run.isoformat() if self._last_run else None,
        }
