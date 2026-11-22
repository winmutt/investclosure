"""Enrich GA properties with acreage from the counties' own qPublic search.

Georgia has no statewide parcel hub, but every county's qPublic (Schneider
Corp) app exposes the same property search, and the record it resolves prints
the county's Acres figure. Plain HTTP gets Cloudflare-403, so this runs through
the scraper's camoufox browser (one shared session per batch, ~2s between
loads).

Why not the deep links ``gis_urls.get_ga_gis_url`` builds? Those guess the
app's internal ``KeyValue`` spelling per county (``space_at``/``space_count``),
and a wrong guess is invisible: Lumpkin's ``059-029`` became
``KeyValue=059++++-029``, and every load landed on app chrome reading "No
Global Search Results". Measured 2026-11-22: ``acres_source`` is NULL on **all
673 GA rows** — this enricher had never filled a single one. Searching through
the county's own form returns the record the app resolves itself, KeyValue
included, so the key is never guessed.

The measured form contract (2026-11-22; the same recipe is written up in
``ga_county_summary.md`` §Acreage enrichment):

* each county landing page (``Application.aspx?App=<County>CountyGA``) links
  its real-property SEARCH page (``PageTypeID=2``) — Barrow 635/11218/4614,
  Lumpkin 991/20168/8780, Gilmer 672/11357/4736, Union 773/11809/5644;
* that page always exposes ``ctlBodyPane_ctlNN_ctl01_txtParcelID`` and
  ``..._txtAddress`` with the SAME pane's ``..._btnSearch``. The pane index
  varies per county (Barrow 03, Lumpkin 02), so the field is found by
  placeholder and the button by pane;
* ``page.fill()`` is wiped before submit by the app's typeahead — the search
  then returns "500 Results (Maximum)" instead of the parcel — so the value is
  assigned by silent JS instead;
* the pane's button is clicked with ``el.click()``: an overlay makes
  Playwright's actionability checks time out on ``page.click()``;
* a unique hit lands straight on the record report (``PageTypeID=4``), a
  non-unique one on the result list (``PageTypeID=3``) whose rows carry the
  app's own ``KeyValue``.

A record whose Acres field is 0 or empty (Barrow's platted lots, Rabun's Sky
Valley subdivision lots) is settled as ``acres_source='qpublic_none'``: NULL
acreage on those rows is the truth, and the marker keeps the queue from
re-paying a browser session for the same answer every run.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from typing import Dict, List, Optional
from urllib.parse import unquote

from .base import camoufox_context
from .street_canon import tokens_match

logger = logging.getLogger(__name__)

QPUBLIC_BASE = "https://qpublic.schneidercorp.com"
APPS_PATH = os.path.join(os.path.dirname(__file__), "ga_qpublic_apps.json")

# qPublic's ``App=`` slug keeps each county's own capitalisation, and two GA
# counties are CamelCase: "dekalbcountyga" answers an error page, not the app.
APP_SLUGS = {"dekalb": "DeKalb", "mcintosh": "McIntosh"}

QPUBLIC_ACRES_RE = re.compile(r"Acres\s+([0-9][0-9,]*\.?\d*)", re.IGNORECASE)

# Record labels a county may use for the parcel's acreage. Only a two-cell
# label/value row counts: the improvement table below the record repeats
# "Acres" as a COLUMN header, and Lumpkin's map page printed a bogus "Acres"
# value to a body-wide regex.
ACRES_LABELS = ("acres", "total acres", "land acres", "calculated acres",
                "gross acres", "net acres")


# ---------------------------------------------------------------
# Pure parsing (unit-testable without a browser)
# ---------------------------------------------------------------

def normalize_key(value: Optional[str]) -> str:
    """Comparable form of a parcel key: upper-case letters and digits only."""
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def parse_qpublic_acres(body: str) -> Optional[float]:
    """Extract the Acres value from a qPublic report body, or None.

    Kept for the county report pages that are plain text; ``record_acres`` is
    preferred because it reads the labelled field rather than the first
    "Acres"-shaped thing on the page.
    """
    if not body:
        return None
    m = QPUBLIC_ACRES_RE.search(body)
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if not (0 <= value < 100000):
        return None
    return value


def record_fields(table_rows: List[List[str]]) -> Dict[str, str]:
    """Map the two-cell label/value rows of a qPublic record table.

    Rows with any other cell count are skipped: the record carries one-cell
    note rows ("(Note: Not to be used on legal documents)") and the wide
    improvement/sale tables underneath.
    """
    fields: Dict[str, str] = {}
    for row in table_rows or []:
        cells = [(c or "").strip() for c in row]
        if len(cells) != 2 or not cells[0] or not cells[1]:
            continue
        label = re.sub(r"\s+", " ", cells[0]).strip().lower()
        if label not in fields:
            fields[label] = re.sub(r"\s+", " ", cells[1]).strip()
    return fields


def record_acres(fields: Dict[str, str]) -> Optional[float]:
    """The record's acreage, or None when the county publishes none/zero."""
    for label in ACRES_LABELS:
        raw = (fields or {}).get(label)
        if not raw:
            continue
        m = re.search(r"\d[\d,]*\.?\d*", raw)
        if not m:
            continue
        try:
            value = float(m.group(0).replace(",", ""))
        except ValueError:
            continue
        if 0 < value < 100000:
            return value
        # A published 0 is an answer about that label, not about the parcel:
        # keep looking at the other acreage labels, and if none carries a
        # positive figure the caller settles the row via
        # ``record_publishes_no_acres``.
    return None


