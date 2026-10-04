"""Income aggregation from persisted broker events into the configured IPS tree."""

from __future__ import annotations

import calendar
import json
from dataclasses import dataclass, field
from datetime import datetime, time, timezone, tzinfo
from decimal import Decimal
from pathlib import Path
from typing import Any

from .allocation import AllocationNode, load_allocation_tree
from .domain import FxFixing, Snapshot


ZERO = Decimal("0")
METRICS = ("actual_ytd", "forecast_year", "actual_mtd", "forecast_month")


@dataclass(slots=True)
class IncomeMoney:
    rub: Decimal = ZERO
    usd: Decimal = ZERO

    def add(self, rub: Decimal | None, usd: Decimal | None) -> None:
        if rub is not None:
            self.rub += rub
        if usd is not None:
            self.usd += usd


@dataclass(slots=True)
class IncomePosition:
    account_key: str
    account_label: str
    instrument_id: str
    name: str
    totals: dict[str, IncomeMoney] = field(
        default_factory=lambda: {metric: IncomeMoney() for metric in METRICS}
    )


@dataclass(slots=True)
class IncomeNode:
    bucket_id: str
    label: str
    totals: dict[str, IncomeMoney] = field(
        default_factory=lambda: {metric: IncomeMoney() for metric in METRICS}
    )
    children: list["IncomeNode"] = field(default_factory=list)
    positions: list[IncomePosition] = field(default_factory=list)
    synthetic: bool = False


@dataclass(slots=True)
class IncomeReport:
    as_of: datetime
    snapshot_as_of: datetime | None
    root: IncomeNode
    actual_coverage_complete: bool
    notes: tuple[str, ...]


def _copy_tree(source: AllocationNode) -> IncomeNode:
    return IncomeNode(
        bucket_id=source.bucket_id,
        label=source.label,
        children=[_copy_tree(child) for child in source.children],
    )


def _periods(now: datetime, calendar_tz: tzinfo) -> dict[str, tuple[datetime, datetime]]:
    local_now = now.astimezone(calendar_tz)
    year_start = datetime.combine(datetime(local_now.year, 1, 1).date(), time.min, calendar_tz)
    year_end = datetime.combine(datetime(local_now.year, 12, 31).date(), time.max, calendar_tz)
    month_start = datetime(local_now.year, local_now.month, 1, tzinfo=calendar_tz)
    month_end = datetime(
        local_now.year, local_now.month,
        calendar.monthrange(local_now.year, local_now.month)[1],
        23, 59, 59, 999999, tzinfo=calendar_tz,
    )
    return {
        "actual_ytd": (year_start, local_now),
        "forecast_year": (local_now, year_end),
        "actual_mtd": (month_start, local_now),
        "forecast_month": (local_now, month_end),
    }


