"""Flask web dashboard for investclosure — property listings, stats, API.

Serves as both the scraper runner (in background) and the web dashboard.
Ported from land-scout/realestate project.

Usage:
    python -m scraper.server    # Run web server on port 5001
"""
from __future__ import annotations
import json
import math
import os
import re
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Ensure scraper package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import (Flask, render_template, request, redirect, url_for,
                     jsonify, send_file, flash, abort, Response, g)

from scraper import db as scraper_db
from scraper.config import config, VERTICALS, DEFAULT_VERTICAL
from scraper.nc_gis_lookup import (
    build_satellite_url,
    build_street_view_url,
    build_google_maps_url,
    build_google_maps_topo_url,
)
from scraper.gis_urls import (HOSTED_VIEWER_PATH, get_hosted_viewer_url,
                              get_nconemap_viewer_url)

DATA_DIR = Path(os.environ.get('DATA_DIR', str(config.data_dir)))
REPORTS_DIR = Path(os.environ.get('REPORTS_DIR', str(config.data_dir / "reports")))

_TEMPLATE_DIR = str(Path(__file__).resolve().parent.parent / "templates" / "investclosure")
_STATIC_DIR = str(Path(__file__).resolve().parent.parent / "static")

app = Flask(__name__,
            template_folder=_TEMPLATE_DIR,
            static_folder=_STATIC_DIR)
app.secret_key = os.environ.get('SECRET_KEY', 'investclosure-secret-key-change-in-production')
app.jinja_env.auto_reload = True


@app.template_filter('fmt_num')
def fmt_num(value):
    """Format a stored number for display: 2.0 -> '2', 2.5 -> '2.5'."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return value
    return f"{num:g}"


@app.template_filter('clean_date')
def clean_date(value):
    """Clean a stored date field for safe display on cards / detail pages.

    Strips HTML (nc_publicnotice stores ``<span class='red'>Sale date not yet
    set</span>``) and hides placeholder text such as "not yet set".
    """
    if not value:
        return None
    text = re.sub(r"<[^>]+>", "", str(value)).strip()
    if not text or "not yet" in text.lower():
        return None
    return text


def property_category(prop: dict) -> str:
    """Bucket a property into 'tax', 'mtg', or 're' for dashboard tabs.

    Each state has one Tax tab (tax foreclosures, tax sales, and all other
    public/auction notices) and one Mtg tab for mortgage / deed-of-trust
    (bank) foreclosures plus bank-owned (REO) auction inventory. Non-
    foreclosure market listings (the 're' tab) are sourced entirely from
    regular listing feeds (zillow) rather than foreclosure proceedings.
    Mortgage detection checks the explicit ``property_type`` first
    (``mortgage_foreclosure`` / ``bank_owned``, set by the scrapers),
    falling back to notes/description keyword heuristics for legacy rows
    stored before explicit tagging.
    """
    if (prop.get("source") or "").strip().lower() == "zillow":
        return "re"
    ptype = (prop.get("property_type") or "").strip().lower()
    notes = (prop.get("notes") or "").lower()
    desc = (prop.get("description") or "").lower()
    if ("mortgage" in ptype or "mtg" in ptype or "deed of trust" in ptype
            or "bank_owned" in ptype or "reo" in ptype
            or "mortgage" in notes or "mtg" in notes
            or "deed of trust" in desc):
        return "mtg"
    return "tax"


# Sources that belong to the beach (water-frontage) vertical. Empty until the
# beach-front / lake-front scrapers land; every source today is a mountain
# source, so every row falls through to the 'mountains' vertical.
BEACH_SOURCES: set[str] = set()


def property_vertical(prop: dict) -> str:
    """Bucket a property into a top-level vertical ('mountains' or 'beach').

    Mirrors :func:`property_category` — derived from the row rather than a
    dedicated column. All current sources belong to the mountains vertical;
    the beach vertical is claimed only by the (not yet live) water-frontage
    sources registered in ``BEACH_SOURCES``.
    """
    if (prop.get("source") or "").strip().lower() in BEACH_SOURCES:
        return "beach"
    return "mountains"


def _bid_to_cents(text) -> Optional[int]:
    """Parse a bid string ("$78,750.00") into cents; None when not a dollar
    amount ("TBD", prose). Used for the upset/current-bid price display."""
    if not text:
        return None
    m = re.search(r"\$?\s*([\d][\d,]*(?:\.\d+)?)", str(text))
    if not m:
        return None
    try:
        dollars = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if dollars <= 0:
        return None
    return int(round(dollars * 100))


def _row_to_dict(row):
    """Convert a sqlite3.Row to a plain dict for template rendering.

    Ensures numeric fields are properly typed (float/int).
    """
    d = dict(row)
    for field, type_fn in [('acres', float), ('price_cents', int), ('elevation_ft', float), ('manual_acres_override', float)]:
        if field in d and d[field] is not None:
            try:
                d[field] = type_fn(d[field])
            except (ValueError, TypeError):
                d[field] = None
    # Templates render price from `prop.price` (cents, like price_cents);
    # db rows carry only price_cents, so derive it here (0 = no price).
    # A stored opening bid goes stale the moment a sale enters the upset-
    # bid period (#37 Cook Creek Rd: tile showed the $14,200 opening bid
    # while the current bid sat above $75k). The price shown is therefore
    # the freshest bid: the stored upset/current bid whenever it parses
    # and exceeds the opening bid, else the opening bid. The original
    # opening bid is kept in ``opening_price`` so templates can show the
    # progression ($14,200 → $78,750).
    opening = d.get("price_cents") or 0
    upset = _bid_to_cents(d.get("upset_bid"))
    if upset and upset > opening:
        d["price"] = upset
        d["opening_price"] = opening or None
    else:
        d["price"] = opening
        d["opening_price"] = None
    # Derive map/photo links from coordinates/address so the dashboard can
    # always offer a map + "photo" of the property (legacy rows inserted
    # before maps URLs were stored, e.g. zillow rows).
    if not d.get("google_maps_url"):
        d["google_maps_url"] = build_google_maps_url(
            d.get("longitude"), d.get("latitude"),
            d.get("address"), d.get("city"), d.get("county"), state=d.get("state"),
        )
    if not d.get("google_maps_topo_url") and d.get("google_maps_url"):
        d["google_maps_topo_url"] = build_google_maps_topo_url(
            d.get("longitude"), d.get("latitude"),
            d.get("address"), d.get("city"), d.get("county"), state=d.get("state"),
        )
    if not d.get("google_maps_satellite_url"):
        d["google_maps_satellite_url"] = build_satellite_url(
            d.get("longitude"), d.get("latitude"),
            d.get("address"), d.get("city"), d.get("county"), state=d.get("state"),
        )
    if not d.get("google_maps_street_url"):
        d["google_maps_street_url"] = build_street_view_url(
            d.get("longitude"), d.get("latitude"),
            d.get("address"), d.get("city"), d.get("county"), state=d.get("state"),
        )
    # The GIS button: a stored deep link when enrichment produced one (GA
    # qPublic / TN TPAD are parcel-deep), otherwise our hosted viewer built
    # from the row's parcel or coordinates — most Zillow rows carry coordinates
    # and no parcel, so their cards would otherwise offer no parcel map at all.
    d["gis_link"] = _gis_button_url(d)
    return d


def _rows_to_dicts(rows):
    """Convert a list of sqlite3.Row objects to plain dicts."""
    if rows is None:
        return []
    return [_row_to_dict(r) for r in rows]


def get_conn():
    """Get a database connection using investclosure config."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return scraper_db._ensure_db(config.db_path)