def record_publishes_no_acres(fields: Dict[str, str]) -> bool:
    """True when the record HAS an Acres field and its value is 0/blank."""
    raw = (fields or {}).get("acres")
    if raw is None:
        return False
    m = re.search(r"\d[\d,]*\.?\d*", raw)
    if m:
        try:
            return float(m.group(0).replace(",", "")) == 0.0
        except ValueError:
            return False
    return True


def _street_tokens(address: Optional[str]) -> List[str]:
    """Comparable street tokens: the part before the city, letters only."""
    head = re.split(r"[,;]", address or "")[0]
    return [t for t in re.sub(r"[^A-Za-z ]", " ", head).upper().split()
            if len(t) > 1]


def address_matches(ours: Optional[str], theirs: Optional[str]) -> bool:
    """Do two street addresses name the same place?

    The house number must agree when we have one (qPublic lists a whole road
    when the number is absent, and a neighbouring lot is a WRONG acreage), and
    the street words must agree on canonical spelling ("MTN"/"MOUNTAIN").
    """
    our_num = re.match(r"\s*(\d+)", ours or "")
    their_num = re.match(r"\s*(\d+)", theirs or "")
    if our_num:
        if not their_num or our_num.group(1) != their_num.group(1):
            return False
    elif not their_num:
        # Neither side has a number: only a road name. Never bind on that.
        return False
    return tokens_match(_street_tokens(ours), _street_tokens(theirs))


def row_key(row: dict) -> str:
    """The app's own parcel key for a result row, from its KeyValue link."""
    m = re.search(r"[?&]KeyValue=([^&]*)", unquote(row.get("href") or ""))
    return normalize_key(m.group(1)) if m else ""


def row_text(row: dict) -> str:
    """All cells of a result row, joined (the county's own spelling)."""
    return " ".join((c or "").strip() for c in row.get("cells") or [])


def _address_cell(row: dict, house_num: str) -> str:
    """The row cell that carries a situs address with our house number."""
    for cell in row.get("cells") or []:
        cell = (cell or "").strip()
        if re.match(rf"^0*{re.escape(house_num)}(?![0-9])\s", cell):
            return cell
    return ""


def result_rows(table_rows: List[dict]) -> List[dict]:
    """Parse a qPublic result list into rows that carry a KeyValue link.

    ``table_rows`` is the DOM dump: ``{"cells": [...], "href": ...}`` per row.
    Rows without a KeyValue link (headers, notes, pagination) are dropped. No
    column order is assumed: the county's own ``KeyValue`` is what identifies
    a row's parcel, and the address is found by the cell that carries the
    house number — Gilmer adds a "Select" column and renders "Parcel ID" and
    "Alternate ID" as two headers over one cell, so indexing by header breaks.
    """
    return [{"cells": [(c or "").strip() for c in row.get("cells") or []],
             "href": row.get("href")}
            for row in table_rows or [] if row.get("href")]


