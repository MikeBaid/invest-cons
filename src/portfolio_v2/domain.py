"""Pure portfolio calculations. All monetary inputs keep their source currency."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from math import isfinite
from typing import Literal


def amount(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def currency_code(value: str) -> str:
    code = value.strip().upper()
    return "RUB" if code == "RUR" else code


@dataclass(frozen=True, slots=True)
class Account:
    broker: str
    account_id: str
    owner_id: str
    kind: str = "ordinary"
    label: str = ""

    @property
    def contour(self) -> str:
        return "rf" if self.broker == "tinkoff" else "external"

    @property
    def key(self) -> str:
        return f"{self.broker}:{self.account_id}"


@dataclass(frozen=True, slots=True)
class Position:
    account_key: str
    instrument_id: str
    name: str
    kind: str
    quantity_units: Decimal
    price_currency: str
    price_per_unit: Decimal | None
    accrued_per_unit: Decimal = Decimal("0")
    broker_value_native: Decimal | None = None
    accrued_currency: str | None = None
    floating_coupon_flag: bool | None = None

    @property
    def value_native(self) -> Decimal | None:
        if self.broker_value_native is not None:
            return self.broker_value_native
        if self.price_per_unit is None:
            return None
        if (self.accrued_per_unit != 0 and self.accrued_currency and
                currency_code(self.accrued_currency) != currency_code(self.price_currency)):
            return None
        return self.quantity_units * (self.price_per_unit + self.accrued_per_unit)


@dataclass(frozen=True, slots=True)
class FxFixing:
    rate_date: datetime
    source: str
    rub_per_currency: dict[str, Decimal]

    def convert(self, value: Decimal, source: str, target: str) -> Decimal | None:
        src = currency_code(source)
        dst = currency_code(target)
        if src == dst:
            return value
        src_rate = Decimal("1") if src == "RUB" else self.rub_per_currency.get(src)
        dst_rate = Decimal("1") if dst == "RUB" else self.rub_per_currency.get(dst)
        if src_rate is None or dst_rate is None or src_rate <= 0 or dst_rate <= 0:
            return None
        return value * src_rate / dst_rate


@dataclass(frozen=True, slots=True)
class ValuedPosition:
    position: Position
    value_rub: Decimal | None
    value_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class Snapshot:
    as_of: datetime
    accounts: tuple[Account, ...]
    positions: tuple[ValuedPosition, ...]
    account_errors: tuple[str, ...] = ()
    valuation_errors: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return bool(self.accounts) and not self.account_errors and not self.valuation_errors

    def total(self, currency: Literal["RUB", "USD"]) -> Decimal | None:
        if not self.complete:
            return None
        values = [p.value_rub if currency == "RUB" else p.value_usd for p in self.positions]
        if any(value is None for value in values):
            return None
        return sum((value for value in values if value is not None), Decimal("0"))


def value_portfolio(
    *,
    as_of: datetime,
    accounts: list[Account],
    positions: list[Position],
    fx: FxFixing,
    account_errors: list[str] | None = None,
) -> Snapshot:
    known = {account.key for account in accounts}
    errors: list[str] = []
    valued: list[ValuedPosition] = []
    for p in positions:
        if p.account_key not in known:
            errors.append(f"unknown_account:{p.account_key}:{p.instrument_id}")
        if p.price_per_unit is None and p.broker_value_native is None:
            errors.append(f"missing_price:{p.account_key}:{p.instrument_id}")
            valued.append(ValuedPosition(p, None, None))
            continue
        if p.broker_value_native is not None:
            components = [(p.broker_value_native, p.price_currency)]
        else:
            components = [
                (p.quantity_units * p.price_per_unit, p.price_currency),
                (p.quantity_units * p.accrued_per_unit,
                 p.accrued_currency or p.price_currency),
            ]
        rub_components = [fx.convert(value, ccy, "RUB") for value, ccy in components]
        usd_components = [fx.convert(value, ccy, "USD") for value, ccy in components]
        rub = (sum((value for value in rub_components if value is not None), Decimal("0"))
               if all(value is not None for value in rub_components) else None)
        usd = (sum((value for value in usd_components if value is not None), Decimal("0"))
               if all(value is not None for value in usd_components) else None)
        if rub is None or usd is None:
            errors.append(f"missing_fx:{p.account_key}:{p.instrument_id}:{p.price_currency}")
        valued.append(ValuedPosition(p, rub, usd))
    return Snapshot(
        as_of=as_of,
        accounts=tuple(accounts),
        positions=tuple(valued),
        account_errors=tuple(account_errors or ()),
        valuation_errors=tuple(errors),
    )


@dataclass(frozen=True, slots=True)
class CashFlow:
    account_key: str
    occurred_at: datetime
    signed_amount_native: Decimal
    currency: str
    amount_rub: Decimal | None
    amount_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class Performance:
    start: datetime
    end: datetime
    currency: str
    start_nav: Decimal
    end_nav: Decimal
    net_external_flow: Decimal
    pnl: Decimal
    modified_dietz_pct: Decimal | None
    xirr_pct: Decimal | None


def _xirr(start: datetime, end: datetime, start_nav: Decimal, end_nav: Decimal,
          flows: list[tuple[datetime, Decimal]]) -> Decimal | None:
    points = [(start, -float(start_nav))]
    points.extend((ts, -float(value)) for ts, value in flows)
    points.append((end, float(end_nav)))
    if (end - start).total_seconds() < 86400:
        return None
    years = [(ts - start).total_seconds() / (365.25 * 86400) for ts, _ in points]

    def npv(rate: float) -> float:
        try:
            return sum(v / ((1.0 + rate) ** y) for y, (_, v) in zip(years, points))
        except (OverflowError, ZeroDivisionError):
            return float("nan")

    grid = [-0.9999, -0.99, -0.9, -0.5, 0, 0.1, 0.5, 1, 2, 5, 10, 100]
    brackets: list[tuple[float, float]] = []
    for left, right in zip(grid, grid[1:]):
        vl, vr = npv(left), npv(right)
        if isfinite(vl) and abs(vl) < 1e-10:
            return amount(left * 100)
        if isfinite(vr) and abs(vr) < 1e-10:
            return amount(right * 100)
        if isfinite(vl) and isfinite(vr) and vl * vr < 0:
            brackets.append((left, right))
    if len(brackets) != 1:
        return None
    low, high = brackets[0]
    low_value = npv(low)
    for _ in range(100):
        mid = (low + high) / 2
        if npv(mid) * low_value > 0:
            low = mid
            low_value = npv(mid)
        else:
            high = mid
    return amount((low + high) * 50)


def calculate_performance(
    start: Snapshot,
    end: Snapshot,
    flows: list[CashFlow],
    *,
    currency: Literal["RUB", "USD"],
    operations_complete: bool,
) -> Performance | None:
    if not operations_complete or not start.complete or not end.complete:
        return None
    if start.as_of >= end.as_of:
        return None
    if {a.key for a in start.accounts} != {a.key for a in end.accounts}:
        return None
    initial = start.total(currency)
    final = end.total(currency)
    if initial is None or final is None:
        return None
    selected = [f for f in flows if start.as_of < f.occurred_at <= end.as_of]
    numbers = [((f.amount_rub if currency == "RUB" else f.amount_usd), f.occurred_at)
               for f in selected]
    if any(number is None for number, _ in numbers):
        return None
    dated = [(ts, number) for number, ts in numbers if number is not None]
    net_flow = sum((value for _, value in dated), Decimal("0"))
    pnl = final - initial - net_flow
    seconds = Decimal(str((end.as_of - start.as_of).total_seconds()))
    weighted = sum((value * Decimal(str((end.as_of - ts).total_seconds())) / seconds
                    for ts, value in dated), Decimal("0"))
    denominator = initial + weighted
    dietz = pnl / denominator * 100 if denominator > 0 else None
    return Performance(
        start=start.as_of,
        end=end.as_of,
        currency=currency,
        start_nav=initial,
        end_nav=final,
        net_external_flow=net_flow,
        pnl=pnl,
        modified_dietz_pct=dietz,
        xirr_pct=_xirr(start.as_of, end.as_of, initial, final, dated),
    )


@dataclass(frozen=True, slots=True)
class IncomeEvent:
    account_key: str
    instrument_id: str
    kind: Literal["coupon", "dividend"]
    payment_at: datetime
    amount_native: Decimal | None
    currency: str
    status: Literal["paid", "declared", "estimate"]
    source: str
    event_id: str
    amount_rub: Decimal | None = None
    amount_usd: Decimal | None = None
    notes: str = ""
