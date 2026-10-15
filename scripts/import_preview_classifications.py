"""Promote confirmed assignments from the reviewed preview into PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from dotenv import load_dotenv

from src.portfolio_v2.allocation import load_allocation_tree
from src.portfolio_v2.storage import Store


ROOT = Path(__file__).resolve().parents[1]


def _leaf_ids(node: dict) -> set[str]:
    result: set[str] = set()
    if not node.get("children"):
        result.add(str(node["id"]))
    for child in node.get("children", []):
        result |= _leaf_ids(child)
    return result


def prepare_assignments(
    preview: dict,
    ips_path: Path,
    overrides_path: Path | None = None,
) -> list[dict[str, str]]:
    config = yaml.safe_load(ips_path.read_text(encoding="utf-8"))
    leaves = _leaf_ids(config["allocation"]["tree"])
    version = str(config.get("classification_policy_version", config.get("version", "unknown")))
    rows: dict[tuple[str, str], dict[str, str]] = {}
    for position in preview.get("positions", []):
        if position.get("assignment_status") != "confirmed":
            continue
        contour = str(position.get("contour") or "").strip()
        instrument_id = str(position.get("instrument_id") or "").strip()
        bucket_id = str(position.get("candidate_bucket") or "").strip()
        if not contour or not instrument_id or bucket_id not in leaves:
            raise ValueError(
                f"Invalid confirmed assignment: {contour}/{instrument_id} -> {bucket_id}"
            )
        key = (contour, instrument_id)
        row = {
            "contour": contour,
            "instrument_id": instrument_id,
            "bucket_id": bucket_id,
            "classification_version": version,
            "source": f"reviewed_preview:{preview.get('as_of', 'unknown')}",
            "reason": str(position.get("reason") or ""),
        }
        previous = rows.get(key)
        if previous and previous["bucket_id"] != bucket_id:
            raise ValueError(f"Conflicting buckets for {contour}/{instrument_id}")
        rows[key] = row

    if overrides_path is not None and overrides_path.exists():
        overrides = yaml.safe_load(overrides_path.read_text(encoding="utf-8")) or {}
        override_version = str(overrides.get("version", "manual-unknown"))
        for item in overrides.get("assignments", []):
            contour = str(item.get("contour") or "").strip()
            instrument_id = str(item.get("instrument_id") or "").strip()
            bucket_id = str(item.get("bucket_id") or "").strip()
            if not contour or not instrument_id or bucket_id not in leaves:
                raise ValueError(
                    f"Invalid manual assignment: {contour}/{instrument_id} -> {bucket_id}"
                )
            key = (contour, instrument_id)
            rows[key] = {
                "contour": contour,
                "instrument_id": instrument_id,
                "bucket_id": bucket_id,
                "classification_version": str(item.get("classification_version") or override_version),
                "source": str(item.get("source") or "user_confirmed_override"),
                "reason": str(item.get("reason") or ""),
            }
    if not rows:
        raise ValueError("Preview contains no confirmed assignments")
    return list(rows.values())


async def _apply(rows: list[dict[str, str]]) -> int:
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        raise ValueError("DATABASE_URL is required")
    host = (urlsplit(dsn.replace("postgresql+asyncpg://", "postgresql://", 1)).hostname or "").lower()
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Classification import is limited to a local PostgreSQL host")
    store = await Store.connect(dsn)
    try:
        await store.initialize()
        return await store.set_active_classifications(rows, preserve_manual=True)
    finally:
        await store.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", type=Path, default=ROOT / "reports/classification_preview.json")
    parser.add_argument("--ips", type=Path, default=ROOT / "config/ips/personal_main.draft.yaml")
    parser.add_argument(
        "--overrides", type=Path,
        default=ROOT / "config/ips/instrument_classifications.yaml",
        help="User-confirmed mappings applied after preview rows and taking precedence over them",
    )
    parser.add_argument("--apply", action="store_true", help="Write confirmed assignments to local PostgreSQL")
    args = parser.parse_args()
    preview = json.loads(args.preview.read_text(encoding="utf-8"))
    rows = prepare_assignments(preview, args.ips, args.overrides)
    if args.apply:
        load_dotenv(ROOT / ".env", override=False)
        changed = await _apply(rows)
        print(json.dumps({"stored": len(rows), "changed": changed,
                          "snapshot_as_of": preview.get("as_of"),
                          "snapshot_complete": preview.get("snapshot_complete")},
                         ensure_ascii=False, indent=2))
    else:
        print(json.dumps({"would_store": len(rows),
                          "snapshot_as_of": preview.get("as_of"),
                          "snapshot_complete": preview.get("snapshot_complete"),
                          "by_contour": {
                              contour: sum(row["contour"] == contour for row in rows)
                              for contour in sorted({row["contour"] for row in rows})
                          }}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
