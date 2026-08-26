# AGENTS.md

For full project context, available commands, and architecture, read **[README.md](./README.md)** first.

## Guiding Rules

### Top Priority
- **Never write to `/tmp` or use heredocs (`<<EOF`/`<< 'EOF'`).** The host `/tmp` is not visible in the container and heredocs can be escaped unpredictably. For temporary files, use `scraper/tmp/` (volume-mounted under `./scraper`) or `/tmp/opencode/` for workspace-external scratch files. Always run shell commands in the container via `podman exec investclosure`.
- **Always run shell commands inside the container.** Use `podman exec investclosure python3 ...` — never `python3` bare on the host for scraper/db work. For file edits, edit the host path (`scraper/`, `templates/`, etc.) which sync via volume mount.
- **Always `rm -rf /app/scraper/__pycache__` after editing `.py` files in the container.** Stale bytecode will cause old code to run silently.
- **Always run the scraper in the container after editing.** The local `tmp` dir is `scraper/tmp/` (volume-mounted via `./scraper:/app/scraper`).

### Operational Safety
- **Rate limit all HTTP calls to 2 requests per second.** Space out requests with `time.sleep(0.5)` or random delay `(0.25, 0.75)`. Never burst-request external APIs.
- **Always use camoufox to fetch external web content — without exception.** Plain `curl`, `wget`, and Python `urllib`/`requests` (including `curl_cffi`) must NEVER be used for investigative/test/link-validation fetches against external sites. Even endpoints that are not Cloudflare-protected (ArcGIS REST services, nconemap.gov, DNS-style HTTP checks, etc.) must be fetched through the scraper's camoufox (stealth Firefox) machinery — `from scraper.base import camoufox_context` — so fetches behave exactly like a real browser (correct Origin/CORS, JS-rendered pages, WebGL, etc.). The only place `curl_cffi` is permitted is inside the scraper's own code paths (e.g. `scraper/nc_gis_lookup.py`'s server-side NC OneMap queries), which is a deliberate implementation choice, not an interactive fetch. For connectivity/DNS-only checks, use `podman exec investclosure python3 -c "import socket; socket.gethostbyname(...)"`, not `curl`. When you need to inspect HTTP status, CORS headers, or whether a page actually renders, drive a camoufox page (`page.goto`, `page.request.get`, `page.evaluate(fetch...)`) and read the result from there.
- **Never make assumptions about infrastructure status.** Always verify podman/docker is running before attempting container operations. Prompt the user for help if container commands fail.
- **Check DNS connectivity explicitly.** If DNS resolution fails (especially for external sites like `nc1map.gov`, `zls-nc.com`, `zillow.com`, or ArcGIS services), inform the user and ask for network troubleshooting help — do not retry indefinitely or assume the network will recover.
- **Report errors immediately.** When a scraper, GIS lookup, or database query fails, log the error and alert the user with context (what was failing, what was attempted, what succeeded).
- **Verify before acting.** After any database modification, scraper run, or container rebuild, verify the result before declaring success.
- **Use existing code patterns.** Always read the source before editing. Match indentation (4 spaces), use type hints, follow existing logging patterns (`logger.info`, `logger.warning`, `logger.error`).
- **Read before editing.** Always `read` a file at least once before using `edit`. Use `grep` or `glob` for targeted searches.
- **Never modify production data without explicit user consent.** Database archiving, schema changes, or data deletions require user confirmation.

### Debugging: Error Detection Without Full Output Capture
When running long commands (scraper runs, tests) that may produce errors mid-stream, **never rely solely on `tail -20`** — critical failures can be silently missed. Use this pattern instead:

```bash
# 1. Write both stdout and stderr to a log file, then check for errors
podman exec investclosure python3 -m scraper --scraper kania_law > scraper/tmp/debug.log 2>&1
# 2. Check exit code and grep for error patterns
grep -c "ERROR\|Exception\|Traceback\|Error.*HTTP\|400\|500" scraper/tmp/debug.log
# 3. Show only lines containing errors + context (1 line before/after)
grep -B1 -A1 -i "error\|exception\|traceback" scraper/tmp/debug.log | tail -30
# 4. Check DB state to confirm impact
podman exec investclosure python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/investclosure.db'); print(conn.execute('SELECT COUNT(*) FROM properties WHERE status=\"active\"').fetchone())"
```

For scraper runs specifically, always check the `scrape_runs` table for `status` and `error_message` columns:
```bash
podman exec investclosure python3 -c "
import sqlite3; conn=sqlite3.connect('/app/data/investclosure.db')
runs = conn.execute('SELECT id, source, status, properties_found, error_message FROM scrape_runs ORDER BY id DESC LIMIT 3').fetchall()
for r in runs: print(r)
"
```

If errors are found but unclear, capture the last 50 lines as a diagnostic:
```bash
sed -n '1000,$p' scraper/tmp/debug.log 2>/dev/null || tail -50 scraper/tmp/debug.log
```

## Quick Reference

| File | Purpose |
|---|---|
| `scraper/base.py` | BaseScraper — captcha solving, acreage parsing, chromium detection |
| `scraper/kania_law.py` | Kania Law scraper — NC tax foreclosure auctions, NC OneMap enrichment |
| `scraper/buncombe_tax.py` | Buncombe County tax foreclosure scraper — Trumba iCal feed (`tax-foreclosures-all.ics`), NC OneMap enrichment |

| `scraper/zls_nc.py` | ZLS-NC scraper — Zacchaeus Legal foreclosure listings, filtered to NC mountain counties |
| `scraper/ncforeclosures.py` | NC foreclosure notices scraper — ncforeclosures.com, PDF text as raw source, NC OneMap enrichment |
| `scraper/tnforeclosures.py` | TN foreclosure notices scraper — tnforeclosures.com, PDF text as raw source, GIS enrichment |
| `scraper/ganotices.py` | GA foreclosure notices scraper — georgiapublicnotice.com (Georgia Press Assoc), 7 N GA mountain counties |
| `scraper/newspaper_notices.py` | Newspaper legal-notice scraper — full notice text as raw source |
| `scraper/nc_gis_lookup.py` | NC OneMap parcel lookup — statewide service for all 100 NC counties |
| `scraper/config.py` | Central config — counties, thresholds, env-overridable |
| `scraper/db.py` | SQLite CRUD — `insert_property()`, `get_stats()`, `archive_below_acres()` |
| `scraper/run.py` | CLI runner — `python3 -m scraper --list` for commands |
| `scraper/server.py` | Flask dashboard — port 5001, auto-refresh listing |
| `scraper/gis_urls.py` | GIS viewer URL builder — county registry for 21 mountain counties |
| `nc_county_summary.md` | Per-county NC property/tax/GIS lookup systems + verified portal URLs |
| `ga_county_summary.md` | Per-county GA qPublic (Schneider Corp) app IDs, pages, KeyValue spacing |
| `tn_county_summary.md` | Per-county TN property/tax/GIS systems + tax-foreclosure handling |
| `.env.example` | Template for environment variables |

## Scrapers

| Scraper | Source | Target | Captcha | GIS Enrichment |
|---|---|---|---|---|
| `kania_law` | kaniabailbond.com | NC (filtered) | None | NC OneMap statewide |
| `buncombe_tax` | trumba.com/calendars/tax-foreclosures-all.ics | Buncombe | None | NC OneMap statewide |

| `zls_nc` | zls-nc.com/listings | NC mountain only | None | NC OneMap statewide |
| `ncforeclosures` | ncforeclosures.com | NC (PDF notices) | None | NC OneMap statewide |
| `tnforeclosures` | tnforeclosures.com | TN (PDF notices) | None | GIS enrichment |
| `ganotices` | georgiapublicnotice.com | GA (7 N mountain counties) | Turnstile | None |

All scrapers use **MIN_ACREAGE = 5.0** (configurable via `INVESTCLOSURE_MIN_ACRES`). Kania Law scrapes ALL 183 records from the API but only processes the 21 qualifying NC mountain counties. ZLS NC scrapes all listings but filters to NC mountain counties only. Buncombe Tax scrapes the county's Trumba iCal feed (bid, case #, PIN, redeem flag) — all Buncombe properties qualify. `ncforeclosures` and `tnforeclosures` download each notice PDF and store the **full PDF text** as `raw_source_text` (the on-page HTML is truncated). When PDF text is available and exceeds 300 chars (ncforeclosures) or when PDF exists (tnforeclosures), it is used as the canonical source; otherwise the on-page notice text falls back. `ganotices` scrapes Georgia Press Association tax-sale notices for the 7 N GA mountain counties and stores the full notice text as `raw_source_text` (extracted from the notice page, not PDF).

## Qualified Counties

Scope spans **6 states** (elevation >1700ft, within 250mi of Atlanta): **GA, AL, KY, NC, SC, TN**. Full county lists live in `scraper/config.py` (`QUALIFYING_COUNTIES`); the investclosure target sets are `NC_MOUNTAIN_COUNTIES` (21) and `GA_MOUNTAIN_COUNTIES` (7).

### NC — 21 mountain counties (investclosure target set)
`alleghany, ashe, avery, buncombe, burke, cherokee, clay, graham, haywood, henderson, jackson, madison, mcdowell, mitchell, polk, macon, swain, transylvania, watauga, yancey`

**Excluded from NC**: Rowan, Rutherford, Cleveland, Catawba, Gaston (foothills, <1700ft), Stokes, Davie, Harnett (outside 250mi radius).

### GA — 7 mountain counties (ganotices target set)
`fannin, gilmer, lumpkin, rabun, towns, union, white`

A broader reference set `GA_FORECLOSURE_COUNTIES` (11) is also defined: `dawson, fannin, gilmer, habersham, lumpkin, murray, pickens, rabun, towns, union, white`.

### Other states (defined in `config.py`)
- **AL** (5): `blount, cherokee, cleburne, dekalb, talladega`
- **KY** (5): `bell, harlan, knox, perry, whitley`
- **SC** (4): `anderson, greenville, oconee, pickens`
- **TN** (37): mountain counties only (see `TN_FORECLOSURE_COUNTIES`)

**GA note**: Georgia has no statewide parcel data hub (data-hub.gio.georgia.gov returns 0 sources), so `ganotices` records are not GIS-enriched — only NC uses NC OneMap.

## GIS Integration

- **NC OneMap statewide service**: `https://services.nconemap.gov/secure/rest/services/NC1Map_Parcels/MapServer/1` — single query for all 100 NC counties
- Primary acreage field: `gisacres` (Double type)
- Key fields: `parno` (parcel number), `ownname` (owner), `siteaddr` (address), `usecd` (land use code)
- GIS lookup via `scraper/nc_gis_lookup.py` with `NC1MapService.by_parcel(parcel_clean)` method
- Falls back to Google Maps parcel search: `https://www.google.com/maps/search/parcel+{parcel}+in+{county}+NC`

### County knowledge base (markdown summaries)

State/county-specific lookup systems, verified portal URLs, qPublic app IDs,
and tax-foreclosure handling are maintained in standalone markdown files (not
in code) so they can be updated as we learn new details without touching the
scrapers:

- **`nc_county_summary.md`** — NC property/tax/GIS portals (Buncombe, Transylvania, Henderson, Watauga, Burke, …).
- **`ga_county_summary.md`** — GA qPublic (Schneider Corp) `AppID`/`LayerID`/`PageID` and `KeyValue` spacing per county; includes the `Q`-token 403 caveat and which counties are still on the generic fallback.
- **`tn_county_summary.md`** — TN property/tax/GIS systems and tax-foreclosure cadence for all 38 east-TN mountain counties.

When you discover a new portal URL, qPublic app ID, parcel-key format, or
tax-sale nuance, **update the relevant summary file** (and, for GA, the
`GA_QPUBLIC_APPS` registry in `scraper/gis_urls.py` + re-run
`python3 -m scraper.backfill_ga_gis`).

## CLI Commands

```bash
python3 -m scraper --list            # List available scrapers
python3 -m scraper                   # Run all scrapers
python3 -m scraper --scraper kania_law  # Run Kania Law only
python3 -m scraper --scraper buncombe_tax  # Run Buncombe Tax only
python3 -m scraper --scraper zls_nc     # Run ZLS-NC only
python3 -m scraper --status              # DB stats
python3 -m scraper --new                 # New properties since last run
python3 -m scraper --archive             # Archive below MIN_ACRES
python3 -m scraper --cron                # Continuous mode (every 360 min)
python3 -m scraper --help                # Show all options
```

## Docker / Podman

```bash
# Rebuild and run (source volume-mounted — no rebuild needed for code changes)
podman compose up --build
podman compose up -d          # Run in background
docker compose up --build      # Docker alternative

# Inspect running container
podman ps          # Check container status
podman logs investclosure  # View logs
podman exec investclosure python3 -m scraper --status  # Run inside container
```

## Directory Structure

**Volume mounts** — host paths sync to container paths via `docker-compose.yml`:

| Host Path | Container Path | Purpose |
|---|---|---|
| `./data/` | `/app/data/` | SQLite DB, backups, logs |
| `./reports/` | `/app/reports/` | Generated reports |
| `./scraper/` | `/app/scraper/` | Scraper source code (volume-mounted, live edits) |
| `./templates/` | `/app/templates/` | Flask HTML templates |
| `./static/` | `/app/static/` | Flask static assets (CSS, JS) |

**Container layout** (`/app/` inside `investclosure`):

```
/app/
├── data/                  ← host ./data/
│   ├── investclosure.db   ← main SQLite database
│   ├── backups/           ← DB backups
│   └── logs/              ← application logs
├── reports/               ← host ./reports/
├── scraper/               ← host ./scraper/ (live volume mount)
│   ├── __pycache__/       ← remove after editing .py files
│   ├── base.py
│   ├── kania_law.py
│   ├── zillow.py
│   ├── zls_nc.py
│   ├── nc_gis_lookup.py
│   ├── config.py
│   ├── db.py
│   ├── run.py
│   ├── server.py
│   ├── gis_urls.py
│   └── tmp/               ← temporary files (debug.log, etc.)
├── templates/             ← host ./templates/
└── static/                ← host ./static/
```

**Key notes**:
- `scraper/` volume-mounted — edits take effect immediately, no rebuild needed
- `data/` volume-mounted — persists across container restarts/rebuilds
- Always `rm -rf /app/scraper/__pycache__` after editing `.py` files inside the container
- Temporary files go in `scraper/tmp/` (not host `/tmp`)
- **All code updates must be followed by a commit and push.**

## Configuration

All paths configurable via env vars — **no hardcoded paths**:

| Variable | Default | Purpose |
|---|---|---|
| `INVESTCLOSURE_DATA_DIR` | `./data` | Base data directory |
| `INVESTCLOSURE_DB_PATH` | `./data/investclosure.db` | SQLite database path |
| `INVESTCLOSURE_BACKUPS_DIR` | `./data/backups` | DB backups directory |
| `INVESTCLOSURE_LOGS_DIR` | `./data/logs` | Log files directory |
| `INVESTCLOSURE_MIN_ACRES` | `5.0` | Minimum acreage filter |
| `INVESTCLOSURE_MAX_ACRES` | `1000.0` | Maximum acreage filter |
| `INVESTCLOSURE_PROXY` | | Proxy `host:port` |

## Recent Updates

- **2026-07-29**: ZLS NC scraper — filtered to NC mountain counties (21 counties), NC OneMap GIS enrichment
- **2026-07-29**: Kania Law scraper — county filtering to 21 NC mountain counties added
- **2026-07-29**: DB cleaned — archived 62 properties from non-qualifying counties (Rowan, Burke, etc.)
- **2026-07-29**: NC OneMap statewide parcel service replaces per-county portal lookups
- **2026-07-26**: Dashboard overhaul — parcel + address on tiles, topo/GIS links, last_seen ordering
- **2026-07-26**: GIS county portal registry for 21 NC mountain counties
- **2026-07-26**: ZLS-NC scraper — all-page size (241 rows), no pagination
- **2026-07-26**: Kania Law — ArcGIS LAND_UNITS acreage, 5-acre filter, GIS enrichment
- **GA in scope**: 7 N GA mountain counties (fannin, gilmer, lumpkin, rabun, towns, union, white) via `ganotices`; AGENTS.md scope updated to 6 states. `ganotices` drops non-sale proceedings (quiet-title / tax-redemption, excess-fund interpleaders, foreclosure of equity of redemption) and keeps only upcoming tax-sale foreclosures. **GA tax sales are held on the first Tuesday of every month** (computed from the notice's "first Tuesday in <Month> <Year>"), and **GA has no upset-bid period**. Bundled notices (e.g. White County lists many parcels per notice "by deed/page"; Towns County lists each tax-map parcel in its own block) are split into **separate per-parcel listings** keyed on `<county>:<parcel_number>`; duplicate postings of the same parcel collapse while distinct parcels stay separate.
- **2026-08-25**: `ga_county_summary.md` created — GA qPublic (Schneider Corp) app IDs / pages / KeyValue spacing per county; Lumpkin (`AppID=991`) and White (`AppID=982`) verified and added to `GA_QPUBLIC_APPS` in `scraper/gis_urls.py`. County knowledge bases now tracked in `nc_county_summary.md`, `ga_county_summary.md`, and `tn_county_summary.md` (see "County knowledge base" under GIS Integration).

## Tests

Integration tests cover DB operations, archive filtering, county filtering, and GIS enrichment:

```bash
python3 -m pytest tests/test_scraper.py -v          # Run all tests
python3 -m pytest tests/test_scraper.py::TestArchiveBelowAcres -v  # Archive tests only
```

### Test Coverage
- **DB CRUD**: insert, dedup, get stats, scrape run tracking
- **Archive**: parameter ordering fix (critical bug), source filtering, acres threshold
- **ZLS NC**: county filtering (mountain vs coastal), GIS enrichment
- **Kania Law**: scraper initialization, county count verification
- **Dedup hash**: case-insensitive, with/without coordinates