def pick_result_row(rows: List[dict], parcel: Optional[str] = None,
                    address: Optional[str] = None) -> Optional[dict]:
    """Choose the result row that IS our property, or None.

    The app's own KeyValue beats everything (it is the county's spelling of
    the parcel key, not ours). With an address, a single row is enough (the
    county's own form filtered it); several rows need the house number and the
    canonical street words to agree on one cell, and an ambiguous list is
    refused — an acreage from the wrong lot is worse than none.
    """
    rows = rows or []
    if not rows:
        return None
    want = normalize_key(parcel)
    if want:
        for row in rows:
            if row_key(row) == want:
                return row
        # No KeyValue equal to ours (some counties key differently): accept a
        # row whose text carries our key, and let the record's own "Parcel
        # Number" field be the final say.
        hits = [r for r in rows if want and want in normalize_key(row_text(r))]
        return hits[0] if len(hits) == 1 else None
    if len(rows) == 1:
        return rows[0]
    house = re.match(r"\s*(\d+)", address or "")
    if house:
        hits = [r for r in rows
                if address_matches(address, _address_cell(r, house.group(1)))]
        return hits[0] if len(hits) == 1 else None
    return None


def clean_detail_url(url: Optional[str]) -> str:
    """Drop qPublic's per-request ``&Q=`` nonce from a parcel-report link."""
    return re.sub(r"&Q=\d+", "", url or "")


# ---------------------------------------------------------------
# Browser layer
# ---------------------------------------------------------------

_FIELD_JS = """(kind) => {
  const want = kind === 'address' ? /address/i : /parcel number|parcel bill/i;
  for (const el of document.querySelectorAll('input[type=text]')) {
      if (!(el.offsetWidth || el.offsetHeight)) continue;
      if (!want.test(el.placeholder || '')) continue;
      const m = /^(ctlBodyPane_ctl\\d+_ctl\\d+)_/.exec(el.id || '');
      if (m) return { id: el.id, pane: m[1] };
  }
  return null;
}"""

_ASSIGN_JS = """(a) => {
  const el = document.getElementById(a.id);
  if (!el) return false;
  // Silent assignment on purpose: page.fill() dispatches the input events
  // that the app's typeahead answers by clearing the field, and the search
  // then posts an empty value ("500 Results (Maximum)" instead of the parcel).
  el.value = a.v;
  return el.value === a.v;
}"""

_CLICK_JS = """(id) => {
  const el = document.getElementById(id);
  if (!el) return false;
  el.click();
  return true;
}"""

_ROWS_JS = """() => Array.from(document.querySelectorAll('table tr')).slice(0, 80)
  .map(tr => ({
      cells: Array.from(tr.querySelectorAll('th,td')).map(
          td => (td.textContent || '').trim().replace(/\\s+/g, ' ')),
      href: (tr.querySelector('a[href*="KeyValue"]') || {}).href || null
  }))"""

_TABLE_JS = """() => Array.from(document.querySelectorAll('table tr')).slice(0, 80)
  .map(tr => Array.from(tr.querySelectorAll('th,td')).map(
      td => (td.textContent || '').trim().replace(/\\s+/g, ' ')))"""

_LINKS_JS = """() => Array.from(document.querySelectorAll('a[href*="PageTypeID=2"]'))
  .map(a => a.getAttribute('href'))"""


