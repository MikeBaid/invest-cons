from datetime import datetime, timezone
from decimal import Decimal

from src.portfolio_v2.allocation import AllocationNode, PortfolioAllocation, PositionRow
from src.portfolio_v2.bot import PortfolioBot
from src.portfolio_v2.allocation import build_portfolio_allocation, load_allocation_tree
from src.portfolio_v2.bot import ClassificationSession
from src.portfolio_v2.domain import Snapshot
from pathlib import Path
from src.portfolio_v2.domain import Account, Position, ValuedPosition
from src.portfolio_v2.income import IncomeMoney, IncomeNode, IncomePosition, IncomeReport


def test_classification_follows_ips_tree_and_only_confirms_leaf() -> None:
    ips = Path(__file__).resolve().parents[1] / "config/ips/personal_main.draft.yaml"
    account = Account("tinkoff", "A", "owner", label="Main")
    valued = ValuedPosition(
        Position(account.key, "NEW-BOND", "New bond", "bond", Decimal("1"),
                 "RUB", Decimal("100")), Decimal("100"), Decimal("1"),
    )
    report = build_portfolio_allocation(
        Snapshot(datetime(2026, 10, 4, tzinfo=timezone.utc), (account,), (valued,)),
        {}, ips,
    )
    candidates = PortfolioBot._unclassified_candidates(report)
    assert len(candidates) == 1
    session = ClassificationSession(candidates)
    bot = object.__new__(PortfolioBot)
    bot.service = type("Service", (), {"classification_tree": lambda self: load_allocation_tree(ips)})()

    text, keyboard, contour = bot._classification_node(session, "test", 0, "-")
    assert contour.bucket_id == "rf"
    assert any(button.text == "bonds" or "bond" in button.text.lower()
               for row in keyboard.inline_keyboard for button in row)
    assert "Подтвердить" not in text

    _text, keyboard, parent = bot._classification_node(session, "test", 0, "0")
    assert parent.bucket_id == "rf.bonds"
    assert all(button.text != "Подтвердить"
               for row in keyboard.inline_keyboard for button in row)
    text, keyboard, leaf = bot._classification_node(session, "test", 0, "0.2")
    assert leaf.bucket_id == "rf.bonds.rub_fixed"
    assert "Подтвердить" in text
    assert keyboard.inline_keyboard[0][0].callback_data == "cl:test:c:0:0.2"
    assert all(len(button.callback_data) <= 64
               for row in keyboard.inline_keyboard for button in row)


def test_portfolio_keyboard_drills_to_position_and_back() -> None:
    account = Account("tinkoff", "A", "owner", label="Main")
    valued = ValuedPosition(
        Position("tinkoff:A", "UID", "Readable bond name", "bond", Decimal("1"),
                 "RUB", Decimal("100")),
        Decimal("100"), Decimal("10"),
    )
    leaf = AllocationNode("rf.bonds", "Bonds", Decimal("0.5"),
                          value_rub=Decimal("100"), value_usd=Decimal("10"),
                          positions=[PositionRow(valued, account)])
    root = AllocationNode("personal_main", "Portfolio", Decimal("1"), children=[leaf])
    report = PortfolioAllocation(datetime(2026, 10, 4, tzinfo=timezone.utc), True,
                                  "draft", Decimal("100"), Decimal("10"), root, ())

    top = PortfolioBot._allocation_keyboard(report, "abc123", root)
    assert top.inline_keyboard[0][0].callback_data == "pa:abc123:n:0"
    assert all(button.text != "Обновить данные"
               for row in top.inline_keyboard for button in row)
    assert top.inline_keyboard[-1][0].callback_data == "ir:open"
    detail = PortfolioBot._allocation_keyboard(report, "abc123", leaf, "0")
    assert detail.inline_keyboard[0][0].callback_data == "pa:abc123:p:0:0:0"
    assert all(button.text != "Обновить данные"
               for row in detail.inline_keyboard for button in row)
    assert all(len(button.callback_data) <= 64 for row in detail.inline_keyboard for button in row)
    assert PortfolioBot._position_text(report, leaf.positions[0]).startswith(
        "<b>Readable bond name</b>"
    )
    overview = PortfolioBot._overview_text(report)
    assert "RUB" in overview and "USD" in overview
    assert "Bonds" in overview


