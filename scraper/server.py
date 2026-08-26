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
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Ensure scraper package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file, flash, abort

from scraper import db as scraper_db
from scraper.config import config

DATA_DIR = Path(os.environ.get('DATA_DIR', str(config.data_dir)))
REPORTS_DIR = Path(os.environ.get('REPORTS_DIR', str(config.data_dir / "reports")))

_TEMPLATE_DIR = str(Path(__file__).resolve().parent.parent / "templates" / "investclosure")
_STATIC_DIR = str(Path(__file__).resolve().parent.parent / "static")

app = Flask(__name__,
            template_folder=_TEMPLATE_DIR,
            static_folder=_STATIC_DIR)
app.secret_key = os.environ.get('SECRET_KEY', 'investclosure-secret-key-change-in-production')
app.jinja_env.auto_reload = True


NOTICE_SOURCES = {"newspaper_notices"}
NOTICE_TYPE_KEYWORDS = ("notice", "estate", "proceeding")


def property_category(prop: dict) -> str:
    """Bucket a property into 'notice' or 'listing' for dashboard tabs.

    Notices are legal/public notices published in newspapers (no auction
    listing). Foreclosure listings are actual auction/sale listings from
    law-firm and auction scrapers.
    """
    source = (prop.get("source") or "").strip().lower()
    ptype = (prop.get("property_type") or "").strip().lower()
    if source in NOTICE_SOURCES or any(k in ptype for k in NOTICE_TYPE_KEYWORDS):
        return "notice"
    return "listing"


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

@app.route('/')
def landing():
    conn = get_conn()
    props = scraper_db.get_all_active(conn, limit=1000, source=None)
    conn.close()
    props = _rows_to_dicts(props)
    notices = [p for p in props if property_category(p) == "notice"]
    listings = [p for p in props if property_category(p) == "listing"]
    return render_template('landing.html',
                           properties=props,
                           notices=notices,
                           listings=listings)


@app.route('/properties')
def properties():
    conn = get_conn()
    query = request.args.get('q', '').strip()
    county = request.args.get('county', '').strip()
    min_acres = request.args.get('min_acres', type=float)
    status_filter = request.args.get('status', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)

    offset = (page - 1) * per_page

    # Build query — investclosure doesn't use price much
    if status_filter:
        sql = "SELECT * FROM properties WHERE status = ?"
        params = [status_filter]
    else:
        sql = "SELECT * FROM properties WHERE status = 'active'"
        params = []

    if query:
        sql += " AND (address LIKE ? OR city LIKE ? OR county LIKE ? OR description LIKE ? OR notes LIKE ?)"
        pattern = f"%{query}%"
        params.extend([pattern, pattern, pattern, pattern, pattern])

    if county:
        sql += " AND county LIKE ?"
        params.append(f"%{county}%")

    if min_acres:
        sql += " AND acres >= ?"
        params.append(min_acres)

    sql += " ORDER BY COALESCE(initial_auction_date, last_seen) DESC, first_seen DESC LIMIT ? OFFSET ?"
    params.extend([per_page, offset])

    rows = conn.execute(sql, params).fetchall()

    # Get count for pagination
    if status_filter:
        count_sql = "SELECT COUNT(*) FROM properties WHERE status = ?"
        count_params = [status_filter]
    else:
        count_sql = "SELECT COUNT(*) FROM properties WHERE status = 'active'"
        count_params = []

    if query:
        count_sql += " AND (address LIKE ? OR city LIKE ? OR county LIKE ? OR description LIKE ? OR notes LIKE ?)"
        pattern = f"%{query}%"
        count_params.extend([pattern, pattern, pattern, pattern, pattern])
    if county:
        count_sql += " AND county LIKE ?"
        count_params.append(f"%{county}%")
    if min_acres:
        count_sql += " AND acres >= ?"
        count_params.append(min_acres)

    total = conn.execute(count_sql, count_params).fetchone()[0]

    county_options = conn.execute(
        "SELECT DISTINCT county FROM properties WHERE county IS NOT NULL AND status='active' ORDER BY county"
    ).fetchall()

    return render_template('properties.html',
                           properties=_rows_to_dicts(rows),
                           total=total,
                           page=page,
                           per_page=per_page,
                           query=query,
                           county=county,
                           min_acres=min_acres,
                           status=status_filter,
                           county_options=_rows_to_dicts(county_options))


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

    if scraper_name == 'ncforeclosures':
        from scraper.ncforeclosures import NCForeclosureScraper
        scraper = NCForeclosureScraper()
        properties = scraper.run()
    elif scraper_name == 'tnforeclosures':
        from scraper.tnforeclosures import scrape_with_enrichment
        properties = scrape_with_enrichment(solve_captcha=True, enrich=True)
    else:
        from scraper.ncforeclosures import NCForeclosureScraper
        from scraper.tnforeclosures import scrape_with_enrichment
        nc_props = NCForeclosureScraper().run()
        tn_props = scrape_with_enrichment(solve_captcha=True, enrich=True)
        properties = nc_props + tn_props

    flash(f'Scraper complete: {len(properties)} properties found')
    return redirect(url_for('landing'))


@app.route('/enrich')
def enrich():
    """Trigger TNMap enrichment of existing properties."""
    from scraper.tnmap import enrich_with_tnmap

    conn = get_conn()
    tn_props = scraper_db.get_all_active(conn, limit=500, source="tnforeclosures")
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
        sql += " AND (address LIKE ? OR city LIKE ? OR county LIKE ? OR description LIKE ?)"
        pattern = f"%{query}%"
        params.extend([pattern, pattern, pattern, pattern])

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
    if category in ('notice', 'listing'):
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
    conn.close()
    return render_template('property.html', prop=_row_to_dict(row),
                           same_case=[_row_to_dict(r) for r in same_case])


@app.route('/archive/<int:property_id>', methods=['POST'])
def archive_property(property_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    conn.execute("UPDATE properties SET status = 'archived' WHERE id = ?", (property_id,))
    conn.commit()
    conn.close()
    flash(f'Property #{property_id} archived')
    return redirect(url_for('landing'))


@app.route('/unarchive/<int:property_id>', methods=['POST'])
def unarchive_property(property_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM properties WHERE id = ?", (property_id,)).fetchone()
    if not row:
        conn.close()
        abort(404)
    conn.execute("UPDATE properties SET status = 'active' WHERE id = ?", (property_id,))
    conn.commit()
    conn.close()
    flash(f'Property #{property_id} unarchived')
    return redirect(url_for('landing'))


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
