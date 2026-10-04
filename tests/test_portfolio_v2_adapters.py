"""Broker mapping regression tests with anonymized payloads."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.portfolio_v2.adapters import (
    FreedomAdapter, Handle, TInvestAdapter, _t_invest_coupon_forecasts, t_operation,
)
from src.portfolio_v2.domain import Account, Position
from src.portfolio_v2.storage import _decoded


NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


def test_t_invest_operation_types_and_raw() -> None:
    def item(kind: str, units: int):
        return SimpleNamespace(
            id=kind, type=SimpleNamespace(name=f"OPERATION_TYPE_{kind}"),
            payment=SimpleNamespace(units=units, nano=0, currency="rub"),
            date=NOW, instrument_uid="",
        )

    incoming = t_operation(item("INPUT", 100))
    outgoing = t_operation(item("OUTPUT", 30))
    coupon = t_operation(item("COUPON", 12))
    assert incoming and incoming.kind == "external_in" and incoming.amount_native == 100
    assert outgoing and outgoing.kind == "external_out" and outgoing.amount_native == -30
    assert coupon and coupon.kind == "coupon"


def test_t_invest_floating_forecast_carries_last_known_coupon() -> None:
    position = Position("tinkoff:A", "UID", "Floating bond", "bond",
                        Decimal("10"), "RUB", Decimal("100"),
                        floating_coupon_flag=True)
    events = [
        SimpleNamespace(
            coupon_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
            coupon_type="FLOATING",
            pay_one_bond=SimpleNamespace(units=12, nano=500000000, currency="rub"),
        ),
        SimpleNamespace(
            coupon_date=datetime(2026, 10, 10, tzinfo=timezone.utc),
            coupon_type="FLOATING",
            pay_one_bond=SimpleNamespace(units=0, nano=0, currency="rub"),
        ),
    ]

    forecasts = _t_invest_coupon_forecasts(
        position, events, datetime(2026, 10, 4, tzinfo=timezone.utc),
        datetime(2026, 12, 31, tzinfo=timezone.utc),
    )

    assert len(forecasts) == 1
    assert forecasts[0].amount_native == Decimal("125")
    assert forecasts[0].status == "estimate"
    assert "последний известный купон" in forecasts[0].notes


def test_t_invest_quantity_is_units_not_lots(monkeypatch: pytest.MonkeyPatch) -> None:
    quote = lambda units, currency="rub": SimpleNamespace(units=units, nano=0, currency=currency)
    position = SimpleNamespace(
        instrument_type="share", instrument_uid="UID", figi="FIGI", ticker="TEST",
        quantity=quote(10), quantity_lots=quote(100), current_price=quote(5),
        current_nkd=None,
    )

    class Client:
        def __init__(self, *_args):
            self.operations = SimpleNamespace(
                get_portfolio=self.get_portfolio, get_positions=self.get_positions,
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get_portfolio(self, **_kwargs):
            return SimpleNamespace(positions=[position])

        async def get_positions(self, **_kwargs):
            return SimpleNamespace(money=[quote(20)])

    monkeypatch.setattr("src.portfolio_v2.adapters.AsyncClient", Client)
    adapter = TInvestAdapter(["token"], ["name"], {})
    adapter._instrument_names["UID"] = "Test"
    rows = asyncio.run(adapter.positions(Handle(Account("tinkoff", "A", "owner"), token="token")))
    assert rows[0].quantity_units == 10
    assert rows[0].value_native == 50
    assert rows[1].instrument_id == "CASH.RUB"
    assert rows[1].value_native == 20


def test_t_invest_digital_bond_survives_cash_balance_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quote = lambda units, currency="rub": SimpleNamespace(units=units, nano=0, currency=currency)
    position = SimpleNamespace(
        instrument_type="debt_dfa", instrument_uid="fd6da192-a299-43dc-a6c6-ba99be6079d2",
        figi="", ticker="NDM_BNFR-PP-FIXPRCNT-03.27", quantity=quote(283),
        quantity_lots=quote(283), current_price=quote(1008), current_nkd=None,
    )

    class Client:
        def __init__(self, *_args):
            self.operations = SimpleNamespace(
                get_portfolio=self.get_portfolio, get_positions=self.get_positions,
            )
            self.instruments = SimpleNamespace(
                bond_by=self.bond_by, get_instrument_by=self.get_instrument_by,
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get_portfolio(self, **_kwargs):
            return SimpleNamespace(positions=[position])

        async def get_positions(self, **_kwargs):
            raise RuntimeError("cash balances unavailable")

        async def bond_by(self, **_kwargs):
            raise RuntimeError("DFA is not a regular bond instrument")

        async def get_instrument_by(self, **_kwargs):
            raise RuntimeError("instrument details unavailable")

    monkeypatch.setattr("src.portfolio_v2.adapters.AsyncClient", Client)
    adapter = TInvestAdapter(["token"], ["name"], {})
    rows = asyncio.run(adapter.positions(
        Handle(Account("tinkoff", "A", "owner"), token="token")
    ))

    assert len(rows) == 1
    assert rows[0].instrument_id == "fd6da192-a299-43dc-a6c6-ba99be6079d2"
    assert rows[0].kind == "bond"
    assert rows[0].quantity_units == Decimal("283")
    assert rows[0].value_native == Decimal("285264")
    assert adapter.position_warnings == {"tinkoff:A": "RuntimeError"}


def test_freedom_flow_conservative_classification(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = FreedomAdapter("public", "private", "freedom24.com", "label", "owner")
    account = Handle(Account("freedom", "F", "owner"))
    rows = [
        {"id": "1", "type": "bank", "amount": "100", "currency": "USD", "datetime": "2026-10-02"},
        {"id": "2", "type": "intercompany", "amount": "-20", "currency": "USD", "datetime": "2026-10-02"},
        {"id": "3", "type": "coupon", "amount": "2", "currency": "USD", "datetime": "2026-10-02"},
        {"id": "4", "type": "maturity", "amount": "100", "currency": "USD", "datetime": "2026-10-02"},
    ]

    async def report(*_args: object):
        return {"cash_in_outs": rows, "trades": {"detailed": [{"id": "T1"}]}}

    monkeypatch.setattr(adapter, "_report", report)
    events, verified = asyncio.run(adapter.operations(account, NOW, NOW))
    assert verified
    assert {event.event_id: event.kind for event in events} == {
        "report:2026-10-03:2026-10-03": "raw_report",
        "1": "external_in", "2": "unresolved", "3": "coupon", "4": "other",
    }
    assert events[0].raw["trades"]["detailed"][0]["id"] == "T1"


def test_freedom_positions_keep_native_currency() -> None:
    adapter = FreedomAdapter("public", "private", "freedom24.com", "label", "owner")
    handle = Handle(Account("freedom", "F", "owner"), payload={
        "pos": [{"i": "BOND", "q": 2, "s": 205, "mkt_price": 100,
                 "curr": "USD", "t": 2}],
        "acc": [{"curr": "EUR", "s": 30}],
    })
    rows = asyncio.run(adapter.positions(handle))
    assert len(rows) == 2
    assert rows[0].value_native == Decimal("205")
    assert rows[0].price_currency == "USD"
    assert rows[1].value_native == Decimal("30")
    assert rows[1].price_currency == "EUR"


def test_jsonb_uses_whole_array_not_characters() -> None:
    assert _decoded('["failed_account"]') == ["failed_account"]