def build_income_report(
    *,
    snapshot: Snapshot | None,
    assignments: dict[tuple[str, str], dict[str, str]],
    ips_path: Path,
    actual_events: list[dict[str, Any]],
    forecast_events: list[dict[str, Any]],
    fixing: FxFixing,
    now: datetime,
    calendar_tz: tzinfo,
    actual_coverage_complete: bool,
    forecast_errors: dict[str, list[str]],
    actual_coverage_accounts: tuple[int, int] | None = None,
    excluded_instrument_ids: frozenset[str] = frozenset(),
) -> IncomeReport:
    ips_tree, _status, _version = load_allocation_tree(ips_path)
    root = _copy_tree(ips_tree)
    nodes: dict[str, IncomeNode] = {}
    parents: dict[str, IncomeNode] = {}

    def index(node: IncomeNode, parent: IncomeNode | None = None) -> None:
        nodes[node.bucket_id] = node
        if parent is not None:
            parents[node.bucket_id] = parent
        for child in node.children:
            index(child, node)

    index(root)
    account_contours = {account.key: account.contour for account in snapshot.accounts} if snapshot else {}
    account_labels = {
        account.key: account.label or account.account_id
        for account in snapshot.accounts
    } if snapshot else {}
    position_names = {
        (valued.position.account_key, valued.position.instrument_id): valued.position.name
        for valued in snapshot.positions
    } if snapshot else {}
    for row in actual_events:
        raw = row.get("raw")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = None
        if isinstance(raw, dict):
            name = str(raw.get("name") or raw.get("ticker") or "").strip()
            key = (str(row.get("account_key") or ""),
                   str(row.get("instrument_id") or ""))
            if name and key[1] and key not in position_names:
                position_names[key] = name
    unclassified: dict[str, IncomeNode] = {}
    income_positions: dict[tuple[str, str, str], IncomePosition] = {}
    unclassified_instruments: set[tuple[str, str]] = set()
    notes: list[str] = []
    estimated_forecast_ids: set[str] = set()
    if snapshot is None:
        notes.append("Нет сохраненного снимка портфеля; привязка событий к счетам может быть неполной.")
    elif not snapshot.complete:
        cash_errors = sum(":cash_balances:" in error for error in snapshot.account_errors)
        other_account_errors = len(snapshot.account_errors) - cash_errors
        details = []
        if cash_errors:
            details.append(f"не получены дополнительные денежные остатки по счетам: {cash_errors}")
        if other_account_errors:
            details.append(f"ошибки загрузки счетов: {other_account_errors}")
        if snapshot.valuation_errors:
            details.append(f"ошибки оценки позиций: {len(snapshot.valuation_errors)}")
        notes.append(
            "Снимок портфеля неполный (" + "; ".join(details or ["причина не классифицирована"])
            + "); NAV может быть неполным."
        )
    if not actual_coverage_complete:
        if actual_coverage_accounts:
            confirmed, total = actual_coverage_accounts
            notes.append(
                f"Годовой охват операций подтвержден для {confirmed} из {total} счетов; "
                "фактическая сумма может быть неполной."
            )
        else:
            notes.append("Охват операций с начала года не подтвержден; фактическая сумма может быть неполной.")

    forecast_codes: dict[str, int] = {}
    for errors in forecast_errors.values():
        for error in errors:
            code = error.split(":", 1)[0]
            forecast_codes[code] = forecast_codes.get(code, 0) + 1
    if forecast_codes.get("dividend_calendar_unavailable"):
        notes.append(
            f"По {forecast_codes['dividend_calendar_unavailable']} позициям Freedom "
            "прогноз дивидендов пока не формируется: календарь в адаптере не реализован."
        )
    if forecast_codes.get("dividend_calendar_empty"):
        notes.append(
            f"По {forecast_codes['dividend_calendar_empty']} запросам календарь T-Invest "
            "вернул пустой список дивидендов; это не доказывает, что будущих выплат не будет."
        )
    calendar_failures = sum(count for code, count in forecast_codes.items()
                            if code in {"calendar", "calendar_missing", "dividend_date_missing"})
    if calendar_failures:
        notes.append(
            f"Проблемных ответов календарей: {calendar_failures}; "
            "прогноз по этим запросам может быть неполным."
        )
    other_forecast_issues = sum(count for code, count in forecast_codes.items() if code not in {
        "dividend_calendar_unavailable", "dividend_calendar_empty", "calendar",
        "calendar_missing", "dividend_date_missing",
    })
    if other_forecast_issues:
        notes.append(f"Прочие проблемы данных прогноза: {other_forecast_issues}.")

    periods = _periods(now, calendar_tz)
    missing_fx: set[str] = set()
    missing_forecast_amounts = 0

    def get_target(account_key: str, instrument_id: str) -> IncomeNode | None:
        contour = account_contours.get(account_key)
        if contour is None:
            notes.append(f"Счет события не найден в последнем снимке: {account_key}.")
            return None
        assignment = assignments.get((contour, instrument_id))
        bucket_id = assignment.get("bucket_id") if assignment else None
        if bucket_id and ".other" in bucket_id:
            return None
        target = nodes.get(bucket_id or "")
        if target is not None and not target.children and ".other" not in target.bucket_id:
            return target
        target = unclassified.get(contour)
        if target is None:
            contour_node = nodes.get(contour)
            if contour_node is None:
                notes.append(f"В IPS нет контура для счета: {contour}.")
                return None
            target = IncomeNode(f"{contour}.unclassified", "Без классификации", synthetic=True)
            contour_node.children.append(target)
            nodes[target.bucket_id] = target
            parents[target.bucket_id] = contour_node
            unclassified[contour] = target
        unclassified_instruments.add((contour, instrument_id))
        return target

    def add_event(
        row: dict[str, Any], metric: str, *, forecast: bool,
    ) -> None:
        nonlocal missing_forecast_amounts
        instrument_id = str(row.get("instrument_id") or "")
        if instrument_id in excluded_instrument_ids:
            return
        target = get_target(str(row.get("account_key") or ""), instrument_id)
        if target is None:
            return
        amount_rub = row.get("amount_rub")
        amount_usd = row.get("amount_usd")
        if forecast:
            native = row.get("amount_native")
            if native is None:
                missing_forecast_amounts += 1
                return
            amount_rub = fixing.convert(native, str(row.get("currency") or ""), "RUB")
            amount_usd = fixing.convert(native, str(row.get("currency") or ""), "USD")
        if amount_rub is None:
            missing_fx.add("RUB")
        if amount_usd is None:
            missing_fx.add("USD")

        account_key = str(row.get("account_key") or "")
        position_key = (target.bucket_id, account_key, instrument_id)
        position = income_positions.get(position_key)
        if position is None:
            position = IncomePosition(
                account_key=account_key,
                account_label=account_labels.get(account_key, account_key),
                instrument_id=instrument_id,
                name=position_names.get((account_key, instrument_id), instrument_id or "Без ID"),
            )
            income_positions[position_key] = position
            target.positions.append(position)
        position.totals[metric].add(amount_rub, amount_usd)

        current: IncomeNode | None = target
        while current is not None:
            current.totals[metric].add(amount_rub, amount_usd)
            current = parents.get(current.bucket_id)

    for row in actual_events:
        when = row.get("occurred_at")
        if when is None:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        for metric in ("actual_ytd", "actual_mtd"):
            start, end = periods[metric]
            if start <= when <= end:
                add_event(row, metric, forecast=False)

    for row in forecast_events:
        when = row.get("payment_at")
        if when is None:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        for metric in ("forecast_year", "forecast_month"):
            start, end = periods[metric]
            if start <= when <= end:
                if row.get("status") == "estimate":
                    estimated_forecast_ids.add(str(row.get("event_id") or when.isoformat()))
                add_event(row, metric, forecast=True)

    if unclassified_instruments:
        notes.append(
            f"Выплаты по {len(unclassified_instruments)} инструментам без активной "
            "классификации IPS помещены в раздел «Без классификации»; их можно раскрыть в отчете."
        )
    if estimated_forecast_ids:
        notes.append(
            f"В прогнозе есть расчетные события (estimate): {len(estimated_forecast_ids)}."
        )
    if missing_forecast_amounts:
        notes.append(
            f"Для {missing_forecast_amounts} прогнозных событий не удалось рассчитать сумму; "
            "они не включены в итоги."
        )
    if missing_fx:
        notes.append("Часть выплат не переведена в " + "/".join(sorted(missing_fx)) + "; суммы в этой валюте неполны.")
    for node in nodes.values():
        node.positions.sort(key=lambda item: (item.name.casefold(), item.account_label.casefold()))
    return IncomeReport(
        as_of=now,
        snapshot_as_of=snapshot.as_of if snapshot else None,
        root=root,
        actual_coverage_complete=actual_coverage_complete,
        notes=tuple(dict.fromkeys(notes)),
    )
