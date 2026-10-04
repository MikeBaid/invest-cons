from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from src.portfolio_v2.scheduler import DailyRefreshSchedule, next_run_at
from src.portfolio_v2.runtime_config import RuntimeConfig


ROOT = Path(__file__).resolve().parents[1]


def test_daily_refresh_schedule_loads_moscow_time_from_config() -> None:
    schedule = RuntimeConfig.load(ROOT / "config/runtime.yaml").schedule

    assert schedule.enabled
    assert schedule.run_at == time(6, 0)
    assert schedule.timezone.key == "Europe/Moscow"


def test_next_daily_refresh_uses_next_local_occurrence() -> None:
    schedule = DailyRefreshSchedule(True, time(6, 0), ZoneInfo("Europe/Moscow"))
    before = datetime(2026, 10, 4, 2, 0, tzinfo=ZoneInfo("UTC"))
    after = datetime(2026, 10, 4, 4, 0, tzinfo=ZoneInfo("UTC"))

    assert next_run_at(before, schedule).isoformat() == "2026-10-04T06:00:00+03:00"
    assert next_run_at(after, schedule).isoformat() == "2026-10-05T06:00:00+03:00"
