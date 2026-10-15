"""Thin Telegram presentation for the first accounting release."""

from __future__ import annotations

import html
import logging
import secrets
import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .allocation import AllocationNode, PortfolioAllocation, PositionRow
from .domain import Snapshot
from .income import METRICS, IncomeNode, IncomePosition, IncomeReport
from .service import PortfolioService


logger = logging.getLogger(__name__)

INCOME_METRIC_LABELS = {
    "actual_ytd": "Факт с начала календарного года",
    "forecast_year": "Прогноз до конца календарного года",
    "actual_mtd": "Факт с начала календарного месяца",
    "forecast_month": "Прогноз до конца календарного месяца",
}


@dataclass(frozen=True, slots=True)
class ClassificationCandidate:
    contour: str
    instrument_id: str
    name: str
    kind: str
    account_label: str


@dataclass(slots=True)
class ClassificationSession:
    candidates: list[ClassificationCandidate]


def _money(value: Decimal | None, currency: str, *, blur: bool = False) -> str:
    if value is None:
        return "н/д"
    formatted = f"{value:,.2f} {currency}".replace(",", " ")
    return f"<tg-spoiler>{formatted}</tg-spoiler>" if blur else formatted


def _date(value: datetime | None) -> str:
    return value.strftime("%d.%m.%Y %H:%M %Z") if value else "н/д"


def _parts(lines: list[str], *, max_size: int = 3500) -> list[str]:
    parts: list[str] = []
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_size and current:
            parts.append(current)
            current = line
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def portfolio_lines(snapshot: Snapshot | None, *, blur_amounts: bool = False) -> list[str]:
    if snapshot is None:
        return ["Срез портфеля пока не загружен. Запустите /refresh."]
    lines = ["<b>Инвестиционный портфель</b>", f"На {_date(snapshot.as_of)}",
             f"Охват: {'полный' if snapshot.complete else 'неполный'}",
             f"Итого: {_money(snapshot.total('RUB'), 'RUB', blur=blur_amounts)} / "
             f"{_money(snapshot.total('USD'), 'USD', blur=blur_amounts)}"]
    if snapshot.account_errors or snapshot.valuation_errors:
        lines.append("<b>Проблемы данных</b>")
        lines.extend(f"• {html.escape(error)}" for error in
                     (*snapshot.account_errors, *snapshot.valuation_errors)[:20])
    for account in snapshot.accounts:
        own = [p for p in snapshot.positions if p.position.account_key == account.key]
        rub = sum((p.value_rub for p in own if p.value_rub is not None), Decimal("0"))
        usd = sum((p.value_usd for p in own if p.value_usd is not None), Decimal("0"))
        complete = (all(p.value_rub is not None and p.value_usd is not None for p in own)
                    and not any(error.startswith(f"{account.key}:") for error in snapshot.account_errors)
                    and not any(f":{account.key}:" in error for error in snapshot.valuation_errors))
        lines.append(f"\n<b>{html.escape(account.label or account.key)}</b> "
                     f"({html.escape(account.broker)}, {html.escape(account.owner_id)}, "
                     f"{html.escape(account.kind)})")
        lines.append(f"Счёт: {_money(rub if complete else None, 'RUB', blur=blur_amounts)} / "
                     f"{_money(usd if complete else None, 'USD', blur=blur_amounts)}")
        for valued in sorted(own, key=lambda p: p.position.name):
            p = valued.position
            lines.append(f"• {html.escape(p.name)}: {p.quantity_units} шт.; "
                         f"{_money(p.value_native, html.escape(p.price_currency), blur=blur_amounts)}; "
                         f"{_money(valued.value_rub, 'RUB', blur=blur_amounts)} / "
                         f"{_money(valued.value_usd, 'USD', blur=blur_amounts)}")
    return lines


