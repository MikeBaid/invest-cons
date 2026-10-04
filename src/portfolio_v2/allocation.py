"""IPS tree aggregation over one immutable portfolio snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from .domain import Account, Snapshot, ValuedPosition


ZERO = Decimal("0")


@dataclass(slots=True)
class PositionRow:
    valued: ValuedPosition
    account: Account


@dataclass(slots=True)
class AllocationNode:
    bucket_id: str
    label: str
    target_pct: Decimal | None
    value_rub: Decimal = ZERO
    value_usd: Decimal = ZERO
    weight_rub_pct: Decimal | None = None
    weight_usd_pct: Decimal | None = None
    deviation_rub_pct: Decimal | None = None
    deviation_usd_pct: Decimal | None = None
    target_rub: Decimal | None = None
    target_usd: Decimal | None = None
    children: list["AllocationNode"] = field(default_factory=list)
    positions: list[PositionRow] = field(default_factory=list)
    synthetic: bool = False


@dataclass(slots=True)
class PortfolioAllocation:
    as_of: Any
    complete: bool
    ips_status: str
    total_rub: Decimal | None
    total_usd: Decimal | None
    root: AllocationNode
    data_issues: tuple[str, ...]


def _node_from_config(raw: dict[str, Any]) -> AllocationNode:
    return AllocationNode(
        bucket_id=str(raw["id"]),
        label=str(raw.get("label_ru") or raw.get("label") or raw["id"]),
        target_pct=Decimal(str(raw["target"])) if raw.get("target") is not None else None,
        children=[_node_from_config(child) for child in raw.get("children", [])],
    )


def load_allocation_tree(path: Path) -> tuple[AllocationNode, str, str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    tree = data["allocation"]["tree"]
    root = _node_from_config(tree)
    nodes: dict[str, AllocationNode] = {}

    def index(node: AllocationNode) -> None:
        if node.bucket_id in nodes:
            raise ValueError(f"Duplicate IPS bucket id: {node.bucket_id}")
        nodes[node.bucket_id] = node
        for child in node.children:
            index(child)

    index(root)
    for node in nodes.values():
        if not node.children:
            continue
        if node.target_pct is None or any(child.target_pct is None for child in node.children):
            continue
        children_sum = sum((child.target_pct for child in node.children if child.target_pct is not None), ZERO)
        if children_sum != node.target_pct:
            raise ValueError(
                f"IPS targets under {node.bucket_id} sum to {children_sum}, "
                f"expected {node.target_pct}"
            )
    return root, str(data.get("status", "draft")), str(data.get("version", "unknown"))


def build_portfolio_allocation(
    snapshot: Snapshot,
    assignments: dict[tuple[str, str], dict[str, str]],
    ips_path: Path,
) -> PortfolioAllocation:
    root, ips_status, _version = load_allocation_tree(ips_path)
    nodes: dict[str, AllocationNode] = {}
    parent: dict[str, AllocationNode] = {}

    def index(node: AllocationNode) -> None:
        nodes[node.bucket_id] = node
        for child in node.children:
            parent[child.bucket_id] = node
            index(child)

    index(root)
    contours = {account.key: account.contour for account in snapshot.accounts}
    unclassified_nodes: dict[str, AllocationNode] = {}
    data_issues = list(snapshot.account_errors) + list(snapshot.valuation_errors)

    for valued in snapshot.positions:
        position = valued.position
        contour = contours.get(position.account_key)
        if contour is None:
            data_issues.append(f"unknown_account:{position.account_key}")
            continue
        assignment = assignments.get((contour, position.instrument_id))
        bucket_id = assignment.get("bucket_id") if assignment else None
        if not bucket_id and position.kind == "cash":
            cash_id = f"{contour}.liquidity.cash"
            if cash_id in nodes:
                bucket_id = cash_id
        target = nodes.get(bucket_id or "")
        if target is None or target.children:
            target = unclassified_nodes.get(contour)
            if target is None:
                parent_node = nodes.get(contour)
                if parent_node is None:
                    data_issues.append(f"missing_contour_node:{contour}")
                    continue
                target = AllocationNode(
                    bucket_id=f"{contour}.unclassified",
                    label="Без классификации",
                    target_pct=None,
                    synthetic=True,
                )
                parent_node.children.append(target)
                unclassified_nodes[contour] = target
                nodes[target.bucket_id] = target
                parent[target.bucket_id] = parent_node
        target.positions.append(PositionRow(valued, next(
            account for account in snapshot.accounts if account.key == position.account_key
        )))
        target.value_rub += valued.value_rub or ZERO
        target.value_usd += valued.value_usd or ZERO

    complete = snapshot.complete and not data_issues
    observed_rub = [position.value_rub for position in snapshot.positions
                    if position.value_rub is not None]
    observed_usd = [position.value_usd for position in snapshot.positions
                    if position.value_usd is not None]
    total_rub = sum(observed_rub, ZERO) if observed_rub else None
    total_usd = sum(observed_usd, ZERO) if observed_usd else None

    def roll_up(node: AllocationNode) -> tuple[Decimal, Decimal]:
        for child in node.children:
            child_rub, child_usd = roll_up(child)
            node.value_rub += child_rub
            node.value_usd += child_usd
        if total_rub is not None and total_rub != ZERO:
            node.weight_rub_pct = node.value_rub / total_rub
        if total_usd is not None and total_usd != ZERO:
            node.weight_usd_pct = node.value_usd / total_usd
        if node.target_pct is not None:
            if total_rub is not None:
                node.target_rub = total_rub * node.target_pct
                if node.weight_rub_pct is not None:
                    node.deviation_rub_pct = node.weight_rub_pct - node.target_pct
            if total_usd is not None:
                node.target_usd = total_usd * node.target_pct
                if node.weight_usd_pct is not None:
                    node.deviation_usd_pct = node.weight_usd_pct - node.target_pct
        node.positions.sort(key=lambda row: row.valued.position.name.casefold())
        return node.value_rub, node.value_usd

    roll_up(root)
    return PortfolioAllocation(
        as_of=snapshot.as_of,
        complete=complete,
        ips_status=ips_status,
        total_rub=total_rub,
        total_usd=total_usd,
        root=root,
        data_issues=tuple(dict.fromkeys(data_issues)),
    )
