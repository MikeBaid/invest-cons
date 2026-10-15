"""Run the isolated first-release portfolio bot or perform one synchronization."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from tradernet import Tradernet

from src.portfolio_v2.adapters import FreedomAdapter, TInvestAdapter
from src.portfolio_v2.bot import PortfolioBot
from src.portfolio_v2.service import PortfolioService
from src.portfolio_v2.policy import InvestmentScopePolicy
from src.portfolio_v2.scheduler import run_daily_refresh
from src.portfolio_v2.runtime_config import RuntimeConfig
from src.portfolio_v2.storage import Store
from src.tinvest_certificates import try_configure_tinvest_grpc_ssl_roots


ROOT = Path(__file__).resolve().parent


def _list(raw: str) -> list[str]:
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("Expected JSON array")
        return [str(item).strip() for item in data if str(item).strip()]
    return [item.strip() for item in raw.split(",") if item.strip()]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Isolated portfolio V2 bot")
    parser.add_argument("--refresh-once", action="store_true",
                        help="Synchronize once without starting Telegram polling")
    parser.add_argument("--disable-scheduler", action="store_true",
                        help="Start the bot without its scheduled refresh")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    try_configure_tinvest_grpc_ssl_roots(
        extra_root_certs_raw=os.getenv("TINVEST_GRPC_EXTRA_ROOT_CERTS", "").strip(),
        bundle_path_raw=os.getenv("TINVEST_GRPC_ROOTS_BUNDLE_PATH", "").strip(),
        base_dir=ROOT,
    )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    tokens = _list(os.getenv("INVEST_TOKEN_READ", ""))
    labels = _list(os.getenv("INVEST_CLIENT_LABELS", ""))
    owner_map = json.loads(os.getenv("PORTFOLIO_V2_OWNER_MAP", "{}"))
    freedom_label = os.getenv("FREEDOM_CLIENT_LABEL", "Freedom")
    if not tokens or not os.getenv("FREEDOM_TRADERNET_PUBLIC_KEY") or not os.getenv("FREEDOM_TRADERNET_PRIVATE_KEY"):
        raise ValueError("Both T-Invest and Freedom read credentials are required")
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise ValueError("DATABASE_URL is required")
    token = os.getenv("PORTFOLIO_V2_TELEGRAM_TOKEN", "").strip()
    allowed = {int(value) for value in _list(os.getenv("PORTFOLIO_V2_TELEGRAM_IDS", ""))}
    classification_enabled = _env_bool("PORTFOLIO_V2_CLASSIFICATION_ENABLED")
    classification_admin_ids = {
        int(value) for value in _list(os.getenv("PORTFOLIO_V2_CLASSIFICATION_ADMIN_IDS", ""))
    }
    if classification_enabled and not classification_admin_ids:
        raise ValueError("PORTFOLIO_V2_CLASSIFICATION_ADMIN_IDS is required when classification is enabled")
    if classification_enabled and not classification_admin_ids <= allowed:
        raise ValueError("Classification admins must be in PORTFOLIO_V2_TELEGRAM_IDS")
    if not args.refresh_once and (not token or not allowed):
        raise ValueError("PORTFOLIO_V2_TELEGRAM_TOKEN and PORTFOLIO_V2_TELEGRAM_IDS are required")
    runtime = RuntimeConfig.load(ROOT / "config/runtime.yaml")
    schedule = runtime.schedule
    store = await Store.connect(dsn)
    try:
        await store.initialize()
        tinkoff = TInvestAdapter(
            tokens, labels, owner_map,
            set(_list(os.getenv("PORTFOLIO_V2_EXCLUDE_ACCOUNTS", ""))),
        )
        freedom = FreedomAdapter(
            os.environ["FREEDOM_TRADERNET_PUBLIC_KEY"],
            os.environ["FREEDOM_TRADERNET_PRIVATE_KEY"],
            os.getenv("FREEDOM_TRADERNET_DOMAIN", Tradernet.DOMAIN),
            freedom_label,
            owner_map.get(freedom_label, freedom_label),
        )
        policy = InvestmentScopePolicy.load(ROOT / "config/ips/portfolio_exclusions.yaml")
        service = PortfolioService(
            store, tinkoff, freedom, scope_policy=policy,
            ips_path=ROOT / "config/ips/personal_main.draft.yaml",
            calendar_timezone=schedule.timezone,
            initial_history_days=runtime.initial_history_days,
            forecast_horizon_days=runtime.forecast_horizon_days,
            historical_fx_max_age_calendar_days=runtime.historical_fx_max_age_calendar_days,
        )
        if args.refresh_once:
            result = await service.refresh()
            print(json.dumps({
                "run_id": result.run_id,
                "complete": result.snapshot.complete,
                "accounts": len(result.snapshot.accounts),
                "account_errors": result.snapshot.account_errors,
                "valuation_errors": result.snapshot.valuation_errors,
                "operation_status": result.operation_status,
                "forecast_status": result.forecast_status,
            }, ensure_ascii=False, indent=2))
            if not result.snapshot.complete or any(
                status != "ok" for status in result.operation_status.values()
            ):
                raise SystemExit(2)
        else:
            scheduler_enabled = schedule.enabled and not args.disable_scheduler
            scheduler_task = asyncio.create_task(
                run_daily_refresh(service, schedule), name="daily-portfolio-refresh"
            ) if scheduler_enabled else None
            try:
                await PortfolioBot(
                    token, allowed, service,
                    blur_amounts=_env_bool("PORTFOLIO_V2_BLUR_AMOUNTS"),
                    classification_enabled=classification_enabled,
                    classification_admin_ids=classification_admin_ids,
                ).run()
            finally:
                if scheduler_task is not None:
                    scheduler_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await scheduler_task
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
