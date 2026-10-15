"""Isolated PostgreSQL storage for reproducible portfolio snapshots and events."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg

from .domain import Account, CashFlow, FxFixing, IncomeEvent, Position, Snapshot, ValuedPosition


DDL = """
CREATE SCHEMA IF NOT EXISTS portfolio_v2;
CREATE TABLE IF NOT EXISTS portfolio_v2.refreshes (
    run_id uuid PRIMARY KEY,
    as_of timestamptz NOT NULL,
    accounts jsonb NOT NULL,
    account_errors jsonb NOT NULL,
    valuation_errors jsonb NOT NULL,
    fx_source text NOT NULL,
    fx_date date NOT NULL,
    fx_rates jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_v2_refreshes_as_of ON portfolio_v2.refreshes(as_of DESC);
CREATE TABLE IF NOT EXISTS portfolio_v2.positions (
    run_id uuid NOT NULL REFERENCES portfolio_v2.refreshes(run_id),
    account_key text NOT NULL,
    instrument_id text NOT NULL,
    name text NOT NULL,
    kind text NOT NULL,
    quantity_units numeric NOT NULL,
    price_per_unit numeric,
    accrued_per_unit numeric NOT NULL,
    accrued_currency text,
    price_currency text NOT NULL,
    broker_value_native numeric,
    value_rub numeric,
    value_usd numeric,
    floating_coupon_flag boolean,
    PRIMARY KEY (run_id, account_key, instrument_id)
);
ALTER TABLE portfolio_v2.positions ADD COLUMN IF NOT EXISTS accrued_currency text;
ALTER TABLE portfolio_v2.positions ADD COLUMN IF NOT EXISTS floating_coupon_flag boolean;
CREATE TABLE IF NOT EXISTS portfolio_v2.events (
    account_key text NOT NULL,
    event_id text NOT NULL,
    occurred_at timestamptz NOT NULL,
    event_kind text NOT NULL,
    instrument_id text NOT NULL DEFAULT '',
    amount_native numeric NOT NULL,
    currency text NOT NULL,
    amount_rub numeric,
    amount_usd numeric,
    source text NOT NULL,
    raw jsonb NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_key, event_id)
);
CREATE INDEX IF NOT EXISTS ix_v2_events_time ON portfolio_v2.events(occurred_at);
CREATE TABLE IF NOT EXISTS portfolio_v2.forecasts (
    account_key text NOT NULL,
    event_id text NOT NULL,
    instrument_id text NOT NULL,
    kind text NOT NULL,
    payment_at timestamptz NOT NULL,
    amount_native numeric,
    currency text NOT NULL,
    status text NOT NULL,
    source text NOT NULL,
    notes text NOT NULL DEFAULT '',
    observed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_key, event_id)
);
CREATE TABLE IF NOT EXISTS portfolio_v2.sync_state (
    account_key text PRIMARY KEY,
    success_from timestamptz,
    success_to timestamptz,
    flows_verified boolean NOT NULL DEFAULT false,
    income_success_from timestamptz,
    income_success_to timestamptz,
    income_verified boolean NOT NULL DEFAULT false,
    last_error text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE portfolio_v2.sync_state ADD COLUMN IF NOT EXISTS income_success_from timestamptz;
ALTER TABLE portfolio_v2.sync_state ADD COLUMN IF NOT EXISTS income_success_to timestamptz;
ALTER TABLE portfolio_v2.sync_state ADD COLUMN IF NOT EXISTS income_verified boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS portfolio_v2.fx_daily (
    requested_date date NOT NULL,
    currency text NOT NULL,
    rub_per_unit numeric NOT NULL,
    source_date date NOT NULL,
    source text NOT NULL,
    PRIMARY KEY (requested_date, currency)
);
CREATE TABLE IF NOT EXISTS portfolio_v2.forecast_sync (
    account_key text PRIMARY KEY,
    observed_at timestamptz NOT NULL DEFAULT now(),
    errors jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS portfolio_v2.classification_history (
    classification_id bigserial PRIMARY KEY,
    contour text NOT NULL,
    instrument_id text NOT NULL,
    bucket_id text NOT NULL,
    classification_version text NOT NULL,
    source text NOT NULL,
    reason text NOT NULL DEFAULT '',
    valid_from timestamptz NOT NULL DEFAULT now(),
    valid_to timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_v2_classification_active
    ON portfolio_v2.classification_history(contour, instrument_id)
    WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS ix_v2_classification_history_lookup
    ON portfolio_v2.classification_history(contour, instrument_id, valid_from DESC);
"""


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _decoded(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class Store:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def connect(cls, dsn: str) -> "Store":
        normalized = dsn.replace("postgresql+asyncpg://", "postgresql://", 1)
        pool = await asyncpg.create_pool(dsn=normalized, min_size=1, max_size=5)
        return cls(pool)

    async def close(self) -> None:
        await self.pool.close()

    async def initialize(self) -> None:
        async with self.pool.acquire() as connection:
            await connection.execute(DDL)

    async def load_fx(self, day: date) -> FxFixing | None:
        rows = await self.pool.fetch(
            "SELECT currency, rub_per_unit, source_date, source FROM portfolio_v2.fx_daily "
            "WHERE requested_date=$1", day,
        )
        if not rows or not any(row["currency"] == "USD" for row in rows):
            return None
        return FxFixing(
            rate_date=datetime.combine(rows[0]["source_date"], datetime.min.time(), timezone.utc),
            source=rows[0]["source"],
            rub_per_currency={r["currency"]: r["rub_per_unit"] for r in rows},
        )

    async def save_fx(self, day: date, fixing: FxFixing) -> None:
        async with self.pool.acquire() as connection:
            await connection.executemany(
                "INSERT INTO portfolio_v2.fx_daily "
                "(requested_date,currency,rub_per_unit,source_date,source) VALUES($1,$2,$3,$4,$5) "
                "ON CONFLICT(requested_date,currency) DO UPDATE SET "
                "rub_per_unit=EXCLUDED.rub_per_unit,source_date=EXCLUDED.source_date,source=EXCLUDED.source",
                [(day, c, v, fixing.rate_date.date(), fixing.source)
                 for c, v in fixing.rub_per_currency.items()],
            )

    async def save_snapshot(self, run_id: UUID, snapshot: Snapshot, fixing: FxFixing) -> None:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "INSERT INTO portfolio_v2.refreshes "
                    "(run_id,as_of,accounts,account_errors,valuation_errors,fx_source,fx_date,fx_rates) "
                    "VALUES ($1,$2,$3::jsonb,$4::jsonb,$5::jsonb,$6,$7,$8::jsonb)",
                    run_id, snapshot.as_of,
                    _j([asdict(a) for a in snapshot.accounts]),
                    _j(snapshot.account_errors), _j(snapshot.valuation_errors),
                    fixing.source, fixing.rate_date.date(),
                    _j({c: str(v) for c, v in fixing.rub_per_currency.items()}),
                )
                rows = []
                for valued in snapshot.positions:
                    p = valued.position
                    rows.append((run_id, p.account_key, p.instrument_id, p.name, p.kind,
                                 p.quantity_units, p.price_per_unit, p.accrued_per_unit,
                                 p.price_currency, p.broker_value_native,
                                 valued.value_rub, valued.value_usd, p.accrued_currency,
                                 p.floating_coupon_flag))
                if rows:
                    await connection.executemany(
                        "INSERT INTO portfolio_v2.positions "
                        "(run_id,account_key,instrument_id,name,kind,quantity_units,price_per_unit,"
                        "accrued_per_unit,price_currency,broker_value_native,value_rub,value_usd,"
                        "accrued_currency,floating_coupon_flag) "
                        "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)", rows,
                    )

    async def save_events(self, account_key: str, events: list[dict[str, Any]], *,
                          success_from: datetime, success_to: datetime,
                          flows_verified: bool, income_verified: bool = False,
                          error: str | None = None) -> None:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                for row in events:
                    await connection.execute(
                        "INSERT INTO portfolio_v2.events "
                        "(account_key,event_id,occurred_at,event_kind,instrument_id,amount_native,"
                        "currency,amount_rub,amount_usd,source,raw) "
                        "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb) "
                        "ON CONFLICT(account_key,event_id) DO UPDATE SET "
                        "occurred_at=EXCLUDED.occurred_at,event_kind=EXCLUDED.event_kind,"
                        "instrument_id=EXCLUDED.instrument_id,amount_native=EXCLUDED.amount_native,"
                        "currency=EXCLUDED.currency,amount_rub=EXCLUDED.amount_rub,"
                        "amount_usd=EXCLUDED.amount_usd,raw=EXCLUDED.raw,observed_at=now()",
                        account_key, row["event_id"], row["occurred_at"], row["event_kind"],
                        row.get("instrument_id", ""), row["amount_native"], row["currency"],
                        row.get("amount_rub"), row.get("amount_usd"), row["source"],
                        _j(row.get("raw", {})),
                    )
                await connection.execute(
                    "INSERT INTO portfolio_v2.sync_state "
                    "(account_key,success_from,success_to,flows_verified,last_error," 
                    "income_success_from,income_success_to,income_verified) "
                    "VALUES($1,$2,$3,$4,$5,$2,$3,$6) ON CONFLICT(account_key) DO UPDATE SET "
                    "success_from=LEAST(portfolio_v2.sync_state.success_from,EXCLUDED.success_from),"
                    "success_to=GREATEST(portfolio_v2.sync_state.success_to,EXCLUDED.success_to),"
                    "flows_verified=portfolio_v2.sync_state.flows_verified AND EXCLUDED.flows_verified,"
                    "income_success_from=LEAST(portfolio_v2.sync_state.income_success_from,"
                    "EXCLUDED.income_success_from),"
                    "income_success_to=GREATEST(portfolio_v2.sync_state.income_success_to,"
                    "EXCLUDED.income_success_to),"
                    "income_verified=EXCLUDED.income_verified,"
                    "last_error=EXCLUDED.last_error,updated_at=now()",
                    account_key, success_from, success_to, flows_verified, error, income_verified,
                )

    async def sync_start(self, account_key: str) -> datetime | None:
        row = await self.pool.fetchrow(
            "SELECT success_to FROM portfolio_v2.sync_state WHERE account_key=$1", account_key,
        )
        return row["success_to"] if row else None

    async def needs_income_backfill(self, account_key: str, year_start: datetime) -> bool:
        row = await self.pool.fetchrow(
            "SELECT income_success_from,income_verified FROM portfolio_v2.sync_state "
            "WHERE account_key=$1", account_key,
        )
        return (row is None or not row["income_verified"] or
                row["income_success_from"] is None or row["income_success_from"] > year_start)

    async def income_operations_cover(self, accounts: tuple[Account, ...],
                                      start: datetime, end: datetime) -> bool:
        covered, _confirmed, _total = await self.income_operations_coverage(accounts, start, end)
        return covered

    async def income_operations_coverage(self, accounts: tuple[Account, ...],
                                         start: datetime, end: datetime
                                         ) -> tuple[bool, int, int]:
        keys = [account.key for account in accounts]
        if not keys:
            return False, 0, 0
        rows = await self.pool.fetch(
            "SELECT account_key,income_success_from,income_success_to,income_verified "
            "FROM portfolio_v2.sync_state WHERE account_key=ANY($1::text[])", keys,
        )
        coverage = {row["account_key"]: row for row in rows}
        confirmed = sum(
            account.key in coverage
            and coverage[account.key]["income_verified"]
            and coverage[account.key]["income_success_from"] is not None
            and coverage[account.key]["income_success_from"] <= start
            and coverage[account.key]["income_success_to"] is not None
            and coverage[account.key]["income_success_to"] >= end
            for account in accounts
        )
        total = len(accounts)
        return confirmed == total, confirmed, total

    async def save_sync_error(self, account_key: str, error: str) -> None:
        await self.pool.execute(
            "INSERT INTO portfolio_v2.sync_state(account_key,last_error) VALUES($1,$2) "
            "ON CONFLICT(account_key) DO UPDATE SET last_error=$2,updated_at=now()",
            account_key, error,
        )

    async def save_forecasts(self, account_key: str, events: list[IncomeEvent]) -> None:
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM portfolio_v2.forecasts WHERE account_key=$1", account_key,
                )
                for event in events:
                    await connection.execute(
                        "INSERT INTO portfolio_v2.forecasts "
                        "(account_key,event_id,instrument_id,kind,payment_at,amount_native,currency,"
                        "status,source,notes) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                        account_key, event.event_id, event.instrument_id, event.kind,
                        event.payment_at, event.amount_native, event.currency,
                        event.status, event.source, event.notes,
                    )

    async def save_forecast_errors(self, account_key: str, errors: list[str]) -> None:
        await self.pool.execute(
            "INSERT INTO portfolio_v2.forecast_sync(account_key,errors) VALUES($1,$2::jsonb) "
            "ON CONFLICT(account_key) DO UPDATE SET errors=EXCLUDED.errors,observed_at=now()",
            account_key, _j(errors),
        )

    async def forecast_errors(self) -> dict[str, list[str]]:
        rows = await self.pool.fetch("SELECT account_key,errors FROM portfolio_v2.forecast_sync")
        return {r["account_key"]: _decoded(r["errors"]) for r in rows}

    async def latest_snapshots(self, *, limit: int = 100) -> list[Snapshot]:
        headers = await self.pool.fetch(
            "SELECT * FROM portfolio_v2.refreshes ORDER BY as_of DESC LIMIT $1", limit,
        )
        result = []
        for header in headers:
            positions = await self.pool.fetch(
                "SELECT * FROM portfolio_v2.positions WHERE run_id=$1", header["run_id"],
            )
            accounts = tuple(Account(**a) for a in _decoded(header["accounts"]))
            result.append(Snapshot(
                as_of=header["as_of"], accounts=accounts,
                positions=tuple(ValuedPosition(
                    Position(
                        p["account_key"], p["instrument_id"], p["name"], p["kind"],
                        p["quantity_units"], p["price_currency"], p["price_per_unit"],
                        p["accrued_per_unit"], p["broker_value_native"], p["accrued_currency"],
                        p["floating_coupon_flag"],
                    ), p["value_rub"], p["value_usd"],
                ) for p in positions),
                account_errors=tuple(_decoded(header["account_errors"])),
                valuation_errors=tuple(_decoded(header["valuation_errors"])),
            ))
        return result

    async def active_classifications(self) -> dict[tuple[str, str], dict[str, str]]:
        rows = await self.pool.fetch(
            "SELECT contour,instrument_id,bucket_id,classification_version,source,reason "
            "FROM portfolio_v2.classification_history WHERE valid_to IS NULL"
        )
        return {(row["contour"], row["instrument_id"]): {
            "bucket_id": row["bucket_id"],
            "version": row["classification_version"],
            "source": row["source"],
            "reason": row["reason"],
        } for row in rows}

    async def set_active_classifications(self, rows: list[dict[str, str]],
                                         *, preserve_manual: bool = False) -> int:
        """Version confirmed instrument assignments; identical active rows are idempotent."""
        changed = 0
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                for row in rows:
                    await connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                        f"{row['contour']}:{row['instrument_id']}",
                    )
                    current = await connection.fetchrow(
                        "SELECT bucket_id,classification_version,source FROM "
                        "portfolio_v2.classification_history "
                        "WHERE contour=$1 AND instrument_id=$2 AND valid_to IS NULL FOR UPDATE",
                        row["contour"], row["instrument_id"],
                    )
                    if ("expected_bucket_id" in row and
                            (current["bucket_id"] if current else None) != row["expected_bucket_id"]):
                        raise ValueError("Classification changed; reopen the report")
                    if preserve_manual and current and current["source"] == "telegram_manual":
                        continue
                    if current and (current["bucket_id"], current["classification_version"]) == (
                        row["bucket_id"], row["classification_version"]
                    ):
                        continue
                    if current:
                        await connection.execute(
                            "UPDATE portfolio_v2.classification_history SET valid_to=now() "
                            "WHERE contour=$1 AND instrument_id=$2 AND valid_to IS NULL",
                            row["contour"], row["instrument_id"],
                        )
                    await connection.execute(
                        "INSERT INTO portfolio_v2.classification_history "
                        "(contour,instrument_id,bucket_id,classification_version,source,reason) "
                        "VALUES($1,$2,$3,$4,$5,$6)",
                        row["contour"], row["instrument_id"], row["bucket_id"],
                        row["classification_version"], row["source"], row.get("reason", ""),
                    )
                    changed += 1
        return changed

    async def flows_between(self, start: datetime, end: datetime) -> list[CashFlow]:
        rows = await self.pool.fetch(
            "SELECT * FROM portfolio_v2.events WHERE occurred_at>$1 AND occurred_at<=$2 "
            "AND event_kind IN ('external_in','external_out') ORDER BY occurred_at",
            start, end,
        )
        return [CashFlow(r["account_key"], r["occurred_at"], r["amount_native"],
                         r["currency"], r["amount_rub"], r["amount_usd"]) for r in rows]

    async def excluded_instrument_flows_between(
        self, start: datetime, end: datetime, instrument_ids: frozenset[str],
        account_keys: set[str], affected_account_keys: set[str],
    ) -> list[CashFlow] | None:
        if not instrument_ids or not account_keys:
            return []
        rows = await self.pool.fetch(
            "SELECT account_key,occurred_at,event_kind,amount_native,currency,amount_rub,amount_usd "
            "FROM portfolio_v2.events WHERE occurred_at>$1 AND occurred_at<=$2 "
            "AND instrument_id=ANY($3::text[]) AND account_key=ANY($4::text[])",
            start, end, list(instrument_ids), list(account_keys),
        )
        if any(row["event_kind"] not in {"coupon", "dividend"} or
               row["amount_rub"] is None or row["amount_usd"] is None for row in rows):
            return None
        if affected_account_keys:
            unidentified_transfer = await self.pool.fetchval(
                "SELECT EXISTS (SELECT 1 FROM portfolio_v2.events "
                "WHERE occurred_at>$1 AND occurred_at<=$2 "
                "AND account_key=ANY($3::text[]) AND instrument_id='' "
                "AND event_kind IN ('coupon','dividend','other') AND amount_native<>0)",
                start, end, list(affected_account_keys),
            )
            if unidentified_transfer:
                return None
        return [CashFlow(row["account_key"], row["occurred_at"], row["amount_native"],
                         row["currency"], row["amount_rub"], row["amount_usd"])
                for row in rows]

    async def operations_cover(self, accounts: tuple[Account, ...],
                               start: datetime, end: datetime) -> bool:
        keys = [a.key for a in accounts]
        rows = await self.pool.fetch(
            "SELECT account_key,success_from,success_to,flows_verified FROM portfolio_v2.sync_state "
            "WHERE account_key=ANY($1::text[])", keys,
        )
        coverage = {r["account_key"]: r for r in rows}
        covered = all(
            a.key in coverage
            and coverage[a.key]["flows_verified"]
            and coverage[a.key]["success_from"] is not None
            and coverage[a.key]["success_from"] <= start
            and coverage[a.key]["success_to"] is not None
            and coverage[a.key]["success_to"] >= end
            for a in accounts
        )
        if not covered:
            return False
        unresolved = await self.pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM portfolio_v2.events WHERE account_key=ANY($1::text[]) "
            "AND occurred_at>$2 AND occurred_at<=$3 AND event_kind='unresolved')",
            keys, start, end,
        )
        return not unresolved

    async def income(self, start: datetime, end: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        actual = await self.pool.fetch(
            "SELECT * FROM portfolio_v2.events WHERE event_kind IN ('coupon','dividend') "
            "AND occurred_at>=$1 AND occurred_at<=$2 ORDER BY occurred_at", start, end,
        )
        forecast = await self.pool.fetch(
            "SELECT * FROM portfolio_v2.forecasts WHERE payment_at>=$1 AND payment_at<=$2 "
            "ORDER BY payment_at", start, end,
        )
        return [dict(row) for row in actual], [dict(row) for row in forecast]
