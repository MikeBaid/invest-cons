"""Validated, non-secret settings for the running portfolio process."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .scheduler import DailyRefreshSchedule


def _mapping(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return value


def _keys(value: dict, expected: set[str], name: str) -> None:
    missing = expected - value.keys()
    unknown = value.keys() - expected
    if missing or unknown:
        raise ValueError(f"{name} keys: missing={sorted(missing)}, unknown={sorted(unknown)}")


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    schedule: DailyRefreshSchedule
    initial_history_days: int
    forecast_horizon_days: int
    historical_fx_max_age_calendar_days: int

    @classmethod
    def load(cls, path: Path) -> "RuntimeConfig":
        data = _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), "runtime")
        _keys(data, {"schema_version", "daily_refresh", "portfolio_refresh", "fx"}, "runtime")
        if data.get("schema_version") != 1 or isinstance(data.get("schema_version"), bool):
            raise ValueError("runtime.schema_version must be 1")
        schedule = _mapping(data.get("daily_refresh"), "daily_refresh")
        _keys(schedule, {"enabled", "time", "timezone"}, "daily_refresh")
        if type(schedule.get("enabled")) is not bool:
            raise ValueError("daily_refresh.enabled must be a boolean")
        raw_time = schedule.get("time")
        if not isinstance(raw_time, str):
            raise ValueError("daily_refresh.time must be a local HH:MM string")
        run_at = time.fromisoformat(raw_time)
        if run_at.tzinfo is not None:
            raise ValueError("daily_refresh.time must be a local wall-clock time")
        raw_timezone = schedule.get("timezone")
        if not isinstance(raw_timezone, str):
            raise ValueError("daily_refresh.timezone must be an IANA timezone")
        refresh = _mapping(data.get("portfolio_refresh"), "portfolio_refresh")
        fx = _mapping(data.get("fx"), "fx")
        _keys(refresh, {"initial_history_days", "forecast_horizon_days"}, "portfolio_refresh")
        _keys(fx, {"historical_rate_max_age_calendar_days"}, "fx")
        return cls(
            schedule=DailyRefreshSchedule(
                enabled=schedule["enabled"],
                run_at=run_at,
                timezone=ZoneInfo(raw_timezone),
            ),
            initial_history_days=_non_negative_int(
                refresh.get("initial_history_days"), "portfolio_refresh.initial_history_days"
            ),
            forecast_horizon_days=_non_negative_int(
                refresh.get("forecast_horizon_days"), "portfolio_refresh.forecast_horizon_days"
            ),
            historical_fx_max_age_calendar_days=_non_negative_int(
                fx.get("historical_rate_max_age_calendar_days"),
                "fx.historical_rate_max_age_calendar_days",
            ),
        )
