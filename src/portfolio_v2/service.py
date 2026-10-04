"""Application boundary: synchronize broker facts and query independently of Telegram."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from .adapters import FreedomAdapter, Handle, TInvestAdapter
from .allocation import PortfolioAllocation, build_portfolio_allocation
from .domain import FxFixing, Performance, Snapshot, calculate_performance, value_portfolio
from .fx import fetch_cbr_daily
from .income import IncomeReport, build_income_report
from .policy import InvestmentScopePolicy
from .storage import Store


logger = logging.getLogger(__name__)


def _operation_error_code(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        message = str(exc).lower()
        if "undated" in message:
            return "invalid_operation_missing_date"
        if "cursor stopped" in message:
            return "operations_cursor_stalled"
        if "page limit" in message:
            return "operations_page_limit_exceeded"
        if "cash_in_outs" in message:
            return "broker_report_missing_cash_rows"
        return "invalid_operation_response"
    if type(exc).__name__ == "ConnectError":
        return "network_connect_error"
    return f"operation_sync_{type(exc).__name__}"


def operation_sync_start(
    now: datetime,
    previous: datetime | None,
    *,
    initial_history_days: int,
    needs_year_backfill: bool,
    calendar_timezone: tzinfo,
) -> datetime:
    start = (previous - timedelta(days=2) if previous else
             now - timedelta(days=initial_history_days))
    if needs_year_backfill:
        local_now = now.astimezone(calendar_timezone)
        year_start = datetime(local_now.year, 1, 1, tzinfo=calendar_timezone)
        start = min(start, year_start.astimezone(timezone.utc))
    return start


@dataclass(frozen=True, slots=True)
class RefreshResult:
    snapshot: Snapshot
    run_id: str
    operation_status: dict[str, str]
    forecast_status: dict[str, str]


@dataclass(frozen=True, slots=True)
class PerformanceResult:
    latest: Snapshot | None
    start: Snapshot | None
    rub: Performance | None
    usd: Performance | None
    reason: str | None


class PortfolioService:
    def __init__(self, store: Store, tinkoff: TInvestAdapter,
                 freedom: FreedomAdapter, *, initial_history_days: int = 90,
                 scope_policy: InvestmentScopePolicy | None = None,
                 ips_path: Path | None = None,
                 calendar_timezone: tzinfo | None = None):
        self.store = store
        self.adapters = (tinkoff, freedom)
        self.initial_history_days = initial_history_days
        self.scope_policy = scope_policy
        self.ips_path = ips_path
        self.calendar_timezone = calendar_timezone or ZoneInfo("Europe/Moscow")
        self._refresh_lock = asyncio.Lock()
        self._fx_cache: dict[date, FxFixing] = {}

    async def _fx(self, when: datetime) -> FxFixing:
        day = when.date()
        if day in self._fx_cache:
            return self._fx_cache[day]
        cached = await self.store.load_fx(day)
        if cached is not None:
            self._fx_cache[day] = cached
            return cached
        fixing = await fetch_cbr_daily(day)
        await self.store.save_fx(day, fixing)
        self._fx_cache[day] = fixing
        return fixing

    async def refresh(self) -> RefreshResult:
        async with self._refresh_lock:
            now = datetime.now(timezone.utc)
            local_now = now.astimezone(self.calendar_timezone)
            year_start = datetime(local_now.year, 1, 1,
                                  tzinfo=self.calendar_timezone).astimezone(timezone.utc)
            fixing = await self._fx(now)
            discovered: list[tuple[Any, Handle]] = []
            errors: list[str] = []
            positions = []
            position_failed: set[str] = set()
            operation_status: dict[str, str] = {}
            forecast_status: dict[str, str] = {}

            for adapter in self.adapters:
                broker = "tinkoff" if isinstance(adapter, TInvestAdapter) else "freedom"
                try:
                    handles = await adapter.discover()
                    if not handles:
                        errors.append(f"{broker}:no_accounts")
                    discovered.extend((adapter, handle) for handle in handles)
                except Exception as exc:
                    logger.exception("%s account discovery failed", broker)
                    errors.append(f"{broker}:discover:{type(exc).__name__}")

            for adapter, handle in discovered:
                try:
                    account_positions = await adapter.positions(handle)
                    positions.extend(account_positions)
                    cash_warning = getattr(adapter, "position_warnings", {}).get(
                        handle.account.key
                    )
                    if cash_warning:
                        errors.append(
                            f"{handle.account.key}:cash_balances:{cash_warning}"
                        )
                except Exception as exc:
                    logger.exception("Position sync failed for %s", handle.account.key)
                    errors.append(f"{handle.account.key}:positions:{type(exc).__name__}")
                    position_failed.add(handle.account.key)

            snapshot = value_portfolio(
                as_of=now, accounts=[h.account for _, h in discovered],
                positions=positions, fx=fixing, account_errors=errors,
            )
            run_id = uuid4()
            await self.store.save_snapshot(run_id, snapshot, fixing)

            for adapter, handle in discovered:
                key = handle.account.key
                previous = await self.store.sync_start(key)
                needs_backfill = await self.store.needs_income_backfill(key, year_start)
                start = operation_sync_start(
                    now, previous,
                    initial_history_days=self.initial_history_days,
                    needs_year_backfill=needs_backfill,
                    calendar_timezone=self.calendar_timezone,
                )
                try:
                    broker_events, verified = await adapter.operations(handle, start, now)
                    rows = []
                    fx_errors = False
                    for event in broker_events:
                        event_fx = await self._fx(event.occurred_at)
                        rub = event_fx.convert(event.amount_native, event.currency, "RUB")
                        usd = event_fx.convert(event.amount_native, event.currency, "USD")
                        if event.kind in {"external_in", "external_out", "coupon", "dividend"}:
                            fx_errors |= rub is None or usd is None
                        rows.append({
                            "event_id": event.event_id, "occurred_at": event.occurred_at,
                            "event_kind": event.kind, "instrument_id": event.instrument_id,
                            "amount_native": event.amount_native, "currency": event.currency,
                            "amount_rub": rub, "amount_usd": usd, "source": event.source,
                            "raw": event.raw,
                        })
                    await self.store.save_events(
                        key, rows, success_from=start, success_to=now,
                        flows_verified=verified and not fx_errors,
                        income_verified=verified and not fx_errors,
                        error="unverified_events_or_fx" if not verified or fx_errors else None,
                    )
                    unresolved = sum(event.kind == "unresolved" for event in broker_events)
                    operation_status[key] = (
                        f"review_required:{unresolved}" if unresolved else
                        "ok" if verified and not fx_errors else "review_required"
                    )
                except Exception as exc:
                    logger.exception("Operation sync failed for %s", key)
                    error_code = _operation_error_code(exc)
                    await self.store.save_sync_error(key, error_code)
                    operation_status[key] = f"error:{error_code}"

            horizon = now + timedelta(days=365)
            for adapter, handle in discovered:
                key = handle.account.key
                if key in position_failed:
                    forecast_status[key] = "error:positions_unavailable"
                    await self.store.save_forecasts(key, [])
                    await self.store.save_forecast_errors(key, ["positions_unavailable"])
                    continue
                own_positions = [p for p in positions if p.account_key == key]
                try:
                    events, calendar_errors = await adapter.forecasts(handle, own_positions, now, horizon)
                    await self.store.save_forecasts(key, events)
                    await self.store.save_forecast_errors(key, calendar_errors)
                    forecast_status[key] = "ok" if not calendar_errors else f"partial:{len(calendar_errors)}"
                except Exception as exc:
                    logger.exception("Forecast sync failed for %s", key)
                    forecast_status[key] = f"error:{type(exc).__name__}"
                    await self.store.save_forecasts(key, [])
                    await self.store.save_forecast_errors(key, [type(exc).__name__])

            return RefreshResult(snapshot, str(run_id), operation_status, forecast_status)

    async def portfolio(self) -> Snapshot | None:
        rows = await self.store.latest_snapshots(limit=1)
        if not rows:
            return None
        return self.scope_policy.apply(rows[0]) if self.scope_policy else rows[0]

    async def portfolio_allocation(self) -> PortfolioAllocation | None:
        if self.ips_path is None:
            raise RuntimeError("IPS path is not configured")
        snapshot = await self.portfolio()
        if snapshot is None:
            return None
        assignments = await self.store.active_classifications()
        return build_portfolio_allocation(snapshot, assignments, self.ips_path)

    @staticmethod
    def _scope(snapshot: Snapshot, keys: set[str]) -> Snapshot:
        accounts = tuple(account for account in snapshot.accounts if account.key in keys)
        selected_brokers = {account.broker for account in accounts}
        return Snapshot(
            as_of=snapshot.as_of,
            accounts=accounts,
            positions=tuple(p for p in snapshot.positions if p.position.account_key in keys),
            account_errors=tuple(error for error in snapshot.account_errors if
                                 any(error.startswith(f"{key}:") for key in keys) or
                                 any(error.startswith(f"{broker}:discover:") for broker in selected_brokers)),
            valuation_errors=tuple(error for error in snapshot.valuation_errors if
                                   any(f":{key}:" in error for key in keys)),
        )

    async def performance(self, *, max_days: int = 30,
                          broker: str | None = None) -> PerformanceResult:
        raw_snapshots = await self.store.latest_snapshots(limit=100)
        if not raw_snapshots:
            return PerformanceResult(None, None, None, None, "Нет сохраненных снимков")
        snapshots = ([self.scope_policy.apply(snapshot) for snapshot in raw_snapshots]
                     if self.scope_policy else raw_snapshots)
        keys = ({account.key for account in snapshots[0].accounts if account.broker == broker}
                if broker else {account.key for account in snapshots[0].accounts})
        if not keys:
            return PerformanceResult(None, None, None, None, "Нет счетов в выбранном контуре")
        if broker is not None:
            snapshots = [self._scope(snapshot, keys) for snapshot in snapshots]
        latest = snapshots[0]
        if not latest.complete:
            return PerformanceResult(latest, None, None, None, "Последний снимок неполный")
        eligible = [s for s in snapshots[1:] if s.complete and
                    s.as_of <= latest.as_of - timedelta(days=1) and
                    s.as_of >= latest.as_of - timedelta(days=max_days) and
                    {a.key for a in s.accounts} == {a.key for a in latest.accounts}]
        if not eligible:
            return PerformanceResult(latest, None, None, None,
                                     "Нужны два полных снимка с одинаковым составом счетов")
        start = eligible[-1]
        covered = await self.store.operations_cover(latest.accounts, start.as_of, latest.as_of)
        if not covered:
            return PerformanceResult(latest, start, None, None,
                                     "Не подтверждены все внешние потоки между снимками")
        flows = await self.store.flows_between(start.as_of, latest.as_of)
        flows = [flow for flow in flows if flow.account_key in keys]
        if self.scope_policy:
            affected_keys = {valued.position.account_key for snapshot in raw_snapshots
                             if snapshot.as_of in {start.as_of, latest.as_of}
                             for valued in snapshot.positions
                             if valued.position.account_key in keys and
                             valued.position.instrument_id in self.scope_policy.excluded_instrument_ids}
            excluded_flows = await self.store.excluded_instrument_flows_between(
                start.as_of, latest.as_of, self.scope_policy.excluded_instrument_ids,
                keys, affected_keys,
            )
            if excluded_flows is None:
                return PerformanceResult(latest, start, None, None,
                                         "Операции исключенных бумаг не позволяют рассчитать доходность")
            flows.extend(excluded_flows)
        rub = calculate_performance(start, latest, flows, currency="RUB", operations_complete=True)
        usd = calculate_performance(start, latest, flows, currency="USD", operations_complete=True)
        reason = None if rub is not None and usd is not None else "Не хватает FX или иных данных периода"
        return PerformanceResult(latest, start, rub, usd, reason)

    async def income(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        actual, forecast = await self.store.income(now - timedelta(days=30), now + timedelta(days=365))
        current = await self.portfolio()
        fx = await self._fx(now)
        upcoming = []
        for row in forecast:
            if row["payment_at"] < now:
                continue
            value: Decimal | None = row["amount_native"]
            upcoming.append({**row,
                             "amount_rub": fx.convert(value, row["currency"], "RUB") if value is not None else None,
                             "amount_usd": fx.convert(value, row["currency"], "USD") if value is not None else None})
        excluded_ids = self.scope_policy.excluded_instrument_ids if self.scope_policy else frozenset()
        included_keys = {account.key for account in current.accounts} if current else set()
        paid = [row for row in actual if row["occurred_at"] <= now and
                row["instrument_id"] not in excluded_ids and row["account_key"] in included_keys]
        upcoming = [row for row in upcoming if row["instrument_id"] not in excluded_ids
                    and row["account_key"] in included_keys]
        covered = bool(current and current.complete and await self.store.operations_cover(
            current.accounts, now - timedelta(days=30), now,
        ))
        freedom_equities = bool(current and any(
            valued.position.account_key.startswith("freedom:") and
            valued.position.kind == "equity" for valued in current.positions
        ))
        return {
            "as_of": now, "snapshot_as_of": current.as_of if current else None,
            "actual": paid, "forecast": upcoming,
            "actual_coverage_complete": covered and not freedom_equities,
            "actual_coverage_notes": (["Не подтвержден охват операций"] if not covered else []) +
                                     (["Типы дивидендов Freedom не сверены с брокерским отчетом"]
                                      if freedom_equities else []),
            "forecast_errors": {key: errors for key, errors in
                                (await self.store.forecast_errors()).items() if key in included_keys},
            "fx_source": fx.source, "fx_date": fx.rate_date,
        }

    async def income_report(self, *, now: datetime | None = None) -> IncomeReport:
        if self.ips_path is None:
            raise RuntimeError("IPS path is not configured")
        report_now = now or datetime.now(self.calendar_timezone)
        local_now = report_now.astimezone(self.calendar_timezone)
        year_start = datetime(local_now.year, 1, 1, tzinfo=self.calendar_timezone)
        year_end = datetime(local_now.year, 12, 31, 23, 59, 59, 999999,
                            tzinfo=self.calendar_timezone)
        start_utc = year_start.astimezone(timezone.utc)
        end_utc = year_end.astimezone(timezone.utc)
        now_utc = report_now.astimezone(timezone.utc)
        actual, forecasts = await self.store.income(start_utc, end_utc)
        snapshot = await self.portfolio()
        included_accounts = {account.key for account in snapshot.accounts} if snapshot else set()
        excluded_ids = self.scope_policy.excluded_instrument_ids if self.scope_policy else frozenset()
        actual = [row for row in actual if row["account_key"] in included_accounts
                  and row["instrument_id"] not in excluded_ids
                  and row["occurred_at"] <= now_utc]
        forecasts = [row for row in forecasts if row["account_key"] in included_accounts
                     and row["instrument_id"] not in excluded_ids
                     and now_utc <= row["payment_at"] <= end_utc]
        # Coverage describes the persisted snapshot, not wall-clock time when the
        # user opens the report; comparing with now would make every old snapshot stale.
        coverage_end = min(
            now_utc, snapshot.as_of.astimezone(timezone.utc),
        ) if snapshot else now_utc
        covered, confirmed_accounts, total_accounts = (
            await self.store.income_operations_coverage(
                snapshot.accounts, start_utc, coverage_end,
            ) if snapshot and snapshot.accounts else (False, 0, 0)
        )
        raw_forecast_errors = await self.store.forecast_errors()
        forecast_errors = {key: errors for key, errors in raw_forecast_errors.items()
                           if key in included_accounts and errors}
        assignments = await self.store.active_classifications()
        fixing = await self._fx(now_utc)
        return build_income_report(
            snapshot=snapshot,
            assignments=assignments,
            ips_path=self.ips_path,
            actual_events=actual,
            forecast_events=forecasts,
            fixing=fixing,
            now=report_now,
            calendar_tz=self.calendar_timezone,
            actual_coverage_complete=covered,
            forecast_errors=forecast_errors,
            actual_coverage_accounts=(confirmed_accounts, total_accounts),
            excluded_instrument_ids=excluded_ids,
        )
