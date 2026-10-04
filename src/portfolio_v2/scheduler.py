"""In-process daily portfolio synchronization schedule."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .service import PortfolioService


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DailyRefreshSchedule:
    enabled: bool
    run_at: time
    timezone: ZoneInfo

def next_run_at(now: datetime, schedule: DailyRefreshSchedule) -> datetime:
    local_now = now.astimezone(schedule.timezone)
    next_run = datetime.combine(local_now.date(), schedule.run_at, schedule.timezone)
    if next_run <= local_now:
        next_run += timedelta(days=1)
    return next_run


async def run_daily_refresh(
    service: PortfolioService,
    schedule: DailyRefreshSchedule,
) -> None:
    """Run one refresh at the configured local time, then repeat daily."""
    if not schedule.enabled:
        return

    while True:
        now = datetime.now(schedule.timezone)
        next_run = next_run_at(now, schedule)
        delay = max(0.0, (next_run - now).total_seconds())
        logger.info(
            "Next scheduled portfolio refresh at %s (%s)",
            next_run.isoformat(timespec="minutes"), schedule.timezone.key,
        )
        await asyncio.sleep(delay)
        try:
            result = await service.refresh()
            logger.info(
                "Scheduled portfolio refresh completed run_id=%s complete=%s "
                "accounts=%d positions=%d operation_errors=%d forecast_errors=%d",
                result.run_id,
                result.snapshot.complete,
                len(result.snapshot.accounts),
                len(result.snapshot.positions),
                sum(status.startswith("error:") for status in result.operation_status.values()),
                sum(status.startswith("error:") for status in result.forecast_status.values()),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduled portfolio refresh failed")
