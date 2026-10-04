"""Opt-in PostgreSQL roundtrip; restricted to the local test instance."""

import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from dotenv import dotenv_values

from src.portfolio_v2.domain import Account, FxFixing, Position, value_portfolio
from src.portfolio_v2.storage import Store
from src.portfolio_v2.service import PortfolioService


def test_local_postgres_snapshot_roundtrip() -> None:
    if os.getenv("PORTFOLIO_V2_TEST_DB") != "1":
        pytest.skip("Set PORTFOLIO_V2_TEST_DB=1 for local PostgreSQL integration test")
    dsn = dotenv_values(Path(__file__).resolve().parents[1] / ".env").get("DATABASE_URL", "")
    if urlsplit(dsn).hostname not in {"localhost", "127.0.0.1"}:
        pytest.fail("Only local PostgreSQL is allowed")

    async def check() -> None:
        store = await Store.connect(dsn)
        run_id = uuid4()
        try:
            await store.initialize()
            now = datetime.now(timezone.utc)
            account = Account("tinkoff", f"test-{run_id}", "test-owner")
            fx = FxFixing(now, "test", {"RUB": Decimal("1"), "USD": Decimal("80")})
            snapshot = value_portfolio(
                as_of=now, accounts=[account],
                positions=[Position(account.key, "CASH.RUB", "Cash", "cash",
                                    Decimal("100"), "RUB", Decimal("1"))],
                fx=fx, account_errors=[f"{account.key}:sample_error"],
            )
            await store.save_snapshot(run_id, snapshot, fx)
            rows = await store.latest_snapshots(limit=1)
            assert rows[0].accounts == snapshot.accounts
            assert rows[0].account_errors == snapshot.account_errors
            assert rows[0].positions[0].value_usd == Decimal("1.25")
            service = PortfolioService(
                store, None, None, historical_fx_max_age_calendar_days=14,
            )
            assert await service.portfolio() is not None
            assert (await service.performance()).latest is not None
            data = await service.income()
            assert "forecast" in data and "actual" in data
        finally:
            await store.pool.execute("DELETE FROM portfolio_v2.positions WHERE run_id=$1", run_id)
            await store.pool.execute("DELETE FROM portfolio_v2.refreshes WHERE run_id=$1", run_id)
            await store.close()

    asyncio.run(check())
