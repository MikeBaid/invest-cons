from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from src.portfolio_v2.allocation import build_portfolio_allocation
from src.portfolio_v2.domain import Account, Position, Snapshot, ValuedPosition


ROOT = Path(__file__).resolve().parents[1]
IPS = ROOT / "config/ips/personal_main.draft.yaml"


def test_allocation_rolls_up_rub_usd_and_draft_targets() -> None:
    accounts = (
        Account("tinkoff", "A", "owner", label="Main"),
        Account("freedom", "F", "owner", label="Freedom"),
    )
    positions = (
        ValuedPosition(Position("tinkoff:A", "UID-RU", "Bond RU", "bond",
                                Decimal("1"), "RUB", Decimal("60")),
                       Decimal("60"), Decimal("10")),
        ValuedPosition(Position("freedom:F", "UID-KZ", "Bond KZ", "bond",
                                Decimal("1"), "USD", Decimal("5")),
                       Decimal("40"), Decimal("5")),
    )
    snapshot = Snapshot(datetime(2026, 10, 4, tzinfo=timezone.utc), accounts, positions)
    assignments = {
        ("rf", "UID-RU"): {"bucket_id": "rf.bonds.usd_linked"},
        ("external", "UID-KZ"): {"bucket_id": "external.bonds.kz_fx"},
    }

    report = build_portfolio_allocation(snapshot, assignments, IPS)
    nodes = {}

    def index(node):
        nodes[node.bucket_id] = node
        for child in node.children:
            index(child)

    index(report.root)
    assert report.complete
    assert report.total_rub == Decimal("100")
    assert report.total_usd == Decimal("15")
    assert nodes["rf"].value_rub == Decimal("60")
    assert nodes["rf"].weight_rub_pct == Decimal("0.6")
    assert nodes["external.bonds"].value_usd == Decimal("5")
    assert nodes["external.bonds.kz_fx"].target_pct == Decimal("0.04")
    assert nodes["external.bonds.kz_fx"].target_rub == Decimal("4.00")
    assert nodes["external.bonds.kz_fx"].target_usd == Decimal("0.60")


def test_unmapped_position_is_visible_under_synthetic_bucket() -> None:
    account = Account("tinkoff", "A", "owner")
    valued = ValuedPosition(
        Position("tinkoff:A", "UNKNOWN", "Unmapped bond", "bond", Decimal("2"),
                 "RUB", Decimal("25")),
        Decimal("50"), Decimal("5"),
    )
    snapshot = Snapshot(datetime(2026, 10, 4, tzinfo=timezone.utc), (account,), (valued,))

    report = build_portfolio_allocation(snapshot, {}, IPS)
    rf = next(node for node in report.root.children if node.bucket_id == "rf")
    unclassified = next(node for node in rf.children if node.bucket_id == "rf.unclassified")
    assert report.complete
    assert unclassified.value_rub == Decimal("50")
    assert len(unclassified.positions) == 1
    assert unclassified.target_pct is None


def test_incomplete_snapshot_shows_weights_and_targets_against_observed_nav() -> None:
    account = Account("tinkoff", "A", "owner")
    valued = ValuedPosition(
        Position("tinkoff:A", "UID", "Bond", "bond", Decimal("1"), "RUB", Decimal("100")),
        Decimal("100"), Decimal("10"),
    )
    snapshot = Snapshot(datetime(2026, 10, 4, tzinfo=timezone.utc), (account,), (valued,),
                        account_errors=("tinkoff:A:cash_balances:AioRequestError",))

    report = build_portfolio_allocation(
        snapshot, {("rf", "UID"): {"bucket_id": "rf.bonds.rub_fixed"}}, IPS
    )
    rf = next(node for node in report.root.children if node.bucket_id == "rf")
    assert not report.complete
    assert report.total_rub == Decimal("100")
    assert report.total_usd == Decimal("10")
    assert rf.value_rub == Decimal("100")
    assert rf.weight_rub_pct == Decimal("1")
    assert rf.target_rub == Decimal("60.00")