# ---- HTTP Basic Auth ------------------------------------------------------
# Initial bootstrap credentials (owner-requested). Change after first login
# via Admin -> Change password or: python3 -m scraper --set-password winmutt
DEFAULT_ADMIN_USER = "winmutt"
DEFAULT_ADMIN_PASSWORD = "1234asdf"

# Endpoints that stay public (health probes, static assets).
_PUBLIC_ENDPOINTS = {"health", "static"}


def _unauthorized():
    return Response(
        "Login required", 401,
        {"WWW-Authenticate": 'Basic realm="Mountains"'},
    )


@app.before_request
def require_auth():
    """Gate every route behind HTTP Basic Auth (except health/static)."""
    if request.endpoint in _PUBLIC_ENDPOINTS:
        return None
    conn = get_conn()
    try:
        if scraper_db.count_users(conn) == 0:
            scraper_db.create_user(conn, DEFAULT_ADMIN_USER,
                                   DEFAULT_ADMIN_PASSWORD)
    finally:
        conn.close()
    auth = request.authorization
    if not auth or not auth.username or not auth.password:
        return _unauthorized()
    conn = get_conn()
    try:
        ok = scraper_db.verify_user(conn, auth.username, auth.password)
    finally:
        conn.close()
    if not ok:
        return _unauthorized()
    g.current_user = auth.username
    return None


@app.context_processor
def inject_global_stats():
    """Always provide stats to every template."""
    try:
        conn = get_conn()
        stats = scraper_db.get_stats(conn)
        conn.close()
    except Exception:
        stats = {}
    return {'global_stats': stats}


# ---- Routes ---------------------------------------------------------------

# States surfaced first in the dashboard tab bar (primary markets)
STATE_PRIORITY = ["NC", "TN"]

# ---- Dashboard filtering / sorting ----------------------------------------
# Shared by the dashboard ('/') and the search page ('/properties'). Unknown
# acreage sorts last in both directions so a NULL never looks like the
# smallest (or largest) parcel.
_ACRES_ORDER = "CASE WHEN acres IS NULL THEN 1 ELSE 0 END, acres"

PROPERTY_SORTS = {
    "sale": ("Sale date (soonest first)",
             "COALESCE(initial_auction_date, last_seen) DESC, first_seen DESC"),
    "acres_desc": ("Acreage: high to low", _ACRES_ORDER + " DESC"),
    "acres_asc": ("Acreage: low to high", _ACRES_ORDER + " ASC"),
    "county_asc": ("County: A to Z",
                   "LOWER(county) ASC, LOWER(state) ASC, " + _ACRES_ORDER + " DESC"),
    "county_desc": ("County: Z to A",
                    "LOWER(county) DESC, LOWER(state) DESC, " + _ACRES_ORDER + " DESC"),
}
DEFAULT_PROPERTY_SORT = "sale"