class PortfolioBot:
    def __init__(self, token: str, allowed_user_ids: set[int], service: PortfolioService,
                 *, blur_amounts: bool = False, classification_enabled: bool = False,
                 classification_admin_ids: set[int] | None = None):
        self.bot = Bot(token=token)
        self.dispatcher = Dispatcher()
        self.allowed_user_ids = allowed_user_ids
        self.service = service
        self.blur_amounts = blur_amounts
        self.classification_enabled = classification_enabled
        self.classification_admin_ids = classification_admin_ids or set()
        self._allocation_views: dict[str, PortfolioAllocation] = {}
        self._classification_views: dict[str, ClassificationSession] = {}
        self._notified_unclassified: set[tuple[str, str]] = set()
        self._income_views: dict[str, IncomeReport] = {}
        self._refresh_tasks: dict[int, asyncio.Task[None]] = {}
        self._register()

    @staticmethod
    def _pct(value: Decimal | None) -> str:
        return "н/д" if value is None else f"{value * 100:.2f}%"

    @staticmethod
    def _node_at_path(root: AllocationNode, path: str) -> AllocationNode:
        node = root
        if path:
            for index in path.split("."):
                node = node.children[int(index)]
        return node

    @staticmethod
    def _unclassified_candidates(report: PortfolioAllocation) -> list[ClassificationCandidate]:
        candidates: dict[tuple[str, str], ClassificationCandidate] = {}

        def visit(node: AllocationNode) -> None:
            if node.synthetic and node.bucket_id.endswith(".unclassified"):
                for row in node.positions:
                    position = row.valued.position
                    key = (row.account.contour, position.instrument_id)
                    candidates.setdefault(key, ClassificationCandidate(
                        row.account.contour, position.instrument_id, position.name,
                        position.kind, row.account.label or row.account.key,
                    ))
            for child in node.children:
                visit(child)

        visit(report.root)
        return sorted(candidates.values(), key=lambda item: (item.contour, item.name.casefold()))

    def _new_classification_session(self, candidates: list[ClassificationCandidate]) -> str:
        token = secrets.token_hex(4)
        self._classification_views[token] = ClassificationSession(candidates)
        while len(self._classification_views) > 64:
            self._classification_views.pop(next(iter(self._classification_views)))
        return token

    @staticmethod
    def _classification_page(session: ClassificationSession, token: str, offset: int
                             ) -> tuple[str, InlineKeyboardMarkup]:
        rows = [[InlineKeyboardButton(
            text=f"{item.name} ({item.contour})"[:60],
            callback_data=f"cl:{token}:n:{index}:-",
        )] for index, item in enumerate(session.candidates[offset:offset + 20], start=offset)]
        nav = []
        if offset:
            nav.append(InlineKeyboardButton(text="←", callback_data=f"cl:{token}:l:{offset - 20}"))
        if offset + 20 < len(session.candidates):
            nav.append(InlineKeyboardButton(text="→", callback_data=f"cl:{token}:l:{offset + 20}"))
        if nav:
            rows.append(nav)
        return (f"<b>Без классификации: {len(session.candidates)}</b>\n"
                "Выберите инструмент.", InlineKeyboardMarkup(inline_keyboard=rows))

    def _classification_node(self, session: ClassificationSession, token: str,
                             index: int, path: str
                             ) -> tuple[str, InlineKeyboardMarkup, AllocationNode]:
        candidate = session.candidates[index]
        root, _status, _version = self.service.classification_tree()
        contour_node = next((child for child in root.children
                             if child.bucket_id == candidate.contour), None)
        if contour_node is None:
            raise ValueError("Contour is absent from the IPS")
        node = self._node_at_path(contour_node, "" if path == "-" else path)
        lines = [f"<b>{html.escape(candidate.name)}</b>",
                 f"Контур: {html.escape(candidate.contour)}; "
                 f"тип: {html.escape(candidate.kind)}",
                 f"Счёт: {html.escape(candidate.account_label)}",
                 f"Бакет: <code>{html.escape(node.bucket_id)}</code>"]
        rows: list[list[InlineKeyboardButton]] = []
        if node.children:
            lines.append("Выберите следующий уровень IPS:")
            for child_index, child in enumerate(node.children):
                child_path = str(child_index) if path == "-" else f"{path}.{child_index}"
                rows.append([InlineKeyboardButton(
                    text=child.label[:60], callback_data=f"cl:{token}:n:{index}:{child_path}",
                )])
        else:
            lines.append("Подтвердить назначение листового бакета?")
            rows.append([InlineKeyboardButton(
                text="Подтвердить", callback_data=f"cl:{token}:c:{index}:{path}",
            )])
        previous = "-" if path == "-" or "." not in path else path.rpartition(".")[0]
        rows.append([InlineKeyboardButton(
            text="← Назад" if path != "-" else "← К инструментам",
            callback_data=(f"cl:{token}:n:{index}:{previous}" if path != "-"
                           else f"cl:{token}:l:0"),
        )])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows), node

    @staticmethod
    def _income_node_at_path(root: IncomeNode, path: str) -> IncomeNode:
        node = root
        if path:
            for index in path.split("."):
                node = node.children[int(index)]
        return node

    @staticmethod
    def _allocation_keyboard(report: PortfolioAllocation, token: str,
                             node: AllocationNode, path: str = "", page: int = 0,
                             blur_amounts: bool = False,
                             ) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        if node.children:
            for index, child in enumerate(node.children):
                child_path = f"{path}.{index}" if path else str(index)
                value = ("" if blur_amounts else
                         f" · {_money(child.value_rub, 'RUB')}")
                text = f"{child.label} · {PortfolioBot._pct(child.weight_rub_pct)}{value}"
                rows.append([InlineKeyboardButton(
                    text=text[:60], callback_data=f"pa:{token}:n:{child_path}"
                )])
        elif node.positions:
            page_size = 8
            start = page * page_size
            end = min(start + page_size, len(node.positions))
            for index in range(start, end):
                row = node.positions[index]
                pos = row.valued.position
                value = row.valued.value_rub
                share = (value / report.total_rub
                         if value is not None and report.total_rub else None)
                rows.append([InlineKeyboardButton(
                    text=(f"{pos.name} · {PortfolioBot._pct(share)}" if blur_amounts else
                          f"{pos.name} · {PortfolioBot._pct(share)} · "
                          f"{_money(value, 'RUB')}")[:60],
                    callback_data=f"pa:{token}:p:{path}:{index}:{page}",
                )])
            nav: list[InlineKeyboardButton] = []
            if page > 0:
                nav.append(InlineKeyboardButton(
                    text="←", callback_data=f"pa:{token}:l:{path}:{page - 1}"
                ))
            if end < len(node.positions):
                nav.append(InlineKeyboardButton(
                    text="→", callback_data=f"pa:{token}:l:{path}:{page + 1}"
                ))
            if nav:
                rows.append(nav)
        if path:
            parent_path = path.rpartition(".")[0]
            rows.append([InlineKeyboardButton(
                text="← На уровень выше",
                callback_data=f"pa:{token}:n:{parent_path or 'home'}",
            )])
        if path or node.positions:
            rows.append([InlineKeyboardButton(
                text="К верхним бакетам", callback_data=f"pa:{token}:n:home"
            )])
        rows.append([InlineKeyboardButton(
            text="Отчет по доходам", callback_data="ir:open"
        )])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def _income_money(node: IncomeNode, metric: str, blur_amounts: bool = False) -> str:
        totals = node.totals[metric]
        return (f"{_money(totals.rub, 'RUB', blur=blur_amounts)} / "
                f"{_money(totals.usd, 'USD', blur=blur_amounts)}")

    @staticmethod
    def _income_share(value: Decimal, total: Decimal) -> str:
        return "н/д" if total == 0 else f"{value / total * 100:.2f}%"

    @staticmethod
    def _income_share_text(report: IncomeReport, rub: Decimal, usd: Decimal,
                           metric: str) -> str:
        total = report.root.totals[metric]
        return (
            f"Доля в общей сумме доходов: "
            f"RUB {PortfolioBot._income_share(rub, total.rub)} / "
            f"USD {PortfolioBot._income_share(usd, total.usd)}"
        )

    @staticmethod
    def _income_position_money(position: IncomePosition, metric: str,
                               blur_amounts: bool = False) -> str:
        totals = position.totals[metric]
        return (f"{_money(totals.rub, 'RUB', blur=blur_amounts)} / "
                f"{_money(totals.usd, 'USD', blur=blur_amounts)}")

    @staticmethod
    def _income_position_text(report: IncomeReport, position: IncomePosition,
                              metric: str, blur_amounts: bool = False) -> str:
        totals = position.totals[metric]
        return "\n".join([
            f"<b>{html.escape(position.name)}</b>",
            f"{html.escape(INCOME_METRIC_LABELS[metric])}",
            f"Счет: {html.escape(position.account_label)}",
            f"Инструмент: <code>{html.escape(position.instrument_id)}</code>",
            f"Итого: {_money(totals.rub, 'RUB', blur=blur_amounts)} / "
            f"{_money(totals.usd, 'USD', blur=blur_amounts)}",
            PortfolioBot._income_share_text(report, totals.rub, totals.usd, metric),
        ])

    @staticmethod
    def _income_summary_text(report: IncomeReport, blur_amounts: bool = False) -> str:
        year = report.as_of.year
        month = report.as_of.strftime("%m.%Y")
        lines = [
            "<b>Доходы портфеля</b>",
            f"На {_date(report.as_of)} · snapshot: {_date(report.snapshot_as_of)}",
            f"Факт с 01.01.{year}: "
            f"{PortfolioBot._income_money(report.root, 'actual_ytd', blur_amounts)}",
            f"Прогноз до 31.12.{year}: "
            f"{PortfolioBot._income_money(report.root, 'forecast_year', blur_amounts)}",
            "Факт пересчитан по курсу на дату выплаты; прогноз — по текущему курсу.",
            "Суммы взяты из данных брокеров; отдельный налоговый пересчет не выполняется.",
            f"Для детализации выберите период (текущий месяц: {month}):",
        ]
        if not report.actual_coverage_complete:
            lines.append("Охват фактических операций за год не подтвержден; факт может быть неполным.")
        lines.extend(html.escape(note) for note in report.notes)
        lines.append("Выберите показатель для разбивки по иерархии IPS:")
        return "\n".join(lines)

    @staticmethod
    def _income_node_text(report: IncomeReport, node: IncomeNode, metric: str,
                          blur_amounts: bool = False) -> str:
        lines = [
            f"<b>{html.escape(INCOME_METRIC_LABELS[metric])}</b>",
            f"{html.escape(node.label)}",
            f"Итого: {PortfolioBot._income_money(node, metric, blur_amounts)}",
            PortfolioBot._income_share_text(
                report, node.totals[metric].rub, node.totals[metric].usd, metric,
            ),
        ]
        if node.synthetic:
            lines.append("Есть выплаты по инструментам без активной классификации IPS.")
        positions_with_value = [position for position in node.positions
                                if position.totals[metric].rub or position.totals[metric].usd]
        if positions_with_value:
            lines.append(
                f"Позиций с выплатами за выбранный период: {len(positions_with_value)}. "
                "Выберите бумагу ниже."
            )
        if not report.actual_coverage_complete and metric.startswith("actual"):
            lines.append("Внимание: охват операций не подтвержден, показаны найденные события.")
        return "\n".join(lines)

    @staticmethod
    def _income_home_keyboard(token: str) -> InlineKeyboardMarkup:
        rows = [[InlineKeyboardButton(
            text=INCOME_METRIC_LABELS[metric], callback_data=f"ir:{token}:m:{metric}"
        )] for metric in METRICS]
        rows.append([InlineKeyboardButton(text="Обновить данные", callback_data=f"ir:{token}:r")])
        rows.append([InlineKeyboardButton(text="Портфель", callback_data=f"ir:{token}:p")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def _income_tree_keyboard(
        token: str, node: IncomeNode, metric: str,
        path: str = "", page: int = 0, blur_amounts: bool = False,
    ) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = []
        if node.children:
            for index, child in enumerate(node.children):
                child_path = f"{path}.{index}" if path else str(index)
                value = ("" if blur_amounts else
                         f" · {PortfolioBot._income_money(child, metric)}")
                text = f"{child.label}{value}"
                rows.append([InlineKeyboardButton(
                    text=text[:60], callback_data=f"ir:{token}:n:{metric}:{child_path}"
                )])
        elif node.positions:
            page_size = 8
            positions = [
                (index, position) for index, position in enumerate(node.positions)
                if position.totals[metric].rub or position.totals[metric].usd
            ]
            start = page * page_size
            end = min(start + page_size, len(positions))
            for index, position in positions[start:end]:
                rows.append([InlineKeyboardButton(
                    text=(f"{position.name}" if blur_amounts else
                          f"{position.name} · "
                          f"{PortfolioBot._income_position_money(position, metric)}")[:60],
                    callback_data=f"ir:{token}:i:{metric}:{path}:{index}",
                )])
            nav: list[InlineKeyboardButton] = []
            if page > 0:
                nav.append(InlineKeyboardButton(
                    text="←", callback_data=f"ir:{token}:l:{metric}:{path}:{page - 1}"
                ))
            if end < len(positions):
                nav.append(InlineKeyboardButton(
                    text="→", callback_data=f"ir:{token}:l:{metric}:{path}:{page + 1}"
                ))
            if nav:
                rows.append(nav)
        if path:
            parent_path = path.rpartition(".")[0]
            parent_callback = (f"ir:{token}:m:{metric}" if not parent_path else
                               f"ir:{token}:n:{metric}:{parent_path}")
            rows.append([InlineKeyboardButton(text="← На уровень выше", callback_data=parent_callback)])
        rows.append([InlineKeyboardButton(text="← К показателям", callback_data=f"ir:{token}:home")])
        rows.append([InlineKeyboardButton(text="Обновить данные", callback_data=f"ir:{token}:r")])
        rows.append([InlineKeyboardButton(text="Портфель", callback_data=f"ir:{token}:p")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def _refresh_in_background(
        self, message: Message, status_message: Message, chat_id: int, view: str,
    ) -> None:
        try:
            result = await self.service.refresh()
            token = secrets.token_hex(4)
            if view == "income":
                report = await self.service.income_report()
                self._income_views[token] = report
                while len(self._income_views) > 64:
                    self._income_views.pop(next(iter(self._income_views)))
                await message.edit_text(
                    self._income_summary_text(report, self.blur_amounts), parse_mode="HTML",
                    reply_markup=self._income_home_keyboard(token),
                )
            else:
                report = await self.service.portfolio_allocation()
                if report is None:
                    raise RuntimeError("Снимок не появился после обновления")
                self._allocation_views[token] = report
                while len(self._allocation_views) > 64:
                    self._allocation_views.pop(next(iter(self._allocation_views)))
                await message.edit_text(
                    self._overview_text(report, self.blur_amounts), parse_mode="HTML",
                    reply_markup=self._allocation_keyboard(
                        report, token, report.root, blur_amounts=self.blur_amounts,
                    ),
                )
            snapshot = result.snapshot
            operation_counts = Counter(
                value.split(":", 1)[0] if value.startswith("review_required:") else
                value.split(":", 1)[1] if value.startswith("error:") else value
                for value in result.operation_status.values() if value != "ok"
            )
            forecast_counts = Counter(
                value.split(":", 1)[0]
                for value in result.forecast_status.values() if value != "ok"
            )
            operation_issues = sum(operation_counts.values())
            forecast_issues = sum(forecast_counts.values())
            if not snapshot.complete:
                status = "завершено с неполным snapshot"
            elif operation_issues or forecast_issues:
                status = "завершено с замечаниями"
            else:
                status = "завершено успешно"
            operation_summary = ", ".join(
                f"{code}: {count}" for code, count in sorted(operation_counts.items())
            ) or "нет"
            forecast_summary = ", ".join(
                f"{code}: {count}" for code, count in sorted(forecast_counts.items())
            ) or "нет"
            await status_message.edit_text(
                f"Обновление портфеля и выплат {status}. "
                f"Snapshot: {_date(snapshot.as_of)}, счетов: {len(snapshot.accounts)}, "
                f"позиций: {len(snapshot.positions)}. Статусы операций: {operation_summary}. "
                f"Статусы прогноза: {forecast_summary}.",
            )
        except Exception as exc:
            logger.exception("Income background refresh failed")
            await status_message.edit_text(
                f"Обновление не удалось: {type(exc).__name__}. "
                "Предыдущий отчет оставлен без изменений.",
            )
        finally:
            task = self._refresh_tasks.get(chat_id)
            if task is asyncio.current_task():
                self._refresh_tasks.pop(chat_id, None)

    @staticmethod
    def _node_text(report: PortfolioAllocation, node: AllocationNode,
                   blur_amounts: bool = False) -> str:
        lines = [f"<b>{html.escape(node.label)}</b>",
                 f"RUB: {_money(node.value_rub, 'RUB', blur=blur_amounts)} · доля: "
                 f"{PortfolioBot._pct(node.weight_rub_pct)}",
                 f"USD: {_money(node.value_usd, 'USD', blur=blur_amounts)} · доля: "
                 f"{PortfolioBot._pct(node.weight_usd_pct)}"]
        if node.target_pct is not None:
            lines.append(
                f"Цель: {PortfolioBot._pct(node.target_pct)}; "
                f"целевой объём: {_money(node.target_rub, 'RUB', blur=blur_amounts)} / "
                f"{_money(node.target_usd, 'USD', blur=blur_amounts)}"
            )
            if node.deviation_rub_pct is not None:
                deviation = [f"RUB {node.deviation_rub_pct * 100:+.2f} п.п."]
                if node.deviation_usd_pct is not None:
                    deviation.append(f"USD {node.deviation_usd_pct * 100:+.2f} п.п.")
                lines.append("Отклонение: " + " / ".join(deviation))
        if node.positions:
            lines.append(f"Позиций: {len(node.positions)}")
        if node.synthetic:
            lines.append("Классификацию нужно подтвердить; позиция остаётся в NAV.")
        if report.ips_status != "active":
            lines.append("Цели показаны из черновика IPS.")
        if not report.complete:
            lines.append(
                "Внимание: доли рассчитаны от наблюдаемого NAV неполного snapshot; "
                "целевые суммы масштабированы от этого же NAV и изменятся после загрузки "
                "пропущенных данных."
            )
        return "\n".join(lines)

    @staticmethod
    def _overview_text(report: PortfolioAllocation, blur_amounts: bool = False) -> str:
        total_label = "Итого" if report.complete else "Наблюдаемый NAV"
        lines = ["<b>Структура инвестиционного портфеля</b>",
                 f"На {_date(report.as_of)} · охват: "
                 f"{'полный' if report.complete else 'неполный'}",
                 f"{total_label}: {_money(report.total_rub, 'RUB', blur=blur_amounts)} / "
                 f"{_money(report.total_usd, 'USD', blur=blur_amounts)}"]
        if not report.complete:
            lines.append(
                "Доли — от наблюдаемого NAV; целевые суммы рассчитаны от него же, "
                "а не от полного портфеля."
            )
        if report.ips_status != "active":
            lines.append("Целевые доли взяты из черновика IPS.")
        for node in report.root.children:
            target = (f" · цель {PortfolioBot._pct(node.target_pct)}"
                      if node.target_pct is not None else "")
            if node.deviation_rub_pct is not None:
                target += f" · отклонение {node.deviation_rub_pct * 100:+.2f} п.п."
            lines.append(
                f"• <b>{html.escape(node.label)}</b>: "
                f"{_money(node.value_rub, 'RUB', blur=blur_amounts)} / "
                f"{_money(node.value_usd, 'USD', blur=blur_amounts)} · "
                f"доли {PortfolioBot._pct(node.weight_rub_pct)} / "
                f"{PortfolioBot._pct(node.weight_usd_pct)}{target}"
            )
        if report.data_issues:
            lines.append("Проблемы данных: " + "; ".join(
                html.escape(item) for item in report.data_issues[:8]
            ))
        return "\n".join(lines)

    @staticmethod
    def _position_text(report: PortfolioAllocation, row: PositionRow,
                       blur_amounts: bool = False) -> str:
        p = row.valued.position
        weight_rub = (row.valued.value_rub / report.total_rub
                      if row.valued.value_rub is not None and report.total_rub else None)
        weight_usd = (row.valued.value_usd / report.total_usd
                      if row.valued.value_usd is not None and report.total_usd else None)
        return "\n".join([
            f"<b>{html.escape(p.name)}</b>",
            f"Брокер: {html.escape(row.account.broker)}; счёт: "
            f"{html.escape(row.account.label or row.account.account_id)}",
            f"Владелец: {html.escape(row.account.owner_id)}",
            f"ID: <code>{html.escape(p.instrument_id)}</code>",
            f"Тип: {html.escape(p.kind)}; количество: {p.quantity_units}",
            f"Стоимость: {_money(p.value_native, p.price_currency, blur=blur_amounts)}",
            f"Оценка: {_money(row.valued.value_rub, 'RUB', blur=blur_amounts)} / "
            f"{_money(row.valued.value_usd, 'USD', blur=blur_amounts)}",
            f"Доля{' портфеля' if report.complete else ' наблюдаемого NAV'}: "
            f"{PortfolioBot._pct(weight_rub)} / "
            f"{PortfolioBot._pct(weight_usd)}",
            f"Дата среза: {_date(report.as_of)}",
        ])

    async def _send(self, message: Message, lines: list[str]) -> None:
        for part in _parts(lines):
            await message.answer(part, parse_mode="HTML")

    def _register(self) -> None:
        @self.dispatcher.message(Command("start"))
        async def start(message: Message) -> None:
            if not await self._authorized(message):
                return
            try:
                snapshot = await self.service.portfolio()
            except Exception as exc:
                logger.exception("Start status failed")
                await message.answer(f"Не удалось прочитать статус: {type(exc).__name__}.")
                return
            await self._send(message, [
                "<b>Учёт портфеля</b>",
                "/refresh — загрузить данные T-Invest и Freedom",
                "/portfolio — структура портфеля с drill-down",
                "/performance — финансовый результат в RUB и USD",
                "/income — доходы с начала года и прогноз",
                *(["/classify — классифицировать новые инструменты"]
                  if self.classification_enabled and message.from_user and
                  message.from_user.id in self.classification_admin_ids else []),
                f"Последний срез: {_date(snapshot.as_of if snapshot else None)}",
            ])

        @self.dispatcher.message(Command("refresh"))
        async def refresh(message: Message) -> None:
            if not await self._authorized(message):
                return
            await message.answer("Синхронизирую счета, операции и выплаты. Это может занять несколько минут.")
            try:
                result = await self.service.refresh()
                lines = [f"Срез {_date(result.snapshot.as_of)}: "
                         f"{'полный' if result.snapshot.complete else 'неполный'}",
                         f"ID: <code>{html.escape(result.run_id)}</code>"]
                lines.extend(f"• Позиции: {html.escape(error)}" for error in result.snapshot.account_errors)
                lines.extend(f"• Оценка: {html.escape(error)}" for error in result.snapshot.valuation_errors)
                lines.extend(f"• Операции {html.escape(key)}: {html.escape(status)}" for
                             key, status in result.operation_status.items())
                lines.extend(f"• Прогноз {html.escape(key)}: {html.escape(status)}" for
                             key, status in result.forecast_status.items())
                await self._send(message, lines)
            except Exception as exc:
                logger.exception("Refresh failed")
                await message.answer(f"Синхронизация не завершена: {type(exc).__name__}. Подробности в логе.")
                return
            if (self.classification_enabled and message.from_user and
                    message.from_user.id in self.classification_admin_ids):
                try:
                    report = await self.service.portfolio_allocation()
                    candidates = self._unclassified_candidates(report) if report else []
                    current = {(item.contour, item.instrument_id) for item in candidates}
                    new = current - self._notified_unclassified
                    self._notified_unclassified.intersection_update(current)
                    if new:
                        token = self._new_classification_session(candidates)
                        await message.answer(
                            f"Без классификации: {len(candidates)} инструментов, "
                            f"из них новых: {len(new)}.",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                InlineKeyboardButton(
                                    text="Классифицировать",
                                    callback_data=f"cl:{token}:l:0",
                                ),
                            ]]),
                        )
                        self._notified_unclassified.update(current)
                except Exception:
                    logger.exception("Classification reminder failed after refresh")

        @self.dispatcher.message(Command("portfolio", "portfolio_brief"))
        async def portfolio(message: Message) -> None:
            if not await self._authorized(message):
                return
            try:
                report = await self.service.portfolio_allocation()
                if report is None:
                    await message.answer("Срез портфеля пока не загружен. Запустите /refresh.")
                    return
                token = secrets.token_hex(4)
                self._allocation_views[token] = report
                while len(self._allocation_views) > 64:
                    self._allocation_views.pop(next(iter(self._allocation_views)))
                root = report.root
                await message.answer(
                    self._overview_text(report, self.blur_amounts), parse_mode="HTML",
                    reply_markup=self._allocation_keyboard(
                        report, token, root, blur_amounts=self.blur_amounts,
                    ),
                )
            except Exception as exc:
                logger.exception("Portfolio report failed")
                await message.answer(f"Не удалось прочитать портфель: {type(exc).__name__}.")

        @self.dispatcher.message(Command("classify"))
        async def classify(message: Message) -> None:
            if not await self._authorized(message):
                return
            if (not self.classification_enabled or not message.from_user or
                    message.from_user.id not in self.classification_admin_ids):
                await message.answer("Классификация недоступна в этом окружении.")
                return
            try:
                report = await self.service.portfolio_allocation()
                candidates = self._unclassified_candidates(report) if report else []
                if not candidates:
                    await message.answer("Неклассифицированных инструментов нет.")
                    return
                token = self._new_classification_session(candidates)
                text, keyboard = self._classification_page(self._classification_views[token], token, 0)
                await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
            except Exception as exc:
                logger.exception("Classification list failed")
                await message.answer(f"Не удалось открыть классификацию: {type(exc).__name__}.")

        @self.dispatcher.callback_query(lambda query: bool(
            query.data and query.data.startswith("pa:")
        ))
        async def portfolio_allocation_callback(callback: CallbackQuery) -> None:
            user_id = callback.from_user.id
            if user_id not in self.allowed_user_ids:
                await callback.answer("Доступ запрещён.", show_alert=True)
                return
            message = callback.message
            if message is None or not callback.data:
                await callback.answer()
                return
            try:
                _, token, action, *parts = callback.data.split(":")
                report = self._allocation_views.get(token)
                if report is None:
                    await callback.answer("Отчёт устарел. Запустите /portfolio заново.", show_alert=True)
                    return
                if action == "p":
                    path, index_raw, page_raw = parts
                    node = self._node_at_path(report.root, path)
                    row = node.positions[int(index_raw)]
                    text = self._position_text(report, row, self.blur_amounts)
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(
                            text="← К позициям",
                            callback_data=f"pa:{token}:l:{path}:{page_raw}",
                        )],
                        [InlineKeyboardButton(
                            text="К верхним бакетам", callback_data=f"pa:{token}:n:home"
                        )],
                        [InlineKeyboardButton(
                            text="Отчет по доходам", callback_data="ir:open"
                        )],
                    ])
                    if (node.synthetic and self.classification_enabled and
                            user_id in self.classification_admin_ids):
                        candidate = ClassificationCandidate(
                            row.account.contour, row.valued.position.instrument_id,
                            row.valued.position.name, row.valued.position.kind,
                            row.account.label or row.account.key,
                        )
                        class_token = self._new_classification_session([candidate])
                        keyboard.inline_keyboard.insert(0, [InlineKeyboardButton(
                            text="Классифицировать",
                            callback_data=f"cl:{class_token}:n:0:-",
                        )])
                elif action == "r":
                    task = self._refresh_tasks.get(message.chat.id)
                    if task is not None and not task.done():
                        await callback.answer("Обновление уже выполняется.")
                        return
                    await callback.answer("Обновление запущено.")
                    status_message = await message.answer(
                        "Обновляю счета, позиции, операции и прогноз выплат. "
                        "Сообщу по завершении."
                    )
                    task = asyncio.create_task(
                        self._refresh_in_background(
                            message, status_message, message.chat.id, "portfolio",
                        ),
                        name=f"portfolio-refresh-{message.chat.id}",
                    )
                    self._refresh_tasks[message.chat.id] = task
                    return
                elif action == "l":
                    path, page_raw = parts
                    node = self._node_at_path(report.root, path)
                    page = int(page_raw)
                    page_count = max(1, (len(node.positions) + 7) // 8)
                    text = (self._node_text(report, node, self.blur_amounts) +
                            f"\nСтраница {page + 1} из {page_count}.")
                    keyboard = self._allocation_keyboard(
                        report, token, node, path, page,
                        blur_amounts=self.blur_amounts,
                    )
                elif action == "n":
                    path = parts[0]
                    if path == "home":
                        node = report.root
                        text = self._overview_text(report, self.blur_amounts)
                    else:
                        node = self._node_at_path(report.root, path)
                        text = self._node_text(report, node, self.blur_amounts)
                    keyboard = self._allocation_keyboard(
                        report, token, node, "" if path == "home" else path,
                        blur_amounts=self.blur_amounts,
                    )
                else:
                    raise ValueError("Unknown portfolio navigation action")
                await message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
                await callback.answer()
            except Exception as exc:
                logger.exception("Portfolio drill-down failed")
                await callback.answer("Не удалось открыть раздел.", show_alert=True)

        @self.dispatcher.callback_query(lambda query: bool(
            query.data and query.data.startswith("cl:")
        ))
        async def classification_callback(callback: CallbackQuery) -> None:
            if (not self.classification_enabled or
                    callback.from_user.id not in self.classification_admin_ids):
                await callback.answer("Доступ запрещён.", show_alert=True)
                return
            message = callback.message
            if message is None or not callback.data:
                await callback.answer()
                return
            try:
                _, token, action, *parts = callback.data.split(":")
                session = self._classification_views.get(token)
                if session is None:
                    await callback.answer("Выбор устарел. Запустите /classify заново.", show_alert=True)
                    return
                if action == "l":
                    text, keyboard = self._classification_page(session, token, int(parts[0]))
                elif action == "n":
                    text, keyboard, _node = self._classification_node(
                        session, token, int(parts[0]), parts[1],
                    )
                elif action == "c":
                    index, path = int(parts[0]), parts[1]
                    candidate = session.candidates[index]
                    _text, _keyboard, node = self._classification_node(
                        session, token, index, path,
                    )
                    if node.children or node.synthetic:
                        raise ValueError("Only IPS leaves can be classified")
                    await self.service.classify_instrument(
                        candidate.contour, candidate.instrument_id,
                        node.bucket_id, callback.from_user.id,
                    )
                    session.candidates.pop(index)
                    self._classification_views.pop(token, None)
                    if session.candidates:
                        token = self._new_classification_session(session.candidates)
                        text, keyboard = self._classification_page(
                            self._classification_views[token], token, 0,
                        )
                        text = (f"Назначен бакет <code>{html.escape(node.bucket_id)}</code>.\n"
                                + text)
                    else:
                        text = (f"Назначен бакет <code>{html.escape(node.bucket_id)}</code>.\n"
                                "Все инструменты в очереди классифицированы. "
                                "Откройте /portfolio для обновлённой структуры.")
                        keyboard = None
                else:
                    raise ValueError("Unknown classification action")
                await message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
                await callback.answer()
            except (ValueError, IndexError) as exc:
                await callback.answer(str(exc)[:180], show_alert=True)
            except Exception:
                logger.exception("Classification failed")
                await callback.answer("Классификация не сохранена. См. лог.", show_alert=True)

        @self.dispatcher.callback_query(lambda query: bool(
            query.data and query.data.startswith("ir:")
        ))
        async def income_callback(callback: CallbackQuery) -> None:
            if callback.from_user.id not in self.allowed_user_ids:
                await callback.answer("Доступ запрещён.", show_alert=True)
                return
            message = callback.message
            data = callback.data
            if message is None or not data:
                await callback.answer()
                return
            try:
                if data == "ir:open":
                    report = await self.service.income_report()
                    token = secrets.token_hex(4)
                    self._income_views[token] = report
                    while len(self._income_views) > 64:
                        self._income_views.pop(next(iter(self._income_views)))
                    await message.edit_text(
                        self._income_summary_text(report, self.blur_amounts), parse_mode="HTML",
                        reply_markup=self._income_home_keyboard(token),
                    )
                    await callback.answer()
                    return

                _, token, action, *parts = data.split(":")
                report = self._income_views.get(token)
                if report is None:
                    await callback.answer("Отчёт устарел. Откройте /income заново.", show_alert=True)
                    return
                if action == "r":
                    task = self._refresh_tasks.get(message.chat.id)
                    if task is not None and not task.done():
                        await callback.answer("Обновление уже выполняется.")
                        return
                    await callback.answer("Обновление запущено.")
                    status_message = await message.answer(
                        "Обновляю портфель, операции и прогноз выплат. Сообщу по завершении."
                    )
                    task = asyncio.create_task(
                        self._refresh_in_background(
                            message, status_message, message.chat.id, "income",
                        ),
                        name=f"income-refresh-{message.chat.id}",
                    )
                    self._refresh_tasks[message.chat.id] = task
                    return
                if action == "p":
                    portfolio = await self.service.portfolio_allocation()
                    if portfolio is None:
                        raise RuntimeError("Нет сохраненного snapshot портфеля")
                    new_token = secrets.token_hex(4)
                    self._allocation_views[new_token] = portfolio
                    while len(self._allocation_views) > 64:
                        self._allocation_views.pop(next(iter(self._allocation_views)))
                    await message.edit_text(
                        self._overview_text(portfolio, self.blur_amounts), parse_mode="HTML",
                        reply_markup=self._allocation_keyboard(
                            portfolio, new_token, portfolio.root,
                            blur_amounts=self.blur_amounts,
                        ),
                    )
                    await callback.answer()
                    return
                if action == "home":
                    text = self._income_summary_text(report, self.blur_amounts)
                    keyboard = self._income_home_keyboard(token)
                elif action == "m":
                    metric = parts[0]
                    node = report.root
                    text = self._income_node_text(report, node, metric, self.blur_amounts)
                    keyboard = self._income_tree_keyboard(
                        token, node, metric, blur_amounts=self.blur_amounts,
                    )
                elif action == "n":
                    metric, path = parts
                    node = self._income_node_at_path(report.root, path)
                    text = self._income_node_text(report, node, metric, self.blur_amounts)
                    keyboard = self._income_tree_keyboard(
                        token, node, metric, path, blur_amounts=self.blur_amounts,
                    )
                elif action == "l":
                    metric, path, page = parts
                    node = self._income_node_at_path(report.root, path)
                    text = self._income_node_text(report, node, metric, self.blur_amounts)
                    keyboard = self._income_tree_keyboard(
                        token, node, metric, path, int(page), self.blur_amounts,
                    )
                elif action == "i":
                    metric, path, index = parts
                    node = self._income_node_at_path(report.root, path)
                    position = node.positions[int(index)]
                    text = self._income_position_text(
                        report, position, metric, self.blur_amounts,
                    )
                    rows = [[InlineKeyboardButton(
                        text="← К бумагам бакета",
                        callback_data=f"ir:{token}:n:{metric}:{path}",
                    )], [InlineKeyboardButton(
                        text="← К показателям", callback_data=f"ir:{token}:home",
                    )], [InlineKeyboardButton(
                        text="Обновить данные", callback_data=f"ir:{token}:r",
                    )], [InlineKeyboardButton(
                        text="Портфель", callback_data=f"ir:{token}:p",
                    )]]
                    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
                else:
                    raise ValueError("Unknown income navigation action")
                await message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
                await callback.answer()
            except Exception as exc:
                logger.exception("Income drill-down failed")
                await callback.answer("Не удалось открыть раздел.", show_alert=True)

        @self.dispatcher.message(Command("performance"))
        async def performance(message: Message) -> None:
            if not await self._authorized(message):
                return
            lines = ["<b>Финансовый результат</b>"]
            if self.service.scope_policy:
                lines.append("Исключены non-tradeable и счета вне personal_main "
                             f"(политика {html.escape(self.service.scope_policy.version)}).")
            try:
                for scope, title in ((None, "Все счета"), ("tinkoff", "T-Invest"),
                                     ("freedom", "Freedom")):
                    result = await self.service.performance(broker=scope)
                    lines.append(f"\n<b>{title}</b>")
                    if result.start and result.latest:
                        lines.append(f"Период: {_date(result.start.as_of)} — {_date(result.latest.as_of)}")
                    if result.reason:
                        lines.append(f"Доходность недоступна: {html.escape(result.reason)}")
                    for estimate in (result.rub, result.usd):
                        if estimate is None:
                            continue
                        ccy = estimate.currency
                        lines.extend([
                            f"{ccy}: {_money(estimate.start_nav, ccy, blur=self.blur_amounts)} → "
                            f"{_money(estimate.end_nav, ccy, blur=self.blur_amounts)}",
                            f"Внешний поток: "
                            f"{_money(estimate.net_external_flow, ccy, blur=self.blur_amounts)}; "
                            f"результат: {_money(estimate.pnl, ccy, blur=self.blur_amounts)}",
                            f"Modified Dietz: {_money(estimate.modified_dietz_pct, '%')}; "
                            f"XIRR: {_money(estimate.xirr_pct, '%')}",
                        ])
            except Exception as exc:
                logger.exception("Performance report failed")
                await message.answer(f"Не удалось рассчитать результат: {type(exc).__name__}.")
                return
            await self._send(message, lines)

        @self.dispatcher.message(Command("income", "income_report"))
        async def income(message: Message) -> None:
            if not await self._authorized(message):
                return
            try:
                report = await self.service.income_report()
            except Exception as exc:
                logger.exception("Income report failed")
                await message.answer(f"Не удалось собрать выплаты: {type(exc).__name__}.")
                return
            token = secrets.token_hex(4)
            self._income_views[token] = report
            while len(self._income_views) > 64:
                self._income_views.pop(next(iter(self._income_views)))
            await message.answer(
                self._income_summary_text(report, self.blur_amounts), parse_mode="HTML",
                reply_markup=self._income_home_keyboard(token),
            )

    async def _authorized(self, message: Message) -> bool:
        user_id = message.from_user.id if message.from_user else None
        if user_id in self.allowed_user_ids:
            return True
        logger.warning("Unauthorized Telegram user %s", user_id)
        await message.answer("Доступ запрещён.")
        return False

    async def run(self) -> None:
        try:
            await self.dispatcher.start_polling(self.bot)
        finally:
            tasks = list(self._refresh_tasks.values())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self.bot.session.close()
