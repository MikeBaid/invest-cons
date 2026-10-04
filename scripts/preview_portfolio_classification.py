"""Read the latest saved snapshot and propose IPS buckets without changing facts."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src.portfolio_v2.domain import Account, Snapshot
from src.portfolio_v2.policy import InvestmentScopePolicy
from src.portfolio_v2.storage import Store


ROOT = Path(__file__).resolve().parents[1]


def policy_nodes(tree: dict) -> dict[str, dict]:
    nodes: dict[str, dict] = {}

    def visit(node: dict) -> None:
        nodes[node["id"]] = node
        for child in node.get("children", []):
            visit(child)

    visit(tree)
    return nodes


def legacy_index(entries: dict) -> tuple[dict[str, dict], set[str]]:
    indexed: dict[str, dict] = {}
    ambiguous: set[str] = set()
    for storage_key, entry in entries.items():
        for key in {storage_key, entry.get("instrument_uid", "")} - {""}:
            if key in indexed and indexed[key] is not entry:
                ambiguous.add(key)
            else:
                indexed[key] = entry
    return indexed, ambiguous


def classify(account: Account, instrument_id: str, kind: str, legacy: dict | None,
             price_currency: str, floating_coupon_flag: bool | None,
             nodes: dict[str, dict], rules: dict
             ) -> tuple[str | None, str | None, str, str]:
    old_bucket = legacy.get("ips_bucket", "") if legacy else ""
    for rule in rules["rules"]:
        if rule["kind"] != kind or rule["contour"] != account.contour:
            continue
        if rule.get("instrument_id") and instrument_id != rule["instrument_id"]:
            continue
        if rule.get("legacy_bucket_exact") and old_bucket != rule["legacy_bucket_exact"]:
            continue
        if not old_bucket.startswith(rule.get("legacy_bucket_prefix", "")):
            continue
        if ("require_floating_coupon_flag" in rule and
                floating_coupon_flag is not rule["require_floating_coupon_flag"]):
            continue
        currency_link = rule.get("currency_link")
        if rule.get("currency_link_from_price_currency"):
            currency_link = price_currency if price_currency in {"USD", "EUR"} else None
        if rule.get("require_price_currency") and price_currency != rule["require_price_currency"]:
            return None, currency_link, "no_ips_leaf", (
                f"Валюта инструмента {price_currency} не соответствует листу IPS"
            )
        bucket = rule.get("candidate_bucket")
        if not bucket:
            return None, currency_link, rule["status"], rule["reason"]
        if bucket not in nodes or nodes[bucket].get("children"):
            raise ValueError(f"Preview rule points to missing/non-leaf IPS bucket: {bucket}")
        return bucket, currency_link, rule["status"], rule["reason"]

    if not legacy:
        return None, None, "unclassified", "Нет сопоставления в старом маппинге"
    if kind == "bond":
        return None, None, "unclassified", (
            "Не подтверждены условия выпуска или валютная привязка; "
            "старый бакет не переносится автоматически"
        )
    return None, None, "unclassified", "Старый бакет не позволяет подтвердить новое направление"


def build_report(snapshot: Snapshot, legacy_entries: dict, nodes: dict[str, dict],
                 rules: dict, excluded_accounts: set[str], excluded_instruments: frozenset[str],
                 latest_attempt: Snapshot) -> dict:
    indexed, ambiguous = legacy_index(legacy_entries)
    accounts = {account.key: account for account in snapshot.accounts
                if account.account_id not in excluded_accounts and account.key not in excluded_accounts}
    rows = []
    excluded_count = 0
    excluded_rub = Decimal("0")
    excluded_unknown_value = 0
    ambiguous_matches = 0
    for valued in snapshot.positions:
        position = valued.position
        account = accounts.get(position.account_key)
        if account is None:
            continue
        match_key = position.instrument_id
        legacy = None if match_key in ambiguous else indexed.get(match_key)
        if match_key in ambiguous:
            ambiguous_matches += 1
        if match_key in excluded_instruments:
            excluded_count += 1
            if valued.value_rub is None:
                excluded_unknown_value += 1
            else:
                excluded_rub += valued.value_rub
            continue
        manual_facts = rules.get("manual_instrument_facts", {}).get(match_key, {})
        api_floating_flag = position.floating_coupon_flag
        effective_floating_flag = manual_facts.get("floating_coupon_flag", api_floating_flag)
        bucket, currency_link, status, reason = classify(
            account, match_key, position.kind, legacy, position.price_currency,
            effective_floating_flag, nodes, rules,
        )
        if manual_facts.get("currency_link"):
            currency_link = manual_facts["currency_link"]
        asset_class = rules["asset_class_by_position_kind"].get(position.kind)
        class_bucket = f"{account.contour}.{asset_class}" if asset_class else None
        if class_bucket not in nodes:
            class_bucket = None
        rows.append({
            "account_key": account.key,
            "account_label": account.label,
            "contour": account.contour,
            "instrument_id": match_key,
            "name": position.name,
            "kind": position.kind,
            "quantity": str(position.quantity_units),
            "price_currency": position.price_currency,
            "value_rub": str(valued.value_rub) if valued.value_rub is not None else None,
            "value_usd": str(valued.value_usd) if valued.value_usd is not None else None,
            "legacy_bucket": legacy.get("ips_bucket") if legacy else None,
            "asset_class_bucket": class_bucket,
            "candidate_bucket": bucket,
            "currency_link": currency_link,
            "api_floating_coupon_flag": api_floating_flag,
            "floating_coupon_flag": effective_floating_flag,
            "coupon_fact_source": "manual_confirmation" if manual_facts else
                                  "t_invest" if api_floating_flag is not None else "unknown",
            "coupon_fact_conflict": (api_floating_flag is not None and
                                     effective_floating_flag != api_floating_flag),
            "assignment_status": status,
            "reason": reason if match_key not in ambiguous else "Несколько записей старого маппинга с одним ID",
        })
    rows.sort(key=lambda row: (row["account_key"],
                               -Decimal(row["value_rub"] or "0"), row["instrument_id"]))
    counts = Counter(row["assignment_status"] for row in rows)
    bucket_counts = Counter(row["candidate_bucket"] or "unclassified" for row in rows)
    class_counts = Counter(row["asset_class_bucket"] or "unclassified" for row in rows)
    value_by_status = defaultdict(lambda: Decimal("0"))
    value_by_bucket = defaultdict(lambda: Decimal("0"))
    value_by_class = defaultdict(lambda: Decimal("0"))
    unvalued_count = 0
    for row in rows:
        if row["value_rub"] is None:
            unvalued_count += 1
            continue
        value = Decimal(row["value_rub"])
        value_by_status[row["assignment_status"]] += value
        value_by_bucket[row["candidate_bucket"] or "unclassified"] += value
        value_by_class[row["asset_class_bucket"] or "unclassified"] += value
    warnings = []
    if not snapshot.complete:
        warnings.append("Неполный снимок: доли портфеля и отклонения от IPS не рассчитываются")
    if latest_attempt.as_of > snapshot.as_of:
        warnings.append("Более свежая синхронизация завершилась без позиций; использован предыдущий снимок")
    return {
        "as_of": snapshot.as_of.isoformat(),
        "latest_attempt_as_of": latest_attempt.as_of.isoformat(),
        "scope": "selected_accounts_from_saved_snapshot",
        "accounts": [{"key": a.key, "label": a.label, "contour": a.contour,
                      "kind": a.kind} for a in accounts.values()],
        "excluded_account_keys": [a.key for a in snapshot.accounts if a.key not in accounts],
        "snapshot_complete": snapshot.complete,
        "account_errors": list(snapshot.account_errors),
        "valuation_errors": list(snapshot.valuation_errors),
        "warnings": warnings,
        "summary": {
            "visible_positions": len(rows),
            "status_counts": dict(counts),
            "candidate_bucket_counts": dict(bucket_counts),
            "asset_class_bucket_counts": dict(class_counts),
            "observed_value_rub_by_status": {key: str(value) for key, value in value_by_status.items()},
            "observed_value_rub_by_candidate_bucket": {
                key: str(value) for key, value in value_by_bucket.items()},
            "observed_value_rub_by_asset_class_bucket": {
                key: str(value) for key, value in value_by_class.items()},
            "unvalued_visible_positions": unvalued_count,
            "excluded_non_tradeable_count": excluded_count,
            "excluded_non_tradeable_value_rub_observed": str(excluded_rub),
            "excluded_non_tradeable_unknown_value_count": excluded_unknown_value,
            "ambiguous_legacy_id_count": ambiguous_matches,
        },
        "positions": rows,
    }


def markdown(report: dict) -> str:
    lines = ["# Пробная привязка фактического портфеля к IPS", "",
             f"Снимок: {report['as_of']}",
             f"Охват: {'полный' if report['snapshot_complete'] else 'неполный'}; "
             f"счетов: {len(report['accounts'])}; "
             f"видимых позиций: {report['summary']['visible_positions']}.",
             f"Исключено как non-tradeable: {report['summary']['excluded_non_tradeable_count']} "
             "позиций. Они не входят в инвестиционный NAV и таблицу IPS; "
             "исходный снимок не изменен.", ""]
    for warning in report["warnings"]:
        lines.append(f"**Внимание:** {warning}")
    if report["account_errors"]:
        lines.append("Ошибки счетов: " + "; ".join(report["account_errors"]))
    lines.extend(["", "| Счет | Бумага | Вид | RUB | Старый бакет | Класс IPS | Валютная привязка | Плавающий купон | Лист IPS | Статус |",
                  "|---|---|---|---:|---|---|---|---|---|---|"])
    for row in report["positions"]:
        cells = [row["account_label"], row["name"], row["kind"],
                 row["value_rub"] or "н/д", row["legacy_bucket"] or "—",
                 row["asset_class_bucket"] or "—", row["currency_link"] or "—",
                 (("да" if row["floating_coupon_flag"] is True else
                   "нет" if row["floating_coupon_flag"] is False else "н/д") +
                  ("*" if row["coupon_fact_conflict"] else "")),
                 row["candidate_bucket"] or "—", row["assignment_status"]]
        lines.append("| " + " | ".join(str(cell).replace("|", "\\|").replace("\n", " ")
                                        for cell in cells) + " |")
    lines.extend(["", "`*` ручное подтверждение расходится с флагом T-Invest. "
                  "`proposed` не является подтвержденным назначением; "
                  "`unclassified` требует проверки признаков инструмента.", ""])
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Preview IPS mapping from a saved portfolio snapshot")
    parser.add_argument("--legacy-mapping", type=Path, required=True)
    parser.add_argument("--ips", type=Path, default=ROOT / "config/ips/personal_main.draft.yaml")
    parser.add_argument("--legacy-rules", type=Path,
                        default=ROOT / "config/ips/legacy_mapping_preview.yaml")
    parser.add_argument("--exclusions", type=Path,
                        default=ROOT / "config/ips/portfolio_exclusions.yaml")
    parser.add_argument("--output", type=Path, default=ROOT / "reports/classification_preview.json")
    parser.add_argument("--exclude-account", action="append", default=[])
    parser.add_argument("--list-accounts", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise ValueError("DATABASE_URL is required")
    store = await Store.connect(dsn)
    try:
        snapshots = await store.latest_snapshots(limit=100)
    finally:
        await store.close()
    if not snapshots:
        raise ValueError("No saved snapshot; run run_portfolio_v2.py --refresh-once first")
    snapshot = next((item for item in snapshots if item.positions), None)
    if snapshot is None:
        raise ValueError("No saved snapshot with positions; broker refresh must succeed first")
    if args.list_accounts:
        for account in snapshot.accounts:
            print(json.dumps({"key": account.key, "label": account.label,
                              "kind": account.kind}, ensure_ascii=True))
        return
    mapping = json.loads(args.legacy_mapping.read_text(encoding="utf-8"))
    entries = mapping["entries"]
    if not isinstance(entries, dict):
        raise ValueError("Legacy mapping must contain an entries object")
    policy = yaml.safe_load(args.ips.read_text(encoding="utf-8"))
    nodes = policy_nodes(policy["allocation"]["tree"])
    rules = yaml.safe_load(args.legacy_rules.read_text(encoding="utf-8"))
    exclusions = InvestmentScopePolicy.load(args.exclusions)
    excluded = set(args.exclude_account) | set(exclusions.excluded_account_keys)
    excluded.update(item.strip() for item in
                    os.getenv("PORTFOLIO_V2_EXCLUDE_ACCOUNTS", "").split(",") if item.strip())
    report = build_report(snapshot, entries, nodes, rules, excluded,
                          exclusions.excluded_instrument_ids, snapshots[0])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = args.output.with_suffix(".md")
    md_path.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"as_of": report["as_of"], "accounts": len(report["accounts"]),
                      **report["summary"], "json": str(args.output), "markdown": str(md_path)},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
