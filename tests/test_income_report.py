from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from src.portfolio_v2.domain import Account, FxFixing, Position, Snapshot, ValuedPosition
from src.portfolio_v2.income import build_income_report


ROOT = Path(__file__).resolve().parents[1]
IPS = ROOT / "config/ips/personal_main.draft.yaml"
MOSCOW = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 4, 9, 0, tzinfo=MOSCOW)
ACCOUNT = Account("tinkoff", "A", "owner")
FIXING = FxFixing(NOW, "test", {"RUB": Decimal("1"), "USD": Decimal("80")})
UID = "instrument-1"


def _node(report, bucket_id):
    pending = [report.root]
    while pending:
        node = pending.pop()
        if node.bucket_id == bucket_id:
            return node
        pending.extend(node.children)
    raise AssertionError(f"Missing income node: {bucket_id}")


def test_income_report_aggregates_actual_and_forecast_by_ips_and_period() -> None:
    position = ValuedPosition(
        Position(ACCOUNT.key, UID, "Test bond", "bond", Decimal("1"), "RUB", Decimal("100")),
        Decimal("100"), Decimal("1.25"),
    )
    snapshot = Snapshot(NOW, (ACCOUNT,), (position,))
    assignments = {("rf", UID): {"bucket_id": "rf.bonds.rub_fixed"}}
    actual_events = [
        {"account_key": ACCOUNT.key, "instrument_id": UID,
         "occurred_at": datetime(2026, 5, 15, tzinfo=timezone.utc),
         "amount_rub": Decimal("100"), "amount_usd": Decimal("1.25")},
        {"account_key": ACCOUNT.key, "instrument_id": UID,
         "occurred_at": datetime(2026, 10, 2, tzinfo=timezone.utc),
         "amount_rub": Decimal("40"), "amount_usd": Decimal("0.5")},
    ]
    forecast_events = [
        {"account_key": ACCOUNT.key, "instrument_id": UID,
         "payment_at": datetime(2026, 10, 6, tzinfo=timezone.utc),
         "amount_native": Decimal("10"), "currency": "USD"},
        {"account_key": ACCOUNT.key, "instrument_id": UID,
         "payment_at": datetime(2026, 12, 15, tzinfo=timezone.utc),
         "amount_native": Decimal("5"), "currency": "USD"},
    ]

    report = build_income_report(
        snapshot=snapshot, assignments=assignments, ips_path=IPS,
        actual_events=actual_events, forecast_events=forecast_events,
        fixing=FIXING, now=NOW, calendar_tz=MOSCOW,
        actual_coverage_complete=True, forecast_errors={},
    )

    root = report.root.totals
    fixed = _node(report, "rf.bonds.rub_fixed").totals
    assert root["actual_ytd"].rub == Decimal("140")
    assert root["actual_mtd"].rub == Decimal("40")
    assert root["forecast_year"].rub == Decimal("1200")
    assert root["forecast_month"].usd == Decimal("10")
    assert fixed["actual_ytd"].usd == Decimal("1.75")
    assert fixed["forecast_year"].usd == Decimal("15")
    assert report.actual_coverage_complete
    assert len(_node(report, "rf.bonds.rub_fixed").positions) == 1
    assert _node(report, "rf.bonds.rub_fixed").positions[0].name == "Test bond"
    assert _node(report, "rf.bonds.rub_fixed").positions[0].totals["actual_ytd"].rub == Decimal("140")


def test_unclassified_income_is_kept_in_synthetic_ips_bucket() -> None:
    snapshot = Snapshot(NOW, (ACCOUNT,), ())
    event = {"account_key": ACCOUNT.key, "instrument_id": "unknown-uid",
             "occurred_at": datetime(2026, 10, 2, tzinfo=timezone.utc),
             "amount_rub": Decimal("25"), "amount_usd": Decimal("0.3"),
             "raw": {"name": "Historical fund"}}

    report = build_income_report(
        snapshot=snapshot, assignments={}, ips_path=IPS,
        actual_events=[event], forecast_events=[], fixing=FIXING,
        now=NOW, calendar_tz=MOSCOW, actual_coverage_complete=False,
        forecast_errors={},
    )

    unclassified = _node(report, "rf.unclassified")
    assert unclassified.synthetic
    assert unclassified.totals["actual_ytd"].rub == Decimal("25")
    assert unclassified.positions[0].name == "Historical fund"
    assert any("без активной классификации" in note.lower() for note in report.notes)


def test_income_notes_explain_coverage_and_forecast_gaps_without_identifiers() -> None:
    snapshot = Snapshot(
        NOW, (ACCOUNT,), (), account_errors=("tinkoff:private-account:cash_balances:AioRequestError",),
    )
    report = build_income_report(
        snapshot=snapshot, assignments={}, ips_path=IPS,
        actual_events=[], forecast_events=[], fixing=FIXING,
        now=NOW, calendar_tz=MOSCOW, actual_coverage_complete=False,
        actual_coverage_accounts=(5, 12),
        forecast_errors={"tinkoff:private-account": [
            "dividend_calendar_empty:private-uid", "calendar:private-uid:AioRequestError",
        ]},
    )
    notes = " ".join(report.notes)
    assert "не получены дополнительные денежные остатки по счетам: 1" in notes
    assert "5 из 12 счетов" in notes
    assert "пустой список дивидендов" in notes
    assert "Проблемных ответов календарей: 1" in notes
    assert "private-account" not in notes
    assert "private-uid" not in notes
