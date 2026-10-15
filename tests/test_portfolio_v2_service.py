"""The application never converts partial broker data into a numeric return."""

import asyncio
import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from src.portfolio_v2.bot import portfolio_lines
from src.portfolio_v2.domain import Account, CashFlow, FxFixing, Position, value_portfolio
from src.portfolio_v2.service import PortfolioService, operation_sync_start


NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)
ACCOUNT = Account("tinkoff", "A", "owner")
FX = FxFixing(NOW, "test", {"USD": Decimal("80"), "RUB": Decimal("1")})


def _snapshot(when: datetime, cash: int, errors: list[str] | None = None):
    return value_portfolio(
        as_of=when, accounts=[ACCOUNT],
        positions=[Position(ACCOUNT.key, "CASH.RUB", "Cash", "cash",
                            Decimal(cash), "RUB", Decimal("1"))],
        fx=FX, account_errors=errors,
    )


class FakeStore:
    def __init__(self, snapshots, covered: bool):
        self.snapshots = snapshots
        self.covered = covered
        self.flows = [CashFlow(ACCOUNT.key, NOW - timedelta(days=1), Decimal("20"),
                               "RUB", Decimal("20"), Decimal("0.25"))]

    async def latest_snapshots(self, *, limit: int = 100):
        return self.snapshots[:limit]

    async def operations_cover(self, *_args):
        return self.covered

    async def flows_between(self, *_args):
        return self.flows


def test_performance_is_gated_by_operation_coverage() -> None:
    latest = _snapshot(NOW, 130)
    first = _snapshot(NOW - timedelta(days=10), 100)
    service = PortfolioService(FakeStore([latest, first], False), None, None,
                               historical_fx_max_age_calendar_days=14)
    result = asyncio.run(service.performance())
    assert result.rub is None and result.usd is None
    assert "потоки" in result.reason.lower()


def test_manual_classification_validates_leaf_contour_and_current_position() -> None:
    class ClassificationStore(FakeStore):
        def __init__(self):
            super().__init__([], True)
            self.rows = {}
            self.saved = []

        async def active_classifications(self):
            return self.rows

        async def set_active_classifications(self, rows):
            self.saved.extend(rows)
            return len(rows)

    store = ClassificationStore()
    service = PortfolioService(
        store, None, None, historical_fx_max_age_calendar_days=14,
        ips_path=Path(__file__).resolve().parents[1] / "config/ips/personal_main.draft.yaml",
    )
    snapshot = value_portfolio(
        as_of=NOW, accounts=[ACCOUNT],
        positions=[Position(ACCOUNT.key, "NEW", "New bond", "bond",
                            Decimal("1"), "RUB", Decimal("100"))], fx=FX,
    )

    async def portfolio():
        return snapshot

    service.portfolio = portfolio
    assert asyncio.run(service.classify_instrument("rf", "NEW", "rf.bonds.rub_fixed", 42))
    assert store.saved[0]["source"] == "telegram_manual"
    assert store.saved[0]["classification_version"] == "draft-1"
    with pytest.raises(ValueError, match="leaf"):
        asyncio.run(service.classify_instrument("rf", "NEW", "rf.bonds", 42))
    with pytest.raises(ValueError, match="contour"):
        asyncio.run(service.classify_instrument("rf", "NEW", "external.bonds.kz_fx", 42))
    with pytest.raises(ValueError, match="current portfolio"):
        asyncio.run(service.classify_instrument("rf", "MISSING", "rf.bonds.rub_fixed", 42))


def test_performance_has_separate_rub_and_usd_ledger_views() -> None:
    latest = _snapshot(NOW, 130)
    first = _snapshot(NOW - timedelta(days=10), 100)
    service = PortfolioService(FakeStore([latest, first], True), None, None,
                               historical_fx_max_age_calendar_days=14)
    result = asyncio.run(service.performance())
    assert result.rub and result.rub.pnl == Decimal("10")
    assert result.usd and result.usd.pnl == Decimal("0.125")


def test_latest_partial_snapshot_is_not_silently_skipped() -> None:
    latest = _snapshot(NOW, 0, errors=["freedom:unavailable"])
    first = _snapshot(NOW - timedelta(days=10), 100)
    service = PortfolioService(FakeStore([latest, first], True), None, None,
                               historical_fx_max_age_calendar_days=14)
    result = asyncio.run(service.performance())
    assert result.rub is None
    assert result.reason == "Последний снимок неполный"
    assert "н/д" in " ".join(portfolio_lines(latest))


def test_broker_scope_survives_other_broker_failure() -> None:
    freedom = Account("freedom", "F", "owner")

    def snapshot(when: datetime, cash: int, *, failed: bool):
        return value_portfolio(
            as_of=when, accounts=[ACCOUNT, freedom],
            positions=[Position(freedom.key, "CASH.USD", "Cash", "cash",
                                Decimal(cash), "USD", Decimal("1"))],
            fx=FX,
            account_errors=[f"{ACCOUNT.key}:positions:NotFound"] if failed else [],
        )

    latest = snapshot(NOW, 110, failed=True)
    first = snapshot(NOW - timedelta(days=10), 100, failed=False)
    service = PortfolioService(FakeStore([latest, first], True), None, None,
                               historical_fx_max_age_calendar_days=14)
    combined = asyncio.run(service.performance())
    scoped = asyncio.run(service.performance(broker="freedom"))
    assert combined.rub is None
    assert scoped.rub and scoped.rub.pnl == Decimal("800")


def test_operation_sync_backfills_from_calendar_year_start_once_needed() -> None:
    from zoneinfo import ZoneInfo

    timezone_moscow = ZoneInfo("Europe/Moscow")
    now = datetime(2026, 10, 4, 9, 0, tzinfo=timezone_moscow)
    previous = datetime(2026, 10, 3, 9, 0, tzinfo=timezone_moscow)

    start = operation_sync_start(
        now, previous, initial_history_days=90,
        needs_year_backfill=True, calendar_timezone=timezone_moscow,
    )
    next_incremental = operation_sync_start(
        now, previous, initial_history_days=90,
        needs_year_backfill=False, calendar_timezone=timezone_moscow,
    )

    assert start == datetime(2025, 12, 31, 21, 0, tzinfo=timezone.utc)
    assert next_incremental == previous - timedelta(days=2)


def test_income_coverage_is_checked_as_of_the_saved_snapshot() -> None:
    class IncomeStore:
        coverage_end = None

        async def latest_snapshots(self, *, limit=100):
            return [_snapshot(NOW, 100)]

        async def income(self, *_args):
            return [], []

        async def income_operations_coverage(self, _accounts, _start, end):
            self.coverage_end = end
            return True, 1, 1

        async def forecast_errors(self):
            return {}

        async def active_classifications(self):
            return {}

        async def load_fx(self, _day):
            return FX

    store = IncomeStore()
    service = PortfolioService(
        store, None, None,
        historical_fx_max_age_calendar_days=14,
        ips_path=Path(__file__).resolve().parents[1] / "config/ips/personal_main.draft.yaml",
    )
    report_now = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    report = asyncio.run(service.income_report(now=report_now))
    assert store.coverage_end == NOW
    assert report.actual_coverage_complete
