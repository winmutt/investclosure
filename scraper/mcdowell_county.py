"""McDowell County (NC) tax foreclosure scraper.

Live sale tables at (mcdowellgov.com redirects to mcdowellnc.gov):

    https://mcdowellnc.gov/departments/tax-collections/tax-foreclosures/upcoming-tax-foreclosure-sales

Three tables: UPCOMING TAX FORECLOSURE SALES (sale date/time, parcel,
opening bid, file), UPSET BID PERIOD (original sale date, upset end,
parcel, highest bid, file) and PENDING TAX FORECLOSURES (parcel, file).
Parcel cells may bundle two parcels ("1739-00-31-2535 / 1739-00-21-7533")
— each parcel becomes its own row sharing case/bid/dates.

No captcha, no Turnstile. NC OneMap enrichment happens in the pipeline
(run.py auto-enrich) via the parcel number.
"""
from __future__ import annotations

import logging
import re

from .base import BaseScraper, PropertyData
from . import county_static as cs
from .rawlog import log_raw

logger = logging.getLogger(__name__)

SALES_URL = (
    "https://mcdowellnc.gov/departments/tax-collections/"
    "tax-foreclosures/upcoming-tax-foreclosure-sales"
)
COUNTY = "McDowell"

# Header keywords identifying each of the three sale tables.
UPCOMING_KEYS = ("sale date", "opening bid")
UPSET_KEYS = ("upset", "highest bid")
PENDING_KEYS = ("pending",)

PARCEL_CELL_RE = re.compile(r"\d{3,5}-\d{2}-\d{2,3}-\d{3,5}")


def _split_parcels(cell: str) -> list[str]:
    """Split a parcel cell that may bundle two parcels with '/'."""
    return PARCEL_CELL_RE.findall(cell or "")


def _table_kind(headers: list[str]) -> str:
    low = " ".join(headers).lower()
    if "upset" in low or "highest bid" in low:
        return "upset"
    if "opening bid" in low or "sale date" in low:
        return "upcoming"
    return "pending"


class McDowellCountyScraper(BaseScraper):
    """Scrape McDowell County upcoming/upset/pending tax foreclosure tables."""

    SOURCE_NAME = "mcdowell_county"
    BASE_URL = SALES_URL

    def __init__(self, delay_range: tuple[float, float] = (1.0, 2.0)):
        super().__init__(delay_range=delay_range)

    def scrape(self) -> list[PropertyData]:
        logger.info("Fetching McDowell County foreclosure sales ...")
        try:
            tables = cs.extract_tables(SALES_URL)
        except Exception as e:
            logger.error("McDowell County fetch failed: %s", e)
            return []

        properties: list[PropertyData] = []
        for table in tables:
            kind = _table_kind(table["headers"])
            for row in table["rows"]:
                text = " | ".join(row)
                if re.search(r"no .*foreclosure|no .*scheduled|at this time", text, re.I):
                    continue
                for prop in self._parse_row(kind, table["headers"], row):
                    if prop:
                        properties.append(prop)

        if not properties:
            raw = "\n".join(" | ".join(r) for t in tables for r in t["rows"])[:2000]
            cs.log_kept_empty(
                self.SOURCE_NAME, COUNTY,
                "no parcel rows in sale tables", raw, SALES_URL,
            )
            logger.info("McDowell County: no sale rows")
        else:
            logger.info("McDowell County: %d parcel rows", len(properties))
        return properties

    def _parse_row(self, kind: str, headers: list[str], row: list[str]) -> list[PropertyData]:
        cells = {h.lower(): (row[i] if i < len(row) else "") for i, h in enumerate(headers)}
        blob = " | ".join(row)
        parcels = _split_parcels(blob)
        if not parcels:
            log_raw(
                self.SOURCE_NAME, listing_id=f"unparsed_{cs.content_hash(blob)}",
                county=COUNTY, state="NC", decision="dropped_no_key",
                reason=f"{kind} row without parcel: {blob[:200]}",
                raw_text=blob, url=SALES_URL,
            )
            return []

        def pick(*names: str) -> str:
            for n in names:
                for h, v in cells.items():
                    if n in h and v:
                        return v
            return ""

        case = cs.find_case(pick("file number", "file"))
        price = cs.parse_money(pick("opening bid", "highest bid", "bid"))
        sale_date = cs.to_iso_date(pick("sale date", "original sale date"))
        upset_end = cs.to_iso_date(pick("upset", "period ends", "ends"))
        # Upset rows are dated by the 10-day window end (the actionable date);
        # upcoming rows by their sale date.
        action_date = (upset_end or sale_date) if kind == "upset" else sale_date

        props: list[PropertyData] = []
        for parcel in parcels:
            lid = f"mcdowell_{parcel}_{(case or 'nocase')}"
            if kind == "upset":
                desc = (f"[McDowell Co] UPSET-BID parcel {parcel} — highest bid "
                        f"${price:,.2f}" if price else f"[McDowell Co] UPSET-BID parcel {parcel}")
                if upset_end:
                    desc += f", upset ends {upset_end}"
                upset_text = f"${price:,.2f}" if price else None
            elif kind == "pending":
                desc = f"[McDowell Co] PENDING foreclosure parcel {parcel}"
                upset_text = None
            else:
                desc = (f"[McDowell Co] tax sale parcel {parcel} — opening bid "
                        f"${price:,.2f}" if price else f"[McDowell Co] tax sale parcel {parcel}")
                upset_text = None
            if case:
                desc += f" (File {case})"
            log_raw(
                self.SOURCE_NAME, listing_id=lid, county=COUNTY, state="NC",
                decision="kept_tax", reason=f"{kind} table row",
                raw_text=blob, url=SALES_URL,
            )
            props.append(cs.build_tax_row(
                source=self.SOURCE_NAME, listing_id=lid, url=SALES_URL,
                county=COUNTY, parcel=parcel, price=price,
                auction_date=action_date,
                upset_bid=upset_text, court_case=case,
                description=desc, raw_text=blob, extra_key=kind,
            ))
        return props
