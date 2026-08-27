"""Extract owner / acres / address from GA QPublic parcel pages.

QPublic's application host (schneidercorp.com) returns HTTP 403 to plain bots,
but the project's stealth browser (camoufix / camoufox) bypasses Cloudflare,
so we can pull the parcel detail page and parse:

  * Owner name           -> ``owner_name``
  * Tax acres            -> ``acres`` (only when currently unknown)
  * Location / situs     -> ``address`` (only when current address is the
    (legal description)      ``Parcel <n>`` placeholder or empty)

The detail page (``PageTypeID=4``) carries the ``Location Address`` / ``Legal
Description``; for rural parcels there is no street situs, so the legal
description road name is used as the best-effort address.

Run:
    python3 scraper/backfill_ga_addresses.py            # all GA, fetch via browser
    python3 scraper/backfill_ga_addresses.py --county towns --limit 5
"""
import argparse
import random
import re
import sqlite3
import time
from urllib.parse import quote

from scraper import db as D
from scraper.config import config
from scraper.base import camoufox_context
from scraper.gis_urls import GA_QPUBLIC_APPS


def _detail_url(county: str, parcel: str) -> str:
    """Build the QPublic *detail* (PageTypeID=4) URL for a parcel."""
    c = (county or "").strip().lower()
    cfg = GA_QPUBLIC_APPS.get(c)
    if not cfg:
        return None
    page = next((p for p in cfg["pages"] if p["page_type_id"] == 4), cfg["pages"][0])
    kv = (parcel or "").strip()
    if c == "towns" and kv.upper().startswith("YH") and " " not in kv and len(kv) > 4:
        kv = kv[:4] + " " + kv[4:]
    kv = quote(kv).replace("%20", "+")
    return (
        "https://qpublic.schneidercorp.com/Application.aspx"
        f"?AppID={cfg['app_id']}&LayerID={cfg['layer_id']}"
        f"&PageTypeID={page['page_type_id']}&PageID={page['page_id']}&KeyValue={kv}"
    )


_ADDR_RE = re.compile(
    r"(GA\s*\d{5}|ROAD|STREET|AVE|DRIVE|LANE|HWY|HIGHWAY|COURT|BLVD|PKWY|TRAIL|"
    r"RTE\b|BOX|CIRCLE|PLACE|PIKE|WAY|LOOP|TRCE|Hwy)",
    re.IGNORECASE,
)


def _parse_qpublic(txt: str):
    lines = [l.strip() for l in txt.split("\n") if l.strip()]
    owner = None
    acres = None
    situs = None
    legal = None
    for i, l in enumerate(lines):
        low = l.lower()
        if low == "owner":
            if i + 1 < len(lines):
                cand = lines[i + 1]
                if cand and not re.match(
                    r"(legal|acres|class|tax |landlot|rural|sales|valuation|"
                    r"parcel|location|zoning|millage|neighborhood|homestead)",
                    cand, re.I):
                    owner = cand
        if low.startswith("legal description"):
            after = re.split(r"legal description", l, flags=re.I)[1]
            legal = re.split(r"\(Note", after, 1)[0].strip()
        if (low == "acres" or low.startswith("acres\t") or low.startswith("acres ")) and acres is None:
            tail = l.split("\t")[-1] if "\t" in l else l
            m = re.search(r"(\d+(?:\.\d+)?)", tail)
            if m:
                acres = float(m.group(1))
        if low.startswith("location address"):
            for j in range(i + 1, min(i + 5, len(lines))):
                if _ADDR_RE.search(lines[j]):
                    situs = lines[j]
                    break
    return owner, acres, situs, legal


def _safe_inner_text(page) -> str:
    """Read rendered page text, tolerating transient blank-body states."""
    expr = ("() => { var b = document.body; var d = b || document.documentElement; "
            "return (d && d.innerText) ? d.innerText : ''; }")
    last = ""
    for _ in range(3):
        try:
            last = page.evaluate(expr) or ""
        except Exception:
            last = ""
        if isinstance(last, str) and len(last) > 20:
            return last
        page.wait_for_timeout(1500)
    return last


def run(limit: int = None, county: str = None) -> dict:
    conn = D._ensure_db(config.db_path)
    q = ("SELECT id, county, parcel_number, address, owner_name, acres FROM properties "
         "WHERE source='ga_publicnotice'")
    params: list = []
    if county:
        q += " AND county=?"
        params.append(county)
    rows = conn.execute(q, params).fetchall()
    if limit:
        rows = rows[:limit]

    updated = skipped = errors = 0
    with camoufox_context(proxy=None) as page:
        for r in rows:
            pid, co, parcel, addr, owner0, acres0 = r
            url = _detail_url(co, parcel or "")
            if not url:
                skipped += 1
                continue
            txt = ""
            try:
                page.goto(url, wait_until="networkidle", timeout=60000)
                page.wait_for_timeout(random.uniform(800, 1500))
                txt = _safe_inner_text(page)
            except Exception as e:
                errors += 1
                print(f"  ERR fetch #{pid}: {e}")
                continue

            if not isinstance(txt, str) or len(txt) < 20:
                skipped += 1
                continue

            owner, acres, situs, legal = _parse_qpublic(txt)

            # Only fill gaps: don't overwrite an existing real street address
            # or a known owner / acres with (possibly absent) QPublic data.
            new_owner = owner if not owner0 else None
            new_acres = acres if acres0 is None else None
            if situs:
                new_addr = situs
            elif legal and (not addr or addr.lower().startswith("parcel ")):
                new_addr = f"{legal}, {co.title()} County, GA"
            else:
                new_addr = None

            if not (new_addr or new_owner or new_acres):
                skipped += 1
                continue

            conn.execute(
                "UPDATE properties SET owner_name=COALESCE(?, owner_name), "
                "acres=COALESCE(?, acres), address=COALESCE(?, address) WHERE id=?",
                (new_owner, new_acres, new_addr, pid),
            )
            updated += 1
            print(f"  #{pid} {co} {parcel}: owner={owner} acres={acres} addr={new_addr}")
            time.sleep(0.5)

    conn.commit()
    conn.close()
    return {"updated": updated, "skipped": skipped, "errors": errors}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--county", default=None)
    args = ap.parse_args()
    print(run(limit=args.limit, county=args.county))
