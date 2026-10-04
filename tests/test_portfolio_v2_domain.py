"""Financial invariants for the new core, with no broker or database calls."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.portfolio_v2.domain import (
    Account,
    CashFlow,
    FxFixing,
    Position,
    calculate_performance,
    value_portfolio,
)
from src.portfolio_v2.fx import parse_cbr_daily


NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)
ACCOUNT = Account("tinkoff", "A", "owner")
FX = FxFixing(NOW, "test", {"RUB": Decimal("1"), "USD": Decimal("80")})


def _snapshot(value: str, when: datetime):
    return value_portfolio(
        as_of=when,
        accounts=[ACCOUNT],
        positions=[Position(ACCOUNT.key, "CASH.RUB", "Cash", "cash",
                            Decimal(value), "RUB", Decimal("1"))],
        fx=FX,
    )


def test_value_units_and_bond_accrued_interest() -> None:
    positions = [
        Position(ACCOUNT.key, "S", "Share", "equity", Decimal("100"), "RUB", Decimal("10")),
        Position(ACCOUNT.key, "B", "Bond", "bond", Decimal("2"), "RUB", Decimal("950"), Decimal("50")),
    ]
    snapshot = value_portfolio(as_of=NOW, accounts=[ACCOUNT], positions=positions, fx=FX)
    assert snapshot.complete
    assert snapshot.total("RUB") == Decimal("3000")
    assert snapshot.total("USD") == Decimal("37.5")


def test_missing_fx_makes_nav_unavailable() -> None:
    position = Position(ACCOUNT.key, "CNY", "Yuan", "cash", Decimal("100"), "CNY", Decimal("1"))
    snapshot = value_portfolio(as_of=NOW, accounts=[ACCOUNT], positions=[position], fx=FX)
    assert not snapshot.complete
    assert snapshot.total("RUB") is None
    assert snapshot.total("USD") is None
    assert "missing_fx" in snapshot.valuation_errors[0]


def test_bond_accrued_interest_in_other_currency() -> None:
    position = Position(ACCOUNT.key, "B", "FX bond", "bond", Decimal("2"),
                        "USD", Decimal("100"), Decimal("50"), accrued_currency="RUB")
    snapshot = value_portfolio(as_of=NOW, accounts=[ACCOUNT], positions=[position], fx=FX)
    assert snapshot.complete
    assert snapshot.positions[0].position.value_native is None
    assert snapshot.total("RUB") == Decimal("16100")
    assert snapshot.total("USD") == Decimal("201.25")


def test_partial_account_does_not_become_profit_or_loss() -> None:
    start = _snapshot("100", NOW)
    partial = value_portfolio(as_of=NOW + timedelta(days=1), accounts=[ACCOUNT],
                              positions=[], fx=FX, account_errors=["get_portfolio_failed:A"])
    assert calculate_performance(start, partial, [], currency="RUB", operations_complete=True) is None


def test_flow_timing_uses_modified_dietz() -> None:
    end = NOW + timedelta(days=100)
    flow = CashFlow(ACCOUNT.key, NOW + timedelta(days=1), Decimal("100"), "RUB",
                    Decimal("100"), Decimal("1.25"))
    result = calculate_performance(_snapshot("100", NOW), _snapshot("220", end), [flow],
                                   currency="RUB", operations_complete=True)
    assert result is not None
    assert result.pnl == Decimal("20")
    assert 10 < result.modified_dietz_pct < 11
    assert result.xirr_pct is not None


def test_missing_flow_fx_blocks_usd_result() -> None:
    flow = CashFlow(ACCOUNT.key, NOW + timedelta(days=1), Decimal("1"), "CNY", None, None)
    assert calculate_performance(_snapshot("100", NOW), _snapshot("110", NOW + timedelta(days=2)),
                                 [flow], currency="USD", operations_complete=True) is None


def test_cbr_nominal_and_date() -> None:
    xml = (b'<ValCurs Date="03.10.2026">'
           b'<Valute><CharCode>USD</CharCode><Nominal>1</Nominal><Value>80,50</Value></Valute>'
           b'<Valute><CharCode>CNY</CharCode><Nominal>10</Nominal><Value>110,00</Value></Valute>'
           b'</ValCurs>')
    fixing = parse_cbr_daily(xml, NOW.date())
    assert fixing.rub_per_currency["CNY"] == Decimal("11")
    assert fixing.convert(Decimal("10"), "CNY", "USD") == Decimal("110") / Decimal("80.50")