def load_apps() -> Dict[str, dict]:
    """Discovered per-county qPublic apps (``scraper/ga_qpublic_apps.json``).

    A county recorded ``found: false`` has no parcel-search page at all, which
    is how the queue keeps an unanswerable county from eating the whole batch.
    """
    try:
        with open(APPS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("qPublic app registry unreadable (%s); rediscovering", exc)
        return {}


def save_apps(apps: Dict[str, dict]) -> None:
    tmp = APPS_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(apps, fh, indent=1, sort_keys=True)
        os.replace(tmp, APPS_PATH)
    except OSError as exc:
        logger.warning("qPublic app registry not saved: %s", exc)


def search_page_candidates(hrefs: List[str]) -> List[str]:
    """Absolute URLs of the ``PageTypeID=2`` pages a county app links.

    Takes the anchor hrefs (the DOM's ``getAttribute`` values, or raw HTML
    entities — both are decoded here). ``PageID=0`` entries are the app's own
    map landing, which has no search form, so they are dropped.
    """
    out: List[str] = []
    for href in hrefs or []:
        href = unquote((href or "").replace("&amp;", "&"))
        if "PageTypeID=2" not in href or "PageID=0" in href:
            continue
        url = href if href.startswith("http") else QPUBLIC_BASE + (
            href if href.startswith("/") else "/" + href)
        if url not in out:
            out.append(url)
    return out


def app_landing_url(county: str) -> str:
    """A county's qPublic landing URL, in the app's own capitalisation."""
    slug = APP_SLUGS.get((county or "").strip().lower(),
                         (county or "").title())
    return f"{QPUBLIC_BASE}/Application.aspx?App={slug}CountyGA"


def discover_county(page, county: str) -> Optional[dict]:
    """Find a county's parcel-search page from its landing page.

    Three outcomes, deliberately:

    * ``{"app_id", "layer_id", "search_page_id", "search_url", "found": True}``
      — the parcel/address search was located;
    * ``{"found": False}`` — the county's app rendered its pages and none of
      them is a parcel/address search;
    * ``None`` — discovery could not answer: the landing did not load, linked no
      app pages at all, or every candidate page failed to render.

    Only the first two are facts about the county, so only they are cached.
    Blacklisting a county because one browser session had a bad minute would
    take it off the queue silently and forever.

    The parcel/address FIELD decides, never the link label: Barrow labels its
    search "Search", Gilmer "Real Property Search", Lumpkin "Property Search".
    """
    url = app_landing_url(county)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(8000)
        candidates = search_page_candidates(page.evaluate(_LINKS_JS) or [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("qPublic landing failed for %s: %s", county, exc)
        return None
    if not candidates:
        # A county with no app answers the same way a page that never rendered
        # does, so this is not an answer about the county.
        logger.warning("qPublic %s: landing linked no app pages", county)
        return None
    rendered = 0
    for candidate in candidates[:5]:
        try:
            page.goto(candidate, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(6000)
        except Exception as exc:  # noqa: BLE001
            logger.debug("qPublic %s candidate %s failed: %s",
                         county, candidate, exc)
            continue
        rendered += 1
        # A county that publishes only an address field is still usable for the
        # mountain rows, which carry no PIN.
        if (page.evaluate(_FIELD_JS, "pin")
                or page.evaluate(_FIELD_JS, "address")):
            ids = re.search(
                r"AppID=(\d+).*?LayerID=(\d+).*?PageID=(\d+)", candidate)
            found = {
                "app_id": ids.group(1) if ids else "",
                "layer_id": ids.group(2) if ids else "",
                "search_page_id": ids.group(3) if ids else "",
                "search_url": candidate,
                "found": True,
            }
            logger.info("qPublic %s: search page %s", county, candidate)
            return found
    if not rendered:
        logger.warning("qPublic %s: none of %d page(s) rendered", county,
                       len(candidates))
        return None
    logger.info("qPublic %s: %d page(s) rendered, none is a parcel search",
                county, rendered)
    return {"found": False, "reason": "no_search_page"}


def fetch_record(page, search_url: str, kind: str, value: str,
                 parcel: Optional[str] = None,
                 address: Optional[str] = None) -> Optional[dict]:
    """Run one search on a county's own form; return its verified record.

    ``{"fields": {...}, "acres": float|None, "url": str}`` — or None when no
    record was reached or the record reached is not the property asked for.
    """
    try:
        page.goto(search_url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(7000)
        field = page.evaluate(_FIELD_JS, kind)
        if not field:
            logger.warning("qPublic search page has no %s field: %s",
                           kind, search_url)
            return None
        if not page.evaluate(_ASSIGN_JS, {"id": field["id"], "v": value}):
            logger.warning("qPublic could not set %s=%r", field["id"], value)
            return None
        button = f"{field['pane']}_btnSearch"
        if not page.evaluate(_CLICK_JS, button):
            logger.warning("qPublic search button missing: %s", button)
            return None
        page.wait_for_timeout(10000)
    except Exception as exc:  # noqa: BLE001
        logger.warning("qPublic search failed (%s %s): %s", kind, value, exc)
        return None

    if "PageTypeID=4" not in page.url:
        rows = result_rows(page.evaluate(_ROWS_JS))
        row = pick_result_row(rows, parcel=parcel, address=address)
        if not row:
            logger.info("qPublic %s %r: %d result row(s), none matched",
                        kind, value, len(rows))
            return None
        try:
            page.goto(row["href"], wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(8000)
        except Exception as exc:  # noqa: BLE001
            logger.warning("qPublic record load failed (%s): %s",
                           row["href"][:120], exc)
            return None

    fields = record_fields(page.evaluate(_TABLE_JS))
    if not fields:
        return None
    if parcel and normalize_key(fields.get("parcel number")):
        if normalize_key(fields["parcel number"]) != normalize_key(parcel):
            logger.warning(
                "qPublic returned parcel %r for %r — refused",
                fields["parcel number"], parcel)
            return None
    elif address:
        if not address_matches(address, fields.get("location address")):
            logger.warning(
                "qPublic returned address %r for %r — refused",
                fields.get("location address"), address)
            return None
    return {"fields": fields, "acres": record_acres(fields),
            "url": clean_detail_url(page.url)}


# ---------------------------------------------------------------
# Database pass
# ---------------------------------------------------------------

_NEEDS_SQL = (
    "state='GA' AND status='active' AND acres IS NULL"
    " AND (acres_source IS NULL OR acres_source NOT IN"
    "     ('qpublic', 'qpublic_none'))"
    " AND ((parcel_number IS NOT NULL AND TRIM(parcel_number) != '')"
    "      OR (address IS NOT NULL AND TRIM(address) != ''))"
)


def enrich_db(limit: Optional[int] = None) -> Dict[str, int]:
    """Fill NULL acreage on active GA rows from the counties' qPublic search.

    ``limit`` caps how many rows one pass attempts and defaults to
    ``config.GA_ENRICH_LIMIT``: a row costs a browser round trip of ~20-30 s,
    so the post-run hook has to stay inside the scrape window and the queue
    drains over several runs instead.

    Counties with no parcel search are excluded BEFORE the row budget is taken
    (an unanswerable county used to fill the whole ``LIMIT`` window every run,
    which is how 94 rentals rows starved the 7 rows that could be answered),
    and rows whose record publishes no acreage are settled once with
    ``acres_source='qpublic_none'`` instead of being retried forever.
    """
    from . import db as D
    from .config import RENTAL_SOURCES, config

    if limit is None:
        limit = config.GA_ENRICH_LIMIT
    conn = D._ensure_db(config.db_path)
    stats = {"processed": 0, "updated": 0, "no_acres_published": 0,
             "unmatched": 0, "failed": 0, "skipped_no_app": 0}

    apps = load_apps()
    blocked = [c for c, meta in apps.items() if not meta.get("found")]

    county_counts = conn.execute(
        f"SELECT county, COUNT(*) AS n FROM properties WHERE {_NEEDS_SQL}"
        " GROUP BY county").fetchall()
    allowed = [(r["county"] or "").strip().lower() for r in county_counts
               if (r["county"] or "").strip().lower() not in blocked]
    stats["skipped_no_app"] = sum(
        r["n"] for r in county_counts
        if (r["county"] or "").strip().lower() in blocked)
    if stats["skipped_no_app"]:
        logger.info("qPublic: %d row(s) in counties with no parcel search "
                    "skipped up front", stats["skipped_no_app"])
    if not allowed:
        conn.close()
        return stats

    placeholders = ",".join("?" * len(allowed))
    rows = conn.execute(
        "SELECT id, source, county, parcel_number, address "
        f"FROM properties WHERE {_NEEDS_SQL} AND lower(county) IN ({placeholders}) "
        "ORDER BY id LIMIT ?",
        allowed + [limit],
    ).fetchall()
    todo = [dict(r) for r in rows]
    if not todo:
        conn.close()
        return stats

    with camoufox_context() as page:
        page.set_viewport_size({"width": 1600, "height": 1000})
        unknown: set = set()   # discovery could not answer — this pass only
        for prop in todo:
            pid = prop["id"]
            county = (prop["county"] or "").strip().lower()
            parcel = (prop["parcel_number"] or "").strip()
            address = (prop["address"] or "").strip()
            stats["processed"] += 1

            meta = apps.get(county)
            if meta is None:
                if county in unknown:
                    # Already asked this pass; do not re-pay a landing load for
                    # every row of the same county.
                    stats["failed"] += 1
                    continue
                meta = discover_county(page, county)
                time.sleep(random.uniform(1.0, 2.0))
                if meta is None:
                    unknown.add(county)
                    stats["failed"] += 1
                    continue
                apps[county] = meta
                save_apps(apps)
            if not meta.get("found"):
                stats["skipped_no_app"] += 1
                continue

            # The notice's PIN wins; a row with only a street address (most
            # mountain-county notices print no PIN) goes through the county's
            # address search, which the same form provides.
            if parcel:
                kind, value = "pin", parcel
            else:
                kind, value = "address", re.split(r"[,;]", address)[0].strip()
                if not value:
                    stats["unmatched"] += 1
                    continue

            try:
                rec = fetch_record(page, meta["search_url"], kind, value,
                                   parcel=parcel or None,
                                   address=address or None)
            except Exception as exc:  # noqa: BLE001
                logger.warning("qPublic enrich failed #%s %s %s: %s",
                               pid, county, value, exc)
                rec = None
                stats["failed"] += 1
                time.sleep(random.uniform(2.0, 3.5))
                continue
            time.sleep(random.uniform(2.0, 3.5))

            if not rec:
                stats["unmatched"] += 1
                continue

            updates: Dict[str, object] = {"gis_url": rec["url"],
                                          "acres_source": "qpublic"}
            acres = rec.get("acres")
            if not acres:
                if not record_publishes_no_acres(rec["fields"]):
                    # Reached a record but no Acres field at all: leave the
                    # row open — a page that failed to render looks the same,
                    # and a wrong verdict is worse than a retry.
                    stats["unmatched"] += 1
                    continue
                # The county says this parcel has no acreage (platted lots:
                # Barrow's subdivisions, Rabun's Sky Valley). Settled, not
                # skipped, so the queue stops re-paying a browser session for
                # the same answer every run.
                updates["acres_source"] = "qpublic_none"
                stats["no_acres_published"] += 1
            else:
                updates["acres"] = acres

            set_parts = ", ".join(f"{k}=?" for k in updates)
            conn.execute(f"UPDATE properties SET {set_parts} WHERE id=?",
                         list(updates.values()) + [pid])
            conn.commit()
            if acres:
                stats["updated"] += 1
                logger.info("qPublic acres #%s %s %s -> %s",
                            pid, county, value, acres)
                # A row inserted with unknown acreage can resolve BELOW the
                # threshold here, exactly like the NC and KGIS fills. Rentals
                # rows are exempt: a 0.4 ac town lot IS that product.
                if (acres < config.MIN_ACRES
                        and prop.get("source") not in RENTAL_SOURCES):
                    D.archive_property(conn, pid,
                                       reason="below_min_acres (qpublic)",
                                       archived_by="system")
                    logger.info("Archived #%s: %sac < MIN_ACRES %s",
                                pid, acres, config.MIN_ACRES)

    conn.close()
    logger.info("qPublic enrichment: %s filled, %s no-acres-published, "
                "%s linked-only, %s unmatched, %s no-app, %s failed",
                stats["updated"], stats["no_acres_published"],
                stats.get("linked", 0), stats["unmatched"],
                stats["skipped_no_app"], stats["failed"])
    return stats


def fetch_qpublic_acres(page, county: str, parcel: str) -> Optional[float]:
    """Load one county report page in an existing camoufox page; parse acres.

    Kept for callers that already hold a page and only need the number;
    ``enrich_db`` uses ``fetch_record`` (verified record, app-supplied key).
    """
    from .gis_urls import get_ga_gis_url

    try:
        page.goto(get_ga_gis_url(county, parcel),
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(12000)
        body = page.inner_text("body") or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("qPublic load failed for %s %s: %s", county, parcel, exc)
        return None
    return parse_qpublic_acres(body)


if __name__ == "__main__":
    import sys

    print(enrich_db(int(sys.argv[1]) if len(sys.argv) > 1 else None))