def _parse_property_filters(args) -> dict:
    """Read county / acreage / sort query params from a request.

    The county select submits ``STATE|county`` so same-named counties in
    different states (Polk NC/TN, Union GA/TN, …) stay distinct; a bare
    ``county`` value is still accepted for older links.
    """
    county = (args.get('county') or '').strip()
    state = ''
    if '|' in county:
        state, county = (part.strip() for part in county.split('|', 1))
        state = state.upper()
    min_acres = args.get('min_acres', type=float)
    max_acres = args.get('max_acres', type=float)
    if min_acres is not None and min_acres < 0:
        min_acres = None
    if max_acres is not None and max_acres < 0:
        max_acres = None
    if min_acres is not None and max_acres is not None and min_acres > max_acres:
        min_acres, max_acres = max_acres, min_acres
    sort = (args.get('sort') or DEFAULT_PROPERTY_SORT).strip()
    if sort not in PROPERTY_SORTS:
        sort = DEFAULT_PROPERTY_SORT
    return {"county": county, "state": state, "min_acres": min_acres,
            "max_acres": max_acres, "sort": sort}


def _filters_active(filters: dict) -> bool:
    """True when the dashboard is showing anything other than its default view."""
    return bool(filters["county"]
                or filters["min_acres"] is not None
                or filters["max_acres"] is not None
                or filters["sort"] != DEFAULT_PROPERTY_SORT)


def _property_where(filters: dict, *, status: Optional[str] = 'active',
                    query: str = '') -> tuple[str, list]:
    """Build the shared WHERE clause for the dashboard / search queries."""
    clauses: list[str] = []
    params: list = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if query:
        clauses.append(
            "(address LIKE ? OR city LIKE ? OR county LIKE ?"
            " OR description LIKE ? OR notes LIKE ?"
            " OR parcel_number LIKE ? OR court_case LIKE ? OR source_listing_id LIKE ?)")
        params.extend([f"%{query}%"] * 8)
    if filters["state"] and filters["county"]:
        clauses.append("LOWER(state) = ? AND LOWER(county) = ?")
        params.extend([filters["state"].lower(), filters["county"].lower()])
    elif filters["county"]:
        clauses.append("LOWER(county) LIKE ?")
        params.append(f"%{filters['county'].lower()}%")
    # An acreage bound only makes sense for rows whose acreage is known.
    if filters["min_acres"] is not None:
        clauses.append("acres IS NOT NULL AND acres >= ?")
        params.append(filters["min_acres"])
    if filters["max_acres"] is not None:
        clauses.append("acres IS NOT NULL AND acres <= ?")
        params.append(filters["max_acres"])
    return (" AND ".join(clauses) or "1=1"), params


def _county_groups(conn) -> list[dict]:
    """Active counties grouped by state for the ``STATE|county`` selects."""
    rows = conn.execute(
        "SELECT DISTINCT state, county FROM properties"
        " WHERE status = 'active' AND county IS NOT NULL AND TRIM(county) <> ''"
        " ORDER BY LOWER(state), LOWER(county)"
    ).fetchall()
    groups: list[dict] = []
    for row in rows:
        state = (row["state"] or "").strip().upper()
        county = (row["county"] or "").strip()
        if not county:
            continue
        if not groups or groups[-1]["state"] != state:
            groups.append({"state": state, "counties": []})
        groups[-1]["counties"].append(county)
    return groups


def _sort_choices() -> list[tuple[str, str]]:
    return [(key, label) for key, (label, _sql) in PROPERTY_SORTS.items()]


# ---- Per-tab (state x category) dashboard queries -------------------------
# The dashboard used to run ONE query for every state and tab and slice the
# first 1000 rows into buckets. Tennessee alone has ~1,700 're' rows, so that
# window starved the other boards: NC (975 active rows) was left showing 4.
# Every (state, category) pane now runs its own query with its own filters and
# its own row budget, and its tab count is the count of what it actually
# matched.

PROPERTY_CATEGORIES = ("tax", "mtg", "re")
DASHBOARD_PANE_LIMIT = 1000

# SQL mirror of property_category(). Kept next to the Python classifier so the
# per-pane WHERE clauses cannot drift from what the tiles are bucketed by
# (tests/test_scraper.py asserts the two agree on every input shape).
_MTG_PATTERNS = (
    ("property_type", ("%mortgage%", "%mtg%", "%deed of trust%",
                       "%bank_owned%", "%reo%")),
    ("notes", ("%mortgage%", "%mtg%")),
    ("description", ("%deed of trust%",)),
)


def _category_clause(cat: str) -> tuple[str, list]:
    """WHERE fragment selecting one tab's rows (no params)."""
    mtg = " OR ".join(f"LOWER(COALESCE({col}, '')) LIKE '{pat}'"
                      for col, pats in _MTG_PATTERNS for pat in pats)
    is_re = "LOWER(COALESCE(source, '')) = 'zillow'"
    if cat == "re":
        return is_re, []
    if cat == "mtg":
        return f"NOT ({is_re}) AND ({mtg})", []
    return f"NOT ({is_re}) AND NOT ({mtg})", []


def _pane_arg(args, cat: str, name: str):
    """Read ``<name>_<cat>``, falling back to the bare ``<name>``.

    The bare form keeps links written before the per-tab controls (and the
    old shared filter bar) working: they then apply to every tab.
    """
    for key in (f"{name}_{cat}", name):
        value = (args.get(key) or "").strip()
        if value:
            return value
    return None


