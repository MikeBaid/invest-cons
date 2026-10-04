"""Broker adapters. No accounting or portfolio return formulas live here."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from t_tech.invest import AsyncClient
from t_tech.invest.schemas import AccountStatus, GetOperationsByCursorRequest, InstrumentIdType
from tradernet import Tradernet

from .domain import Account, IncomeEvent, Position, amount, currency_code

logger = logging.getLogger(__name__)


def _q(value: Any) -> Decimal | None:
    if value is None:
        return None
    if hasattr(value, "units"):
        return Decimal(str(value.units)) + Decimal(str(value.nano)) / Decimal(1_000_000_000)
    return amount(value)


def _raw(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _raw(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_raw(v) for v in value]
    if hasattr(value, "__dict__"):
        return {str(k): _raw(v) for k, v in vars(value).items()
                if not str(k).startswith("_")}
    return str(value)


def _event_id(value: Any) -> str:
    for name in ("id", "operation_id", "transaction_id"):
        val = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
        if val:
            return str(val)
    content = json.dumps(_raw(value), sort_keys=True, ensure_ascii=False, default=str)
    return "hash:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _t_invest_coupon_forecasts(
    position: Position,
    events: list[Any],
    now: datetime,
    horizon: datetime,
) -> list[IncomeEvent]:
    ordered = sorted(events, key=lambda event: _aware(getattr(event, "coupon_date", None)))
    floating = bool(position.floating_coupon_flag) or any(
        "FLOATING" in str(getattr(event, "coupon_type", "")).upper()
        or "VARIABLE" in str(getattr(event, "coupon_type", "")).upper()
        for event in ordered
    )
    carry: tuple[Decimal, str] | None = None
    if floating:
        for event in ordered:
            when = _aware(getattr(event, "coupon_date", None))
            pay = getattr(event, "pay_one_bond", None)
            per_unit = _q(pay)
            ccy = currency_code(getattr(pay, "currency", "") or position.price_currency)
            if when < now and per_unit is not None and per_unit > 0:
                carry = (per_unit, ccy)

    result: list[IncomeEvent] = []
    for event in ordered:
        when = _aware(getattr(event, "coupon_date", None))
        if not now <= when <= horizon:
            continue
        pay = getattr(event, "pay_one_bond", None)
        per_unit = _q(pay)
        ccy = currency_code(getattr(pay, "currency", "") or position.price_currency)
        status = "declared" if per_unit is not None and per_unit > 0 else "estimate"
        notes = "Текущий объем позиции; при продаже прогноз изменится."
        if (per_unit is None or per_unit <= 0) and floating and carry is not None:
            per_unit, ccy = carry
            status = "estimate"
            notes += " Использован последний известный купон плавающей облигации."
        amount_native = (per_unit * position.quantity_units
                         if per_unit is not None and per_unit > 0 else None)
        result.append(IncomeEvent(
            position.account_key, position.instrument_id, "coupon", when,
            amount_native, ccy, status, "t_invest_coupon_calendar",
            f"bond:{position.instrument_id}:{when.isoformat()}", notes=notes,
        ))
    return result


def _aware(value: Any, *, naive_zone: str = "UTC") -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        source = str(value or "").strip()
        if not source:
            raise ValueError("Event has no date")
        dt = datetime.fromisoformat(source.replace("Z", "+00:00"))
    return dt.replace(tzinfo=ZoneInfo(naive_zone)) if dt.tzinfo is None else dt


@dataclass(frozen=True, slots=True)
class Handle:
    account: Account
    token: str = ""
    payload: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class BrokerEvent:
    event_id: str
    occurred_at: datetime
    kind: str
    instrument_id: str
    amount_native: Decimal
    currency: str
    source: str
    raw: dict[str, Any]


class TInvestAdapter:
    def __init__(self, tokens: list[str], labels: list[str], owners: dict[str, str],
                 excluded_account_ids: set[str] | None = None):
        self.tokens = tokens
        self.labels = labels
        self.owners = owners
        self.excluded_account_ids = excluded_account_ids or set()
        self._instrument_names: dict[str, str] = {}
        self._bond_floating_flags: dict[str, bool | None] = {}
        self.position_warnings: dict[str, str] = {}

    async def discover(self) -> list[Handle]:
        handles: list[Handle] = []
        for index, token in enumerate(self.tokens):
            label = self.labels[index] if index < len(self.labels) else f"owner_{index + 1}"
            async with AsyncClient(token) as client:
                response = await client.users.get_accounts()
            for account in response.accounts:
                if getattr(account, "status", None) != AccountStatus.ACCOUNT_STATUS_OPEN:
                    continue
                key = (getattr(account, "id", "") or "").strip()
                if not key or key in self.excluded_account_ids:
                    continue
                handles.append(Handle(
                    Account("tinkoff", key, self.owners.get(label, label),
                            "iis" if "IIS" in str(getattr(account, "type", "")).upper() else "ordinary",
                            str(getattr(account, "name", "") or label)),
                    token,
                ))
        return handles

    async def positions(self, handle: Handle) -> list[Position]:
        self.position_warnings.pop(handle.account.key, None)
        async with AsyncClient(handle.token) as client:
            portfolio = await client.operations.get_portfolio(account_id=handle.account.account_id)
            try:
                balances = await client.operations.get_positions(account_id=handle.account.account_id)
            except Exception as exc:
                # GetPortfolio is the authoritative position snapshot. A failure to
                # fetch the supplemental cash balances must not discard its positions.
                logger.warning("T-Invest cash balances unavailable for %s: %s",
                               handle.account.key, type(exc).__name__)
                self.position_warnings[handle.account.key] = type(exc).__name__
                balances = None
            rows: list[Position] = []
            for item in portfolio.positions:
                kind = str(getattr(item, "instrument_type", "") or "").lower()
                is_bond = kind in {"bond", "debt_dfa"}
                if kind in {"currency", "cash"}:
                    continue
                instrument_id = (getattr(item, "instrument_uid", "") or
                                 getattr(item, "figi", "") or "").strip()
                if not instrument_id:
                    raise ValueError(f"Position without instrument ID on {handle.account.key}")
                quantity = _q(getattr(item, "quantity", None))
                if quantity is None:
                    raise ValueError(f"Missing quantity_units for {instrument_id}")
                price = getattr(item, "current_price", None)
                currency = currency_code(getattr(price, "currency", "") or "")
                if not currency:
                    raise ValueError(f"Missing price currency for {instrument_id}")
                nkd = getattr(item, "current_nkd", None)
                accrued = _q(nkd) if is_bond else Decimal("0")
                accrued_currency = currency_code(getattr(nkd, "currency", "") or currency)
                figi = (getattr(item, "figi", "") or "").strip()
                name = self._instrument_names.get(instrument_id)
                floating_coupon_flag = None
                if is_bond and instrument_id in self._bond_floating_flags:
                    floating_coupon_flag = self._bond_floating_flags[instrument_id]
                elif is_bond:
                    try:
                        info = await client.instruments.bond_by(
                            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID if
                            getattr(item, "instrument_uid", "") else
                            InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI,
                            id=instrument_id,
                        )
                        bond = getattr(info, "instrument", None)
                        if bond is not None:
                            name = str(getattr(bond, "name", "") or name or "")
                            floating_coupon_flag = bool(
                                getattr(bond, "floating_coupon_flag", False)
                            )
                        self._bond_floating_flags[instrument_id] = floating_coupon_flag
                    except Exception:
                        self._bond_floating_flags[instrument_id] = None
                if not name:
                    name = (getattr(item, "ticker", "") or "").strip() or instrument_id
                    try:
                        info = await client.instruments.get_instrument_by(
                            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_UID if
                            getattr(item, "instrument_uid", "") else
                            InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI,
                            id=instrument_id,
                        )
                        name = str(getattr(info.instrument, "name", "") or name)
                    except Exception:
                        pass
                    self._instrument_names[instrument_id] = name
                rows.append(Position(handle.account.key, instrument_id, name,
                                     "bond" if is_bond else
                                     "equity" if kind in {"share", "equity"} else kind or "other",
                                     quantity, currency, _q(price), accrued or Decimal("0"),
                                     accrued_currency=accrued_currency,
                                     floating_coupon_flag=floating_coupon_flag))
            for balance in getattr(balances, "money", ()) if balances is not None else ():
                ccy = currency_code(getattr(balance, "currency", "") or "")
                value = _q(balance)
                if ccy and value is not None:
                    rows.append(Position(handle.account.key, f"CASH.{ccy}", f"Cash {ccy}",
                                         "cash", value, ccy, Decimal("1")))
            return rows

    async def operations(self, handle: Handle, start: datetime, end: datetime) -> tuple[list[BrokerEvent], bool]:
        cursor: str | None = None
        rows: list[BrokerEvent] = []
        async with AsyncClient(handle.token) as client:
            for _ in range(200):
                request = GetOperationsByCursorRequest(
                    account_id=handle.account.account_id, from_=start, to=end, limit=1000,
                )
                if cursor:
                    request.cursor = cursor
                response = await client.operations.get_operations_by_cursor(request=request)
                for item in response.items:
                    event = t_operation(item)
                    if event is not None:
                        rows.append(event)
                    else:
                        raise ValueError(f"Undated T-Invest operation on {handle.account.key}")
                if not response.has_next:
                    return rows, True
                next_cursor = response.next_cursor or None
                if not next_cursor or next_cursor == cursor:
                    raise ValueError(f"Operations cursor stopped for {handle.account.key}")
                cursor = next_cursor
        raise ValueError(f"Operations page limit exceeded for {handle.account.key}")

    async def forecasts(self, handle: Handle, positions: list[Position],
                        now: datetime, horizon: datetime) -> tuple[list[IncomeEvent], list[str]]:
        result: list[IncomeEvent] = []
        errors: list[str] = []
        async with AsyncClient(handle.token) as client:
            for position in positions:
                if position.kind not in {"bond", "equity"}:
                    continue
                try:
                    if position.kind == "bond":
                        coupon_start = (now - timedelta(days=365)
                                        if position.floating_coupon_flag else now)
                        response = await client.instruments.get_bond_coupons(
                            instrument_id=position.instrument_id, from_=coupon_start, to=horizon,
                        )
                        result.extend(_t_invest_coupon_forecasts(
                            position, list(response.events), now, horizon
                        ))
                    else:
                        response = await client.instruments.get_dividends(
                            instrument_id=position.instrument_id, from_=now, to=horizon,
                        )
                        dividend_events = list(response.dividends)
                        if not dividend_events:
                            errors.append(f"dividend_calendar_empty:{position.instrument_id}")
                        for event in dividend_events:
                            payment = (getattr(event, "payment_date", None)
                                       or getattr(event, "record_date", None)
                                       or getattr(event, "declared_date", None))
                            if payment is None:
                                errors.append(f"dividend_date_missing:{position.instrument_id}")
                                continue
                            when = _aware(payment)
                            if not now <= when <= horizon:
                                continue
                            pay = getattr(event, "dividend_net", None)
                            per_unit = _q(pay)
                            ccy = currency_code(getattr(pay, "currency", "") or position.price_currency)
                            result.append(IncomeEvent(
                                handle.account.key, position.instrument_id, "dividend", when,
                                per_unit * position.quantity_units if per_unit is not None and per_unit > 0 else None,
                                ccy, "declared" if per_unit is not None and per_unit > 0 else "estimate",
                                "t_invest_dividend_calendar",
                                f"share:{position.instrument_id}:{when.isoformat()}",
                                notes="Текущий объем позиции; право на выплату требует проверки даты реестра.",
                            ))
                except Exception as exc:
                    errors.append(f"calendar:{position.instrument_id}:{type(exc).__name__}")
        return result, errors


def t_operation(item: Any) -> BrokerEvent | None:
    kind_raw = getattr(item, "type", "")
    kind = str(getattr(kind_raw, "name", "") or kind_raw).upper().removeprefix("OPERATION_TYPE_")
    payment = getattr(item, "payment", None)
    value = _q(payment)
    ccy = currency_code(getattr(payment, "currency", "") or "")
    when = getattr(item, "date", None)
    if when is None:
        return None
    if value is None or not ccy:
        value = Decimal("0")
        ccy = "RUB"
    if kind in {"INPUT", "INPUT_SWIFT", "INPUT_ACQUIRING"}:
        event_kind = "external_in"
        value = abs(value)
    elif kind in {"OUTPUT", "OUTPUT_SWIFT", "OUTPUT_ACQUIRING"}:
        event_kind = "external_out"
        value = -abs(value)
    elif kind in {"COUPON"}:
        event_kind = "coupon"
    elif kind in {"DIVIDEND", "DIV_EXT"}:
        event_kind = "dividend"
    else:
        event_kind = "other"
    raw = _raw(item)
    return BrokerEvent(_event_id(item), _aware(when), event_kind,
                       str(getattr(item, "instrument_uid", "") or
                           getattr(item, "figi", "") or ""),
                       value, ccy, "t_invest_operations", raw if isinstance(raw, dict) else {"value": raw})


class FreedomAdapter:
    def __init__(self, public_key: str, private_key: str, domain: str,
                 label: str, owner_id: str):
        self.public_key = public_key
        self.private_key = private_key
        self.domain = domain
        self.label = label
        self.owner_id = owner_id

    def _client(self) -> Tradernet:
        if self.domain == Tradernet.DOMAIN:
            return Tradernet(self.public_key, self.private_key)
        cls = type("FreedomV2Client", (Tradernet,), {"DOMAIN": self.domain})
        return cls(self.public_key, self.private_key)

    async def discover(self) -> list[Handle]:
        payload = await asyncio.to_thread(self._client().account_summary)
        if not isinstance(payload, dict) or payload.get("errMsg"):
            raise RuntimeError("Freedom account_summary failed")
        block = payload.get("ps") or payload.get("result", {}).get("ps") or {}
        key = str(block.get("key") or self.label).lstrip("%").strip()
        if not key:
            raise ValueError("Freedom account has no key")
        return [Handle(Account("freedom", key, self.owner_id, "ordinary", self.label),
                       payload=block)]

    async def positions(self, handle: Handle) -> list[Position]:
        block = handle.payload or {}
        result: list[Position] = []
        for row in block.get("pos") or []:
            if not isinstance(row, dict):
                continue
            identifier = str(row.get("i") or row.get("ticker") or "").strip()
            if not identifier:
                continue
            qty = amount(row.get("q") or 0)
            if qty == 0:
                continue
            ccy = currency_code(str(row.get("curr") or row.get("currency") or ""))
            if not ccy:
                raise ValueError(f"Freedom position {identifier} has no currency")
            kind = "bond" if str(row.get("t")) == "2" or str(row.get("k")) == "2" else "equity"
            if str(row.get("t")) == "3":
                kind = "future"
            native = amount(row["s"]) if row.get("s") is not None else None
            price = amount(row["mkt_price"]) if row.get("mkt_price") is not None else None
            result.append(Position(handle.account.key, identifier,
                                   str(row.get("name") or row.get("name2") or identifier),
                                   kind, qty, ccy, price, broker_value_native=native))
        for row in block.get("acc") or []:
            if not isinstance(row, dict):
                continue
            ccy = currency_code(str(row.get("curr") or ""))
            if ccy:
                value = amount(row.get("s") or 0)
                result.append(Position(handle.account.key, f"CASH.{ccy}", f"Cash {ccy}",
                                       "cash", value, ccy, Decimal("1")))
        return result

    async def _report(self, start: datetime, end: datetime) -> dict[str, Any]:
        def get() -> dict[str, Any]:
            response = self._client().authorized_request("getBrokerReport", {
                "date_start": str(start.date()), "date_end": str(end.date()),
                "time_period": "23:59:59", "format": "json",
            })
            if not isinstance(response, dict) or response.get("errMsg"):
                raise RuntimeError("Freedom getBrokerReport failed")
            return response.get("report", response)
        return await asyncio.to_thread(get)

    async def operations(self, handle: Handle, start: datetime, end: datetime) -> tuple[list[BrokerEvent], bool]:
        report = await self._report(start, end)
        rows = report.get("cash_in_outs")
        if not isinstance(rows, list):
            raise ValueError("Freedom broker report lacks cash_in_outs")
        result: list[BrokerEvent] = []
        result.append(BrokerEvent(
            f"report:{start.date().isoformat()}:{end.date().isoformat()}", end,
            "raw_report", "", Decimal("0"), "RUB", "freedom_broker_report", report,
        ))
        reviewed = True
        for row in rows:
            if not isinstance(row, dict):
                continue
            typ = str(row.get("type") or "").lower()
            value = amount(row.get("amount") or 0)
            ccy = currency_code(str(row.get("currency") or ""))
            when = row.get("datetime") or row.get("pay_d") or row.get("date_created")
            if not ccy or not when:
                reviewed = False
                continue
            if typ == "bank":
                if value == 0:
                    kind = "unresolved"
                else:
                    kind = "external_in" if value > 0 else "external_out"
            elif typ in {"coupon", "coupon_reverted"}:
                kind = "coupon"
            elif typ == "maturity":
                kind = "other"
            elif typ == "intercompany":
                kind = "unresolved"
            elif typ in {"dividend", "dividends"}:
                kind = "dividend"
            else:
                kind = "unresolved"
            result.append(BrokerEvent(
                _event_id(row), _aware(when, naive_zone="Europe/Moscow"), kind,
                str(row.get("ticker") or ""), value, ccy,
                "freedom_broker_report", row,
            ))
        return result, reviewed

    async def forecasts(self, handle: Handle, positions: list[Position],
                        now: datetime, horizon: datetime) -> tuple[list[IncomeEvent], list[str]]:
        result: list[IncomeEvent] = []
        errors: list[str] = []
        for position in positions:
            if position.kind == "equity":
                errors.append(f"dividend_calendar_unavailable:{position.instrument_id}")
                continue
            if position.kind != "bond":
                continue
            try:
                data = await asyncio.to_thread(self._client().symbol, position.instrument_id, lang="en")
                quotes = data.get("quotes") if isinstance(data.get("quotes"), dict) else {}
                next_day = quotes.get("couponnext_d") or quotes.get("coupon_d")
                period = int(amount(quotes.get("couponperiod") or 0))
                annual = amount(quotes.get("couponvalue_a") or 0)
                ccy = currency_code(str(quotes.get("curr_c") or data.get("face_curr_c") or
                                        position.price_currency))
                maturity = data.get("maturity_d") or quotes.get("maturity_d")
                if not next_day or not maturity or period <= 0:
                    errors.append(f"calendar_missing:{position.instrument_id}")
                    continue
                payment = _aware(str(next_day)[:19], naive_zone="Europe/Moscow")
                last = min(_aware(str(maturity)[:19], naive_zone="Europe/Moscow"), horizon)
                count = 0
                while payment <= last and count < 30:
                    count += 1
                    if payment >= now:
                        per_unit = annual * Decimal(period) / Decimal("365") if annual > 0 else None
                        result.append(IncomeEvent(
                            handle.account.key, position.instrument_id, "coupon", payment,
                            per_unit * position.quantity_units if per_unit is not None else None,
                            ccy, "estimate", "freedom_stock_data",
                            f"bond:{position.instrument_id}:{payment.isoformat()}",
                            notes="Оценка из годового купона и периода; условия могут измениться.",
                        ))
                    payment += timedelta(days=period)
            except Exception as exc:
                errors.append(f"calendar:{position.instrument_id}:{type(exc).__name__}")
        return result, errors
