from pathlib import Path

import pytest

from src.portfolio_v2.runtime_config import RuntimeConfig


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_config_loads_all_settings() -> None:
    config = RuntimeConfig.load(ROOT / "config/runtime.yaml")

    assert config.schedule.enabled is True
    assert config.initial_history_days == 90
    assert config.forecast_horizon_days == 365
    assert config.historical_fx_max_age_calendar_days == 14


@pytest.mark.parametrize(
    ("before", "after", "error"),
    [
        ("enabled: true", "enabled: 'false'", "daily_refresh.enabled"),
        ("initial_history_days: 90", "initial_history_days: -1", "portfolio_refresh.initial_history_days"),
        ("historical_rate_max_age_calendar_days: 14", "historical_rate_max_age_calendar_days: true", "fx.historical_rate_max_age_calendar_days"),
        ("schema_version: 1", "schema_version: 2", "runtime.schema_version"),
        ("forecast_horizon_days: 365", "forecast_horizon_day: 365", "portfolio_refresh keys"),
    ],
)
def test_runtime_config_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch, before: str, after: str, error: str
) -> None:
    path = ROOT / "config/runtime.yaml"
    source = path.read_text(encoding="utf-8")
    monkeypatch.setattr(Path, "read_text", lambda self, **kwargs: source.replace(before, after))

    with pytest.raises(ValueError, match=error):
        RuntimeConfig.load(path)