def _pane_filters(args, cat: str) -> dict:
    """Filters for one (state, category) pane."""
    county = _pane_arg(args, cat, "county") or ""
    # Legacy shared-bar form: 'NC|Macon'. The state half names the board the
    # county belongs to, so it only applies when that board is on screen.
    county_state = ""
    if "|" in county:
        county_state, county = (p.strip() for p in county.split("|", 1))
        county_state = county_state.upper()

    def _acres(name):
        raw = _pane_arg(args, cat, name)
        try:
            value = float(raw) if raw is not None else None
        except ValueError:
            return None
        return None if value is not None and value < 0 else value

    min_acres, max_acres = _acres("min_acres"), _acres("max_acres")
    if min_acres is not None and max_acres is not None and min_acres > max_acres:
        min_acres, max_acres = max_acres, min_acres
    sort = _pane_arg(args, cat, "sort") or DEFAULT_PROPERTY_SORT
    if sort not in PROPERTY_SORTS:
        sort = DEFAULT_PROPERTY_SORT
    return {"county": county, "county_state": county_state,
            "min_acres": min_acres, "max_acres": max_acres, "sort": sort}


def _pane_filters_active(filters: dict) -> bool:
    """True when a pane shows anything other than its default view."""
    return bool(filters["county"]
                or filters["min_acres"] is not None
                or filters["max_acres"] is not None
                or filters["sort"] != DEFAULT_PROPERTY_SORT)


def _pane_where(state: str, cat: str, filters: dict) -> tuple[str, list]:
    """WHERE clause for one pane: its state, its tab, its county/acreage."""
    clause, params = _category_clause(cat)
    clauses = ["status = 'active'", f"UPPER(state) = ?", f"({clause})"]
    params = [state] + params
    county = filters["county"]
    if county:
        clauses.append("LOWER(county) = ?")
        params.append(county.lower())
    if filters["min_acres"] is not None:
        clauses.append("acres IS NOT NULL AND acres >= ?")
        params.append(filters["min_acres"])
    if filters["max_acres"] is not None:
        clauses.append("acres IS NOT NULL AND acres <= ?")
        params.append(filters["max_acres"])
    return " AND ".join(clauses), params


def _state_counts(conn) -> list[dict]:
    """Active totals per state, priority markets first (the state tab bar)."""
    rows = conn.execute(
        "SELECT UPPER(state) AS state, COUNT(*) AS n FROM properties"
        " WHERE status = 'active' AND state IS NOT NULL"
        " GROUP BY UPPER(state)"
    ).fetchall()
    counts = {(r["state"] or "UNKNOWN"): r["n"] for r in rows}
    return [{"state": s, "count": n} for s, n in sorted(
        counts.items(),
        key=lambda kv: (STATE_PRIORITY.index(kv[0]) if kv[0] in STATE_PRIORITY else 99,
                        kv[0]))]


def _counties_for(conn, state: str, cat: str) -> list[str]:
    """County choices for one pane's select (that state + tab only)."""
    clause, params = _category_clause(cat)
    rows = conn.execute(
        "SELECT DISTINCT county FROM properties"
        f" WHERE status = 'active' AND UPPER(state) = ? AND ({clause})"
        " AND county IS NOT NULL AND TRIM(county) <> ''"
        " ORDER BY LOWER(county)",
        [state] + params).fetchall()
    return [(r["county"] or "").strip() for r in rows if (r["county"] or "").strip()]


@app.route('/')
def landing():
    """Dashboard: one state board, its three category tabs each filtered/sorted
    by their own controls (``?st=NC&cat=mtg&county_mtg=…&sort_re=…``)."""
    conn = get_conn()
    states = _state_counts(conn)
    state_keys = [s["state"] for s in states]
    board = (request.args.get("st") or "").strip().upper()
    if board not in state_keys:
        # A stale/absent board falls back to the first state that has rows.
        board = state_keys[0] if state_keys else ""

    panes: dict[str, dict] = {}
    for cat in PROPERTY_CATEGORIES:
        filters = _pane_filters(request.args, cat)
        # A legacy 'NC|Macon' filter belongs to NC's board only.
        if filters["county_state"] and filters["county_state"] != board:
            filters = dict(filters, county="")
        where, params = _pane_where(board, cat, filters)
        order = PROPERTY_SORTS[filters["sort"]][1]
        rows = conn.execute(
            f"SELECT * FROM properties WHERE {where} ORDER BY {order}, id DESC LIMIT ?",
            [*params, DASHBOARD_PANE_LIMIT]).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) FROM properties WHERE {where}", params).fetchone()[0]
        panes[cat] = {
            "props": _rows_to_dicts(rows),
            "total": total,
            "shown": len(rows),
            "filters": filters,
            "filters_active": _pane_filters_active(filters),
            "counties": _counties_for(conn, board, cat),
        }
    active_total = conn.execute(
        "SELECT COUNT(*) FROM properties WHERE status = 'active'").fetchone()[0]
    conn.close()

    requested = (request.args.get("cat") or "").strip().lower()
    if requested in panes and panes[requested]["total"]:
        active_cat = requested
    else:
        # Open on the first tab that actually has rows: landing on an empty
        # Tax tab while 900 listings sit under Re reads as "no results".
        active_cat = next((c for c in PROPERTY_CATEGORIES if panes[c]["total"]),
                          PROPERTY_CATEGORIES[0])

    # The mountains|beach switch counts every active row (beach stays a stub
    # until the water-frontage sources land — see BEACH_SOURCES).
    verticals = [
        {"key": k, "label": meta["label"], "status": meta["status"],
         "count": active_total if k == DEFAULT_VERTICAL else 0}
        for k, meta in VERTICALS.items()
    ]

    return render_template('landing.html',
                            state=board,
                            states=states,
                            panes=panes,
                            active_cat=active_cat,
                            verticals=verticals,
                            default_vertical=DEFAULT_VERTICAL,
                            categories=PROPERTY_CATEGORIES,
                            sort_choices=_sort_choices())