def test_incomplete_report_labels_observed_nav_and_keeps_targets() -> None:
    account = Account("tinkoff", "A", "owner", label="Main")
    valued = ValuedPosition(
        Position("tinkoff:A", "UID", "Readable bond name", "bond", Decimal("1"),
                 "RUB", Decimal("100")),
        Decimal("100"), Decimal("10"),
    )
    leaf = AllocationNode("rf.bonds", "Bonds", Decimal("0.43"),
                          value_rub=Decimal("100"), value_usd=Decimal("10"),
                          weight_rub_pct=Decimal("1"), weight_usd_pct=Decimal("1"),
                          target_rub=Decimal("43"), target_usd=Decimal("4.3"),
                          positions=[PositionRow(valued, account)])
    root = AllocationNode("personal_main", "Portfolio", Decimal("1"), children=[leaf])
    report = PortfolioAllocation(datetime(2026, 10, 4, tzinfo=timezone.utc), False,
                                 "draft", Decimal("100"), Decimal("10"), root, ())

    overview = PortfolioBot._overview_text(report)
    detail = PortfolioBot._node_text(report, leaf)
    position = PortfolioBot._position_text(report, leaf.positions[0])

    assert "Наблюдаемый NAV" in overview
    assert "Доли — от наблюдаемого NAV" in overview
    assert "целевой объём: 43.00 RUB / 4.30 USD" in detail
    assert "целевые суммы масштабированы от этого же NAV" in detail
    assert "наблюдаемого NAV: 100.00% / 100.00%" in position


def test_income_report_has_four_period_choices_and_ips_drilldown() -> None:
    leaf = IncomeNode("rf.bonds.rub_fixed", "RUB fixed coupon")
    leaf.totals["actual_ytd"] = IncomeMoney(Decimal("1200"), Decimal("15"))
    position = IncomePosition("tinkoff:A", "Main", "UID-1", "Readable coupon bond")
    position.totals["actual_ytd"] = IncomeMoney(Decimal("1200"), Decimal("15"))
    leaf.positions.append(position)
    root = IncomeNode("personal_main", "Portfolio", children=[leaf])
    root.totals["actual_ytd"].rub = Decimal("1200")
    root.totals["actual_ytd"].usd = Decimal("15")
    report = IncomeReport(datetime(2026, 10, 4, tzinfo=timezone.utc), None,
                          root, True, ())

    choices = PortfolioBot._income_home_keyboard("inc123")
    details = PortfolioBot._income_tree_keyboard("inc123", root, "actual_ytd")
    summary = PortfolioBot._income_summary_text(report)

    assert len(choices.inline_keyboard) == 6
    assert choices.inline_keyboard[0][0].callback_data == "ir:inc123:m:actual_ytd"
    assert details.inline_keyboard[0][0].callback_data == "ir:inc123:n:actual_ytd:0"
    assert "Факт с 01.01.2026" in summary
    assert "Прогноз до 31.12.2026" in summary
    leaf_keyboard = PortfolioBot._income_tree_keyboard(
        "inc123", leaf, "actual_ytd", "0",
    )
    assert leaf_keyboard.inline_keyboard[0][0].callback_data == "ir:inc123:i:actual_ytd:0:0"
    assert all(len(button.callback_data) <= 64
               for row in leaf_keyboard.inline_keyboard for button in row)
    node_text = PortfolioBot._income_node_text(report, leaf, "actual_ytd")
    position_text = PortfolioBot._income_position_text(report, position, "actual_ytd")
    assert "Доля в общей сумме доходов: RUB 100.00% / USD 100.00%" in node_text
    assert "Readable coupon bond" in position_text
    assert "Доля в общей сумме доходов: RUB 100.00% / USD 100.00%" in position_text
    assert "отдельный налоговый пересчет не выполняется" in summary
