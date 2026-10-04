"""Official daily FX fixings used consistently for facts and snapshots."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from xml.etree import ElementTree

import httpx

from .domain import FxFixing


def parse_cbr_daily(xml: bytes, requested: date) -> FxFixing:
    root = ElementTree.fromstring(xml)
    if root.tag != "ValCurs":
        raise ValueError("Unexpected CBR XML root")
    source_date = datetime.strptime(root.attrib["Date"], "%d.%m.%Y").date()
    if source_date > requested or (requested - source_date).days > 7:
        raise ValueError(f"CBR rate date {source_date} is unsuitable for {requested}")
    rates: dict[str, Decimal] = {"RUB": Decimal("1")}
    for row in root.findall("Valute"):
        code = (row.findtext("CharCode") or "").strip().upper()
        nominal = (row.findtext("Nominal") or "").strip()
        value = (row.findtext("Value") or "").strip().replace(",", ".")
        if code and nominal and value:
            rate = Decimal(value) / Decimal(nominal)
            if rate > 0:
                rates[code] = rate
    if "USD" not in rates:
        raise ValueError("CBR response lacks USD")
    return FxFixing(
        rate_date=datetime.combine(source_date, datetime.min.time(), timezone.utc),
        source="CBR_XML_daily",
        rub_per_currency=rates,
    )


async def fetch_cbr_daily(day: date) -> FxFixing:
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        response = await client.get(
            "https://www.cbr.ru/scripts/XML_daily.asp",
            params={"date_req": day.strftime("%d/%m/%Y")},
        )
        response.raise_for_status()
        return parse_cbr_daily(response.content, day)