@app.route('/properties')
def properties():
    conn = get_conn()
    query = request.args.get('q', '').strip()
    status_filter = request.args.get('status', '').strip()
    page = max(1, request.args.get('page', 1, type=int))
    per_page = min(200, max(1, request.args.get('per_page', 20, type=int)))
    offset = (page - 1) * per_page
    filters = _parse_property_filters(request.args)

    # An empty status means "active" (historical behaviour); any other value
    # (e.g. 'archived') filters to exactly that status.
    status_value = status_filter or 'active'
    where, params = _property_where(filters, status=status_value, query=query)
    order = PROPERTY_SORTS[filters["sort"]][1]

    rows = conn.execute(
        f"SELECT * FROM properties WHERE {where} ORDER BY {order}, id DESC LIMIT ? OFFSET ?",
        [*params, per_page, offset],
    ).fetchall()
    total = conn.execute(
        f"SELECT COUNT(*) FROM properties WHERE {where}", params).fetchone()[0]
    county_groups = _county_groups(conn)
    conn.close()
    rows = _rows_to_dicts(rows)

    # Keep every filter in the pager links (previously hand-concatenated, which
    # broke on float acreage values and dropped new params such as sort).
    page_query = {k: v for k, v in request.args.items() if k != 'page'}
    prev_url = (url_for('properties', page=page - 1, **page_query)
                if page > 1 else None)
    next_url = (url_for('properties', page=page + 1, **page_query)
                if page * per_page < total else None)

    return render_template('properties.html',
                           properties=rows,
                           total=total,
                           page=page,
                           per_page=per_page,
                           query=query,
                           filters=filters,
                           status=status_filter,
                           county_groups=county_groups,
                           sort_choices=_sort_choices(),
                           prev_url=prev_url,
                           next_url=next_url)


@app.route('/new')
def new():
    conn = get_conn()
    days = request.args.get('days', 7, type=int)
    since_date = (date.today() - timedelta(days=days)).isoformat()
    rows = scraper_db.get_new_since(conn, since_date=since_date)
    conn.close()
    return render_template('new.html', properties=_rows_to_dicts(rows), days=days)


@app.route('/run-scraper', methods=['GET'])
def run_scraper():
    """Manually trigger a scraper run."""
    scraper_name = request.args.get('scraper', 'all')

    if scraper_name == 'nc_publicnotice':
        from scraper.nc_publicnotice import NCPublicNoticeScraper
        scraper = NCPublicNoticeScraper()
        properties = scraper.run()
    elif scraper_name == 'tn_publicnotice':
        from scraper.tn_publicnotice import scrape_with_enrichment
        properties = scrape_with_enrichment(solve_captcha=True, enrich=True)
    elif scraper_name == 'ga_publicnotice':
        from scraper.ga_publicnotice import GAPublicNoticeScraper
        properties = GAPublicNoticeScraper().run()
    else:
        from scraper.nc_publicnotice import NCPublicNoticeScraper
        from scraper.tn_publicnotice import scrape_with_enrichment
        nc_props = NCPublicNoticeScraper().run()
        tn_props = scrape_with_enrichment(solve_captcha=True, enrich=True)
        properties = nc_props + tn_props

    flash(f'Scraper complete: {len(properties)} properties found')
    return redirect(url_for('landing'))


@app.route('/enrich')
def enrich():
    """Trigger TNMap enrichment of existing properties."""
    from scraper.tnmap import enrich_with_tnmap

    conn = get_conn()
    tn_props = scraper_db.get_all_active(conn, limit=500, source="tn_publicnotice")
    conn.close()

    if not tn_props:
        flash('No TN properties found to enrich')
        return redirect(url_for('landing'))

    enriched = enrich_with_tnmap(tn_props)
    count = sum(1 for p in enriched if p.get('tnmap_owner'))
    flash(f'Enriched {count}/{len(tn_props)} properties with TNMap data')
    return redirect(url_for('properties'))


# ---- API endpoints --------------------------------------------------------

@app.route('/api/stats')
def api_stats():
    conn = get_conn()
    stats = scraper_db.get_stats(conn)
    conn.close()
    return jsonify(stats)


@app.route('/api/properties')
def api_properties():
    conn = get_conn()
    query = request.args.get('q', '')
    county = request.args.get('county', '')
    limit = request.args.get('limit', 100, type=int)

    sql = "SELECT * FROM properties WHERE status = 'active'"
    params = []

    if query:
        sql += (" AND (address LIKE ? OR city LIKE ? OR county LIKE ?"
                " OR description LIKE ?"
                " OR parcel_number LIKE ? OR court_case LIKE ? OR source_listing_id LIKE ?)")
        pattern = f"%{query}%"
        params.extend([pattern] * 7)

    if county:
        sql += " AND county LIKE ?"
        params.append(f"%{county}%")

    sql += " ORDER BY COALESCE(initial_auction_date, last_seen) DESC, first_seen DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/scrape-runs')
def api_scrape_runs():
    conn = get_conn()
    runs = conn.execute(
        """SELECT * FROM scrape_runs ORDER BY started_at DESC LIMIT 50"""
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in runs])


@app.route('/api/property/<int:property_id>')
def api_property_detail(property_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()
    conn.close()
    if not row:
        abort(404)
    return jsonify(dict(row))


@app.route('/api/property/<int:property_id>/notes', methods=['PATCH'])
def update_property_notes(property_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Property not found"}), 404
    data = request.get_json() or {}
    notes = data.get('notes', '')
    conn.execute("UPDATE properties SET notes = ? WHERE id = ?", (notes, property_id))
    conn.commit()
    conn.close()
    return jsonify({"message": "Notes updated", "notes": notes})


@app.route('/api/property/<int:property_id>/acres', methods=['PATCH'])
def update_property_acres(property_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Property not found"}), 404
    existing_set = row["manual_acres_set"] or ""
    if existing_set:
        conn.close()
        return jsonify({"error": "Manual acreage already set and is immutable"}), 409
    data = request.get_json()
    if not data or 'acres' not in data:
        conn.close()
        return jsonify({"error": "Missing acres value"}), 400
    new_acres = float(data["acres"])
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """UPDATE properties SET
           acres = ?, manual_acres_set = ?, manual_acres_override = ?
           WHERE id = ?""",
        (new_acres, now, new_acres, property_id),
    )
    conn.commit()
    conn.close()
    return jsonify({"message": "Manual acreage set", "acres": new_acres, "manual_acres_set": now})


@app.route('/api/property/<int:property_id>/navigation')
def property_navigation(property_id):
    conn = get_conn()
    category = request.args.get('category', '').strip().lower()

    # Match the dashboard/search ordering, not raw ID order
    rows = conn.execute(
        """SELECT id, address, source, property_type FROM properties
           WHERE status = 'active'
           ORDER BY COALESCE(initial_auction_date, last_seen) DESC,
                    first_seen DESC, id DESC"""
    ).fetchall()
    conn.close()

    ordered = [dict(r) for r in rows]
    if category in ('tax', 'mtg', 're'):
        ordered = [p for p in ordered if property_category(p) == category]

    ids = [p["id"] for p in ordered]
    try:
        idx = ids.index(property_id)
    except ValueError:
        idx = None

    if idx is None:
        return jsonify({"previous": None, "next": None})

    prev = ordered[idx - 1] if idx > 0 else None
    nxt = ordered[idx + 1] if idx < len(ordered) - 1 else None
    return jsonify({
        "previous": {"id": prev["id"], "address": prev["address"]} if prev else None,
        "next": {"id": nxt["id"], "address": nxt["address"]} if nxt else None,
    })


def _viewer_map_url(prop: dict) -> Optional[str]:
    """Same-origin hosted viewer URL for a row, or None.

    Only ``/static/gis_viewer.html`` can be embedded in an <iframe> — the GA
    qPublic / TN TPAD links are third-party pages that block framing. NC rows
    get the OneMap parcel layer; a row whose enrichment never produced a
    ``gis_url`` (most Zillow rows) gets a viewer built from its coordinates,
    in any state, so the desktop detail view can show the parcel in place.
    """
    gis = (prop.get("gis_url") or "").strip()
    if gis.startswith(HOSTED_VIEWER_PATH):
        return gis
    lat = prop.get("latitude")
    lon = prop.get("longitude")
    parcel = (prop.get("parcel_number") or "").strip() or None
    state = (prop.get("state") or "").upper()
    if state == "NC" and ((lat is not None and lon is not None) or parcel):
        return get_nconemap_viewer_url(lon, lat, parcel, prop.get("county"))
    if lat is not None and lon is not None and not gis:
        return get_hosted_viewer_url(lon, lat, state=state)
    return None


def _gis_button_url(prop: dict) -> Optional[str]:
    """Target for a card's GIS button.

    A stored link wins: the GA qPublic / TN TPAD pages are parcel-deep, which
    beats a coordinate-centred map. Otherwise fall back to the hosted viewer.
    A row with neither gets no button at all (link-depth policy: Google Maps
    still covers address-only rows).
    """
    return (prop.get("gis_url") or "").strip() or _viewer_map_url(prop)


@app.route('/property/<int:property_id>')
def property_detail(property_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    court_case = (row["court_case"] or "").strip()
    same_case = []
    if court_case:
        same_case = scraper_db.get_by_court_case(conn, court_case, exclude_id=property_id)
    cross_links = scraper_db.get_property_links(conn, property_id)
    audit_log = scraper_db.get_audit_log(conn, property_id)
    conn.close()
    prop = _row_to_dict(row)
    return render_template('property.html', prop=prop,
                           map_embed_url=_viewer_map_url(prop),
                           same_case=[_row_to_dict(r) for r in same_case],
                           cross_links=cross_links,
                           audit_log=audit_log)


@app.route('/archive/<int:property_id>', methods=['POST'])
def archive_property(property_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    scraper_db.archive_property(
        conn, property_id,
        reason="dashboard archive button", archived_by="dashboard",
    )
    conn.close()
    flash(f'Property #{property_id} archived')
    return redirect(url_for('landing'))


@app.route('/bulk-archive', methods=['POST'])
def bulk_archive():
    """Bulk archive selected properties (dashboard checkboxes).

    Each id goes through scraper_db.archive_property — audit-logged per
    row, non-active/missing ids are skipped (kept visible in the flash).
    Returns to the posting view via the optional ``return_to`` field
    (same-site path only) so users stay where they were.
    """
    conn = get_conn()
    ids: list[int] = []
    for raw in request.form.getlist('ids'):
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            return abort(400, description=f"invalid property id {raw!r}")
    # bulk only — cap to keep a stray select-all on huge archives safe
    if len(ids) > 500:
        ids = ids[:500]
    reason = (request.form.get('reason') or '').strip() \
        or "dashboard bulk archive"
    archived = skipped = 0
    for property_id in ids:
        if scraper_db.archive_property(
            conn, property_id, reason=reason, archived_by="dashboard",
        ):
            archived += 1
        else:
            skipped += 1
    conn.close()
    flash(f'Bulk archive: {archived} archived'
          + (f', {skipped} skipped (already archived or missing)'
             if skipped else ''))
    return_to = request.form.get('return_to') or ''
    if return_to.startswith('/') and not return_to.startswith('//'):
        return redirect(return_to)
    return redirect(url_for('landing'))


@app.route('/unarchive/<int:property_id>', methods=['POST'])
def unarchive_property(property_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    scraper_db.unarchive_property(
        conn, property_id,
        reason="dashboard unarchive button", restored_by="dashboard",
    )
    conn.close()
    flash(f'Property #{property_id} unarchived')
    return redirect(url_for('landing'))


@app.route('/admin')
def admin():
    conn = get_conn()
    users = scraper_db.list_users(conn)
    conn.close()
    return render_template('admin.html', users=users)


@app.route('/admin/add-user', methods=['POST'])
def admin_add_user():
    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    conn = get_conn()
    try:
        scraper_db.create_user(conn, username, password)
    except ValueError as e:
        flash(f'Add user failed: {e}')
    else:
        flash(f'User {username!r} created')
    finally:
        conn.close()
    return redirect(url_for('admin'))


@app.route('/admin/change-password', methods=['POST'])
def admin_change_password():
    username = (request.form.get('username') or '').strip()
    current = request.form.get('current_password') or ''
    new_password = request.form.get('new_password') or ''
    conn = get_conn()
    try:
        if username == g.get('current_user'):
            if not scraper_db.verify_user(conn, username, current):
                flash('Current password is incorrect')
                return redirect(url_for('admin'))
        if not scraper_db.set_password(conn, username, new_password):
            flash(f'No such user: {username!r}')
        else:
            flash(f'Password updated for {username!r}')
    except ValueError as e:
        flash(f'Change password failed: {e}')
    finally:
        conn.close()
    return redirect(url_for('admin'))


@app.route('/export')
def export():
    conn = get_conn()
    rows = scraper_db.get_all_active(conn, limit=10000)
    conn.close()

    export_data = []
    for r in rows:
        row = dict(r)
        # Clean up internal fields
        row.pop('dedup_hash', None)
        row['price'] = round(row.get('price_cents', 0) / 100, 2) if row.get('price_cents') else 0
        export_data.append(row)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = REPORTS_DIR / f"export_{date.today().isoformat()}.json"
    filename.write_text(json.dumps(export_data, indent=2, default=str))

    flash(f'Exported {len(export_data)} properties to {filename}')
    return redirect(url_for('landing'))


class _ProxyImageCache:
    """Small LRU for proxied GIS images.

    NC OneMap's elevation and ortho ImageServers are dynamic ``exportImage``
    services (no tile cache), so every pan re-renders each tile server-side.
    Keeping the last few hundred kilobytes-bounded tiles makes panning back and
    refreshing the page cheap without unbounded memory growth.
    """

    def __init__(self, max_entries: int = 400, max_bytes: int = 48 * 1024 * 1024):
        from collections import OrderedDict
        self._items: "OrderedDict[tuple, tuple]" = OrderedDict()
        self._bytes = 0
        self._max_entries = max_entries
        self._max_bytes = max_bytes

    def get(self, key):
        hit = self._items.get(key)
        if hit is None:
            return None
        self._items.move_to_end(key)
        return hit

    def put(self, key, body: bytes, content_type: str) -> None:
        if len(body) > self._max_bytes // 4:
            return  # one huge image would evict everything else
        old = self._items.pop(key, None)
        if old is not None:
            self._bytes -= len(old[0])
        self._items[key] = (body, content_type)
        self._bytes += len(body)
        while (len(self._items) > self._max_entries
               or self._bytes > self._max_bytes):
            _, (evicted, _ct) = self._items.popitem(last=False)
            self._bytes -= len(evicted)


_GIS_IMAGE_CACHE = _ProxyImageCache()

# NC OneMap's LiDAR elevation products. DEM03 is the statewide 3.125 US-survey-
# foot (~0.95 m) LiDAR-derived DEM; the ``_hillshade`` / ``_shaded_relief``
# siblings are pre-rendered from it.
NC_ELEV_PATH = "secure/rest/services/Elevation"
# DEM03_hillshade (grayscale) reads far better under parcel lines and the water
# overlay than the tinted _shaded_relief at these zooms; both are pre-rendered
# from the same DEM03 grid.
NC_LIDAR_IMAGE = "DEM03_hillshade/ImageServer"

# Web Mercator half-extent, the origin every XYZ tile bbox is measured from.
_MERCATOR_R = 20037508.342789244


@app.route('/gis/lidar/<int:z>/<int:x>/<int:y>.png')
def gis_lidar_tile(z: int, x: int, y: int):
    """NC OneMap's LiDAR relief as ordinary XYZ tiles.

    The state publishes elevation only as *dynamic* ImageServers (exportImage,
    no tile cache). Publicly the JS SDK's ``ImageryLayer`` stitches those badly
    at these zooms — one stretched image with no-data wedges — so the same
    product is re-tiled here: the XYZ bbox is computed locally, one exportImage
    is requested per tile, and the result is cached in-process and by the
    browser. Deterministic, pan-safe, and no different data from the state's
    own hillshade.
    """
    if not 0 <= z <= 19:
        abort(404)
    n = 1 << z
    if not (0 <= x < n and 0 <= y < n):
        abort(404)

    cache_key = ("lidar", z, x, y)
    hit = _GIS_IMAGE_CACHE.get(cache_key)
    if hit is not None:
        body, ct = hit
        return Response(body, content_type=ct,
                        headers={"Cache-Control": "public, max-age=86400"})

    size = 2 * _MERCATOR_R / n
    x0 = -_MERCATOR_R + x * size
    y1 = _MERCATOR_R - y * size
    bbox = f"{x0},{y1 - size},{x0 + size},{y1}"

    from scraper import nconemap_net
    url = (f"https://services.nconemap.gov/{NC_ELEV_PATH}/{NC_LIDAR_IMAGE}"
           f"/exportImage?bbox={bbox}&bboxSR=3857&imageSR=3857&size=256,256"
           f"&format=png32&f=image&interpolation=RSP_BilinearInterpolation")
    try:
        resp = nconemap_net.get(url, timeout=30)
    except Exception as e:  # pragma: no cover - network
        return jsonify({"error": f"nconemap lidar failed: {e}"}), 502

    ct = resp.headers.get("content-type", "image/png")
    if resp.status_code != 200 or "image" not in ct:
        return jsonify({"error": "nconemap lidar tile unavailable"}), 502

    _GIS_IMAGE_CACHE.put(cache_key, resp.content, ct)
    return Response(resp.content, content_type=ct,
                    headers={"Cache-Control": "public, max-age=86400"})


@app.route('/gis/proxy/<path:dp>', methods=["GET", "POST", "HEAD", "OPTIONS"])
def gis_proxy(dp: str):
    """Server-side proxy to the NC OneMap ArcGIS REST services.

    nconemap.gov only whitelists ``arcgis.com`` for CORS, so a browser cannot
    load a layer or tile from it directly. This route forwards requests to
    ``https://services.nconemap.gov/<dp>`` server-side and returns the response
    same-origin: the parcel layer, the cached ortho tiles and the LiDAR
    ImageServer layers of the hosted viewer all come through here.

    Restricted to ArcGIS service paths to avoid an open proxy. Image responses
    carry a short Cache-Control so panning back does not re-render them, and
    get a small in-process cache — the dynamic elevation/ortho services take
    0.2-2s per tile, so a pan would otherwise queue a dozen TLS round-trips.
    """
    if not (dp.startswith("secure/rest/services/") or dp.startswith("arcgis/rest/services/")):
        abort(403)
    downstream = "https://services.nconemap.gov/" + dp
    qs = request.query_string.decode("latin-1")
    url = downstream + ("?" + qs if qs else "")

    from scraper import nconemap_net

    cache_key = (dp, qs)
    if request.method == "GET":
        hit = _GIS_IMAGE_CACHE.get(cache_key)
        if hit is not None:
            body, ct = hit
            return Response(body, status=200, content_type=ct,
                            headers={"Cache-Control": "public, max-age=3600"})

    try:
        if request.method == "POST":
            resp = nconemap_net.post(
                url, data=request.get_data(),
                headers={"Content-Type": request.headers.get("Content-Type", "application/x-www-form-urlencoded")},
                timeout=30,
            )
        else:
            resp = nconemap_net.get(url, timeout=30)
    except Exception as e:  # pragma: no cover - network
        return jsonify({"error": f"nconemap proxy failed: {e}"}), 502

    ct = resp.headers.get("content-type", "application/json")
    if "image" in ct:
        if request.method == "GET":
            _GIS_IMAGE_CACHE.put(cache_key, resp.content, ct)
        return Response(resp.content, status=resp.status_code, content_type=ct,
                        headers={"Cache-Control": "public, max-age=3600"})
    return Response(resp.content, status=resp.status_code, content_type=ct)


@app.route('/health')
def health():
    """Health endpoint for external monitors."""
    conn = get_conn()

    scrapers_last_run = {}
    for row in conn.execute(
        """SELECT source, started_at, status, properties_found
           FROM scrape_runs
           WHERE (source, started_at) IN (
               SELECT source, MAX(started_at) FROM scrape_runs GROUP BY source
           )
           ORDER BY source"""
    ).fetchall():
        scrapers_last_run[row["source"]] = {
            "last_run": row["started_at"],
            "status": row["status"],
            "found": row["properties_found"],
        }

    six_hours_ago = (datetime.now() - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
    recent_errors = []
    for row in conn.execute(
        """SELECT source, started_at, status, error_message
           FROM scrape_runs
           WHERE started_at >= ? AND status IN ('failed', 'error')
           ORDER BY started_at DESC LIMIT 5""",
        (six_hours_ago,),
    ).fetchall():
        recent_errors.append({
            "scraper": row["source"],
            "time": row["started_at"],
            "error": row["error_message"],
        })

    has_healthy = any(
        s["status"] == "completed" and s["last_run"]
        for s in scrapers_last_run.values()
    )

    conn.close()

    health_status = "healthy" if has_healthy else "degraded"
    if recent_errors:
        health_status = "unhealthy"

    return jsonify({
        "status": health_status,
        "scrapers": scrapers_last_run,
        "recent_errors": recent_errors,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ---- Main -----------------------------------------------------------------

def main():
    port = int(os.environ.get('FLASK_PORT', 5001))
    host = os.environ.get('FLASK_HOST', '0.0.0.0')
    app_host = os.environ.get('FLASK_BIND', f'{host}:{port}')
    print(f"Starting dashboard on {app_host}")
    app.run(host=host, port=port, debug=False)


if __name__ == '__main__':
    main()
