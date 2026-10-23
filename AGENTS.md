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
| `scraper/base.py` | BaseScraper — in-browser Turnstile solving first, 2captcha fallback (key-gated), acreage parsing, chromium detection |
| `scraper/trustee_base.py` | TrusteeSaleScraper — shared mortgage-sale parsing/links for the 5 trustee-sale scrapers |
| `scraper/courthouses.py` | County-courthouse address registry (61 across NC/TN/GA) + matcher — rejects sale-venue addresses |
| `scraper/kania_law.py` | Kania Law scraper — NC tax foreclosure auctions, NC OneMap enrichment |
| `scraper/buncombe_tax.py` | Buncombe County tax foreclosure scraper — Trumba iCal feed (`tax-foreclosures-all.ics`), NC OneMap enrichment |
| `scraper/haywood_county.py` | Haywood County CivicPlus bid-module monitor — detail pages (`bids.aspx?bidID=N`) + DocumentCenter notice PDFs (scanned → OCR via pdftoppm+tesseract), full notice text as raw source |
| `scraper/hutchens_law.py` | Hutchens Law Firm NC foreclosure sales (`sales.hutchenslawfirm.com` RadGrid) — `mortgage_foreclosure`, 20 NC mountain, REAP source, cross-linked to nc_publicnotice; post-run OneMap enrichment (address → parcel) fills acres |

| `scraper/zls_nc.py` | ZLS-NC scraper — Zacchaeus Legal foreclosure listings, filtered to NC mountain counties |
| `scraper/nc_publicnotice.py` | NC "Public Notice" foreclosure scraper — ncnotices.com (ASP.NET WebForms), PDF text as raw source, NC OneMap enrichment |
| `scraper/tn_publicnotice.py` | TN "Public Notice" foreclosure scraper — tnpublicnotice.com (same WebForms backend), PDF text as raw source, TNMap enrichment |
| `scraper/ga_publicnotice.py` | GA "Public Notice" foreclosure scraper — georgiapublicnotice.com (same WebForms backend), 7 N GA mountain counties |
| `scraper/publicnotice_base.py` | Shared base for the three `*_publicnotice` scrapers (GridView parse, Turnstile gate, PDF extract, tax/mortgage classifier) |
| `scraper/newspaper_notices.py` | Newspaper legal-notice scraper — full notice text as raw source |
| `scraper/backfill_newspaper.py` | Re-derives newspaper row type/address/city/zip/auction-date from `raw_source_text` |
| `scraper/al_publicnotice.py` | AL public-notice foreclosure scraper — alabamapublicnotices.com (IPA WebForms: per-category select + per-county checkbox), full notice text as raw source; no statewide parcel service → Maps links only |
| `scraper/ky_publicnotice.py` | KY public-notice scraper — kypublicnotice.com (Newz Group CodeIgniter app, NOT WebForms: POST search + hidden `page` pagination), full newspaper-page PDF text as raw source, Master-Commissioner anchor-window extraction; Maps links only |
| `scraper/nc_gis_lookup.py` | NC OneMap parcel lookup — statewide service for all 100 NC counties |
| `scraper/config.py` | Central config — counties, thresholds, env-overridable |
| `scraper/db.py` | SQLite CRUD — `insert_property()`, `get_stats()`, `archive_below_acres()` |
| `scraper/run.py` | CLI runner — `python3 -m scraper --list` for commands |
| `scraper/server.py` | Flask dashboard (branded **Mountains**) — port 5001, HTTP Basic Auth, Admin page, mountains|beach verticals |
| `scraper/gis_urls.py` | GIS viewer URL builder — county registry for 20 NC mountain counties + GA qPublic apps |
| `scraper/geo_lookup.py` + `town_county.json` | town/ZIP→county cross-reference (4,917 ZIPs / 3,625 USPS towns, 6 states, from Census ZIP→county) — resolves town-only listings |
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
| `nc_publicnotice` | ncnotices.com | NC (PDF notices) | Turnstile | NC OneMap statewide |
| `tn_publicnotice` | tnpublicnotice.com | TN (PDF notices) | Turnstile | TNMap enrichment |
| `ga_publicnotice` | georgiapublicnotice.com | GA (7 N mountain counties) | Turnstile | qPublic acreage (`ga_gis_enrich.py`) |
| `rlselaw` | rlselaw.com GA listings (static table) | 7 GA mountain | None | qPublic links |
| `brockandscott` | brockandscott.com search (URL params + pager) | NC/GA/TN mountain | None | state map links |
| `foreclosuretennessee` | foreclosuretennessee.com grid + details | 38 TN mountain | None | TNMap links |
| `bellcarrington` | bellcarrington.com (Google Sheet CSV) | GA/NC/SC/AL/TN mountain | None | state map links |
| `logs_nc` | logs.com NC report (PowerBI table visual) | 20 NC mountain | None | NC OneMap links |
| `auction_com` | auction.com county pages + details | NC/GA/TN/SC/AL mountain | None | state map links |
| `zillow` | zillow.com county search pages (`__NEXT_DATA__`) | 6 states (88 counties) | PerimeterX (fresh profile/county) | centroid guard + post-insert enrichers |

The five trustee-sale scrapers (`rlselaw`, `brockandscott`,
`foreclosuretennessee`, `bellcarrington`, `logs_nc`) share
`scraper/trustee_base.py` (HTML cleaning, price/date/address parsing,
county filtering, `mortgage_foreclosure` record builder with map/GIS
links). All rows are mortgage/deed-of-trust sales for the Mtg tab.
`auction_com` (same base + county detail pages) adds bank-owned (REO)
inventory as `bank_owned`, also Mtg-tabbed.

All three `*_publicnotice` scrapers share the same ASP.NET WebForms "Public Notice" backend (see `scraper/publicnotice_base.py`). They download each notice PDF and store the **full PDF text** as `raw_source_text` (the on-page HTML is truncated). When PDF text is available and exceeds 300 chars (nc_publicnotice) or when PDF exists (tn_publicnotice), it is used as the canonical source; otherwise the on-page notice text falls back. `ga_publicnotice` scrapes Georgia Press Association tax-sale notices for the 7 N GA mountain counties and stores the full notice text as `raw_source_text` (extracted from the notice page, not PDF).

## Product Verticals (branding)

The dashboard is **multi-category**. A *vertical* is a top-level product line
with its own county scope and qualification rules, sitting **above** the
per-state Tax/Mtg/Re category tabs (`property_category()`).

| Vertical | Status | Scope | Qualification |
|---|---|---|---|
| **Mountains** | active | mountain counties (elevation ≥1700ft, within 250mi of Atlanta — the whole current product) | `MIN_ACRES` / `MAX_ACRES` / `MAX_PRICE` |
| **Beach** | **stub (UI only)** | ocean-front + lake-front counties (water-county map upcoming) | **minimum distance-to-water** — *no* acreage/elevation |

- **Branding is UI-only.** The dashboard is branded **Mountains** (titles, sidebar brand, footer, auth realm, GIS viewer). Internal identifiers are **unchanged**: container/service `investclosure`, DB `data/investclosure.db`, `INVESTCLOSURE_*` env vars, `templates/investclosure/` + `static/investclosure/` dirs.
- Verticals are a `config.py` registry (`VERTICALS` + `DEFAULT_VERTICAL`). The mountains|beach switch is the **outer** tab group in `templates/investclosure/landing.html` (`data-tab-group="vertical"`, reuses the existing tab JS). A vertical is claimed per row by `property_vertical()` in `scraper/server.py`, which claims only `BEACH_SOURCES` (empty until beach scrapers land) — so every current row is `mountains`.
- **Beach is a stub**: `status='stub'`, no water-county map, no distance-to-water data source, and no `properties` schema change yet. When it goes live it will add `water_type` + `distance_to_water_m` (+ a `MIN_DISTANCE_TO_WATER` threshold and a water GIS source: coastline + lake polygons).

## Qualified Counties

Scope spans **6 states** (elevation >1700ft, within 250mi of Atlanta): **GA, AL, KY, NC, SC, TN**. Full county lists live in `scraper/config.py` (`QUALIFYING_COUNTIES`); the investclosure target sets are `NC_MOUNTAIN_COUNTIES` (21) and `GA_MOUNTAIN_COUNTIES` (7).

### NC — 20 mountain counties (investclosure target set)
`alleghany, ashe, avery, buncombe, burke, cherokee, clay, graham, haywood, henderson, jackson, madison, mcdowell, mitchell, polk, macon, swain, transylvania, watauga, yancey`

**Excluded from NC**: Rowan, Rutherford, Cleveland, Catawba, Gaston (foothills, <1700ft), Stokes, Davie, Harnett (outside 250mi radius).

### GA — 7 mountain counties (ga_publicnotice target set)
`fannin, gilmer, lumpkin, rabun, towns, union, white`

A broader reference set `GA_FORECLOSURE_COUNTIES` (11) is also defined: `dawson, fannin, gilmer, habersham, lumpkin, murray, pickens, rabun, towns, union, white`.

### Other states (defined in `config.py`)
- **AL** (5): `blount, cherokee, cleburne, dekalb, talladega`
- **KY** (5): `bell, harlan, knox, perry, whitley`
- **SC** (4): `anderson, greenville, oconee, pickens`
- **TN** (38): mountain counties only (see `TN_FORECLOSURE_COUNTIES`)

**GA note**: Georgia has no statewide parcel data hub (data-hub.gio.georgia.gov returns 0 sources). `ga_publicnotice` acreage is enriched per-county from qPublic parcel reports via camoufox page loads (`scraper/ga_gis_enrich.py`, auto-run after each GA scrape); plain HTTP gets Cloudflare-403 so reports are browser-only. Rabun Sky Valley lots report Acres 0 at source and keep `acres=NULL`.

## GIS Integration

Link-depth policy: every GIS button must take you to the property (parcel-deep
or coordinate-centered). Rows with no parcel/coords get **no** GIS button
(Google Maps still covers address rows); states with no parcel service
(SC/AL) never get viewer links. `scraper/backfill_links.py` rebuilds all
links; `scraper/tn_gis_enrich.py` fills TN TPAD links + acres/owner.

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
python3 -m scraper --add-user alice  # Create dashboard login (prompts, hashed)
python3 -m scraper --set-password winmutt  # Reset a dashboard password
python3 -m scraper --list            # List available scrapers
python3 -m scraper                   # Run all scrapers
python3 -m scraper --scraper kania_law  # Run Kania Law only
python3 -m scraper --scraper buncombe_tax  # Run Buncombe Tax only
python3 -m scraper --scraper zls_nc     # Run ZLS-NC only
python3 -m scraper --status              # DB stats
python3 -m scraper --new                 # New properties since last run
python3 -m scraper --archive             # Archive below MIN_ACRES
python3 -m scraper --cron                # Scheduled mode (default 4a & 4p, --cron-hours to override)
python3 -m scraper --help                # Show all options
```

## Docker / Podman

```bash
# Rebuild and run (source volume-mounted — no rebuild needed for code changes)
podman compose up --build
podman compose up -d          # Run in background
docker compose up --build      # Docker alternative
# NOTE: `restart` preserves creation-time env — `.env` changes (thresholds,
# keys) require recreate: podman stop investclosure && podman rm investclosure
# && podman-compose up -d. (2026-09-11: MIN_ACRES change needed exactly this;
# also fixed unparseable `environment: KEY=${VAR}` lines to quoted mapping.)

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
- **Restart the container after code edits** (`podman restart investclosure`):
  the `--cron` scheduler imports all scrapers once at startup and runs them
  in-process, so scheduled runs silently keep using pre-edit code until the
  container restarts (2026-09-30: a week of cron runs used pre-fix code).
- `data/` volume-mounted — persists across container restarts/rebuilds
- Always `rm -rf /app/scraper/__pycache__` after editing `.py` files inside the container
- Temporary files go in `scraper/tmp/` (not host `/tmp`)
- **All code updates must be followed by a commit and push.** This includes
  dependency changes — any `pip install` performed inside the container (e.g.
  `pdfplumber`, `pdfminer`, `pillow`) must also be added to `requirements.txt`
  and committed, otherwise a `podman compose up --build` rebuilds from the
  committed `requirements.txt` and silently loses the package. A live
  `pip install` only patches the running container and does not survive rebuilds.

## Configuration

All paths configurable via env vars — **no hardcoded paths**:

| Variable | Default | Purpose |
|---|---|---|
| `INVESTCLOSURE_DATA_DIR` | `./data` | Base data directory |
| `INVESTCLOSURE_DB_PATH` | `./data/investclosure.db` | SQLite database path |
| `INVESTCLOSURE_BACKUPS_DIR` | `./data/backups` | DB backups directory |
| `INVESTCLOSURE_LOGS_DIR` | `./data/logs` | Log files directory |
| `INVESTCLOSURE_MIN_ACRES` | `1.1` | Minimum acreage filter |
| `INVESTCLOSURE_MAX_ACRES` | `1000.0` | Maximum acreage filter |
| `INVESTCLOSURE_PROXY` | | Proxy `host:port` |

## Recent Updates

- **2026-10-23 (rebrand to "Mountains" + beach vertical scaffold)**: The
  dashboard is now **multi-vertical** and branded **Mountains**.
  - **Branding-only rename** InvestClosure → **Mountains**: page `<title>`s
    (base/landing/properties/new/admin), sidebar brand (icon ⛰️ + word),
    footer, the GIS viewer title/header (`static/gis_viewer.html`), and the
    HTTP Basic Auth realm (`Basic realm="Mountains"`). **Nothing internal
    changed** — container/service `investclosure`, DB `investclosure.db`,
    `INVESTCLOSURE_*` env, and the `templates/investclosure/` +
    `static/investclosure/` dirs all stay (zero prod/data risk).
  - **New `VERTICALS` registry** in `config.py` (`mountains`=active,
    `beach`=stub) + `DEFAULT_VERTICAL`. `property_vertical()` in `server.py`
    derives a row's vertical (mirrors `property_category()`; source-based via
    `BEACH_SOURCES`, empty for now, so every current row is `mountains`).
    `landing()` now groups vertical→state→category and passes ordered vertical
    metadata; `landing.html` gained a **mountains|beach tab bar** above the
    state tabs (reuses the existing nested `[data-tab-group]` tab JS — no JS
    change) with beach rendering a "coming soon" stub. CSS: `.tabs-vertical` /
    `.tab-soon` / `.muted` in `style.css`.
  - **Deferred (next pass, not built):** the beach/lake-front **water-county
    map**, the **minimum-distance-to-water** rule (+ `MIN_DISTANCE_TO_WATER`
    and a water GIS source: coastline + lake polygons), and any `properties`
    schema additions (`water_type`, `distance_to_water_m`). Beach scrapers
    register in `BEACH_SOURCES` when they land. 4 tests in
    `TestVerticals` (`tests/test_tax_mtg_tabs.py`); full suite 531 pass.

- **2026-10-22 (newspaper HOA-lien fix + cross-scraper type/address audit)**:
  Property **#827** (Sylva Herald, source `newspaper_notices`) was on the Tax
  tab, had no address, and no enrichment. Root causes + fixes:
  - **Surviving-lien boilerplate was a tax signal**: every NC power-of-sale
    notice recites "subject to all prior sales and releases and to all deeds
    of trust, liens, unpaid taxes...matters of record, if any" — the
    "unpaid taxes" there lists liens that SURVIVE the sale. The HOA
    assessment-lien notice (#827 River Run II Townhouse Association) had no
    other signal, and that aside alone flipped it to tax. Fix in
    `newspaper_notices.py` + `publicnotice_base.py` (`SURVIVING_LIEN_BOILERPLATE_RE`)
    + `nc_publicnotice.py` strong-tax tiebreak.
  - **HOA/condo/POA assessment-lien foreclosures** (`_HOA_ENTITY_RE` +
    `_HOA_LIEN_RE`, entity signal AND dues/lien signal) are private lien
    sales: routed to the **Mtg** tab, never Tax (strong tax language still
    wins the other way). `45-21` added to `_MORTGAGE_SALE_RE` (NC
    power-of-sale statute cited in the notice).
  - **Dual-signal notices no longer silently dropped** in NC/TN/GA shared
    classifiers: mortgage-machinery + weak tax aside → mortgage (Mtg),
    mortgage+STRONG_TAX → tax; the previously dead `STRONG_TAX_FC_PATTERNS`
    is now the tiebreaker (bare `tax sale` removed from the strong list —
    too weak as a tiebreaker). NC got a matching `_STRONG_TAX_RE`
    (`_classify_nc_notice` policy tests updated).
  - **Address extraction wired into all five BLOX/AdPerfect newspaper
    sub-scrapers** (previously hardcoded `address=None`; Gannett API path was
    the only one extracting). New `extract_newspaper_address()` returns
    (street, city, zip), supports "Property address: 232 River Run, Unit
    1903, Sapphire, NC 28774" (one-line BLOX bodies capture to the state
    token; road words can't become city), splits city/zip, and
    `_extract_auction_date` now supplies the real sale date instead of the
    publication date. `scraper/backfill_newspaper.py` re-derives type,
    address/city/zip and auction date from `raw_source_text` for existing
    rows (live: 2 reclassified #57/#827 → Mtg, #827 address Sapphire/28774,
    sale date 2026-10-29), then enrichment picks rows up (OneMap address
    path). GA: `_GA_PARCEL_ADDR_RE` also accepts "Property address:".
  - **kgis_enrich wired into `run.py`** post-run (Knox TN rows — TNMap
    statewide layer has zero Knox parcels; KGIS was manual-only).
  - Cross-scraper audit findings NOT changed (open, by design or larger
    effort): trustee scrapers assert type from source identity (no per-notice
    classifier — a firm posting a tax sale would Mtg-tab silently); rlselaw
    rows never enrich acres (no parcel; GA enrichment is parcel-only);
    SC/AL rows have no parcel service so never acres/viewer links; GA
    fannin/union lack qPublic app IDs (generic qPublic link only);
    haywood embeds the bid title in the description (a "deed of trust"
    title could flip its tab); kania/zls price code OK (verified dollars).
    Full suite 526 pass (container tests/ is image-copied, not volume —
    `podman cp tests investclosure:/app/` before running).

- **2026-10-22 (AL + KY public-notice scrapers went live)**: Two new
  foreclosure sources over the two *other* state legal-notice platforms:
  - **`al_publicnotice`** (alabamapublicnotices.com — same IPA/ASP.NET
    WebForms backend as NC/TN/GA): 5 target counties
    (`AL_FORECLOSURE_COUNTIES`: blount, cherokee, cleburne, dekalb,
    talladega), categories looped fresh per iteration
    (**a category pivot needs a FRESH page load** — the 2nd
    `ddlPopularSearches` category searched empty without it), per-county
    checkbox search shares `_set_county_filter`/`_submit_county_search` with
    the publicnotice base. Turnstile detail gate solves in-browser (live
    verified; 2captcha fallback configured). On-page OCR hyphen-splits words
    (`MORT-GAGE FORECLOS-URE`), so classification runs on both raw and
    hyphen-stripped copies — but the **hyphen-stripper fuses statutory cites
    ("40-10-180" → "4010180")**, so patterns must also run on the raw copy.
    **Site-footer trap**: every detail page ends "...in accordance with the
    Code of Alabama..." → the bare "code of alabama" tax template would
    flip EVERY notice to tax_foreclosure; tax signals are strictly the
    AL statutory set (`tax collecting official / delinquent tax / section
    40-10 / tax sale / tax lien / tax redemption`), and the shared
    TN/GA `TAX_FC_PATTERNS` are NOT used. "Delinquent Taxes" category is
    **seasonal** (empty grid verified 2026-10-22; AL tax-foreclosure
    notices publish in the spring window) — empty is not an error.
  - **`ky_publicnotice`** (kypublicnotice.com — **NOT** WebForms: Newz
    Group CodeIgniter app): plain POST search
    (`select[name="counties"]`, hidden `page` offset), 5 target counties
    (`KY_FORECLOSURE_COUNTIES`: bell, harlan, knox, perry, whitley), each
    notice = a full newspaper-page PDF (`/KYLegals/<year>/<file>`, text
    layer) whose columns interleave unrelated blocks. Classification is
    therefore **anchored to the strongest sale marker** (Master
    Commissioner's sale / judgment+order of sale...) with a ±700-char
    window; addresses/parcels/dates extract only inside the window.
    **Trap**: the Master Commissioner's own page cites "right of
    redemption ... pursuant to KRS 426.530" and a "Commonwealth of
    Kentucky" caption even on plain mortgage sales — bare those patterns
    flip mortgage → tax; KY tax signals are limited to `delinquent tax(es)
    / department of revenue / revenue cabinet / KRS 134 / ...`.
  - Both scrapers store full notice text as `raw_source_text` and attach
    **Google-Maps links only** (no parcel service in either state — AL/SC
    link policy). Both register in `run.py` `SCRAPER_MODULES` and the
    `run_scraper` per-name branch list; delay ranges (1.5–3.0s) set in
    `config.DELAY_RANGES`.
  - **Shared-regex fix flowing to TN/GA too**: `publicnotice_base.ADDRESS_RE`
    needed **word-boundary-hardened suffix alternation** — the old pattern
    matched suffixes mid-word ("order of sale" captured "00 and an ord"),
    which live proved on interleaved KY newspaper columns; directionals and
    a trailing house number were also added ("Highway 421 South", "Old
    Highway 5"). AL additionally extracts addresses from labeled clauses
    ONLY (`commonly known as` etc.) — metes-and-bounds notices yield NULL
    addresses instead of junk ("1 and 2 of Blackstone Estates", "1944 and
    which plat is record" were live captures); NC's CKA flow is the model.
  - 25 new tests in `tests/test_al_ky_publicnotice.py` (classification
    hyphen/footer/KRS traps, grid skips for bid/Domestic-Relations ads,
    anchor-window address/parcel/date extraction, county sets); full suite
    488 pass. Live runs: AL 34 found (24 mortgage + grid-skipped:: 8) then
    re-verified; KY 8 pages → 1 qualifying Master-Commissioner page.

- **2026-10-20 (Zillow imported from the legacy realestate app)**: Audited
  `~/src/realestate` (the pre-investclosure app) to find the best-performing
  source: **zillow** (76 runs, 5,506 found, 57 qualifying listings/run —
  vs townandmountain 24, landhub 2, and 0 for landwatch/morocks/arg/
  brrealty/wardandward/mls; the old DB itself was empty because its
  `run.py` passed an unsupported `taxnet_url` kwarg to `insert_property`,
  788 TypeErrors in the log). Ported as `scraper/zillow.py`: same
  `__NEXT_DATA__` parsing, but fetching via `camoufox_context` instead of
  curl_cffi (project policy). Zillow's PerimeterX flags a shared browser
  session after ~3 navigations (84/88 counties bot-walled on the first
  port), so **each county gets a fresh Camoufox profile** — replicating the
  legacy per-request-session recipe — with a bot-page cooldown+retry and
  a 12-consecutive-bot-pages circuit breaker. New `scraper/county_centroids.json`
  (89 target counties from realestate's `target_counties_coords.json` +
  Unicoi) adds a centroid guard Zillow's payload demands: searches serve
  same-name *cities* the state guard can't catch (macon-nc served Warren
  County listings 5° from Macon County; dawson-ga returned Dawson GA the
  city, 39842). Filters live in `config.SCRAPER_OVERRIDES["zillow"]`
  (min_acres 5.0, max_price $200k, max_acres 1000 — env-overridable
  via `ZILLOW_MIN_ACRES`/`ZILLOW_MAX_PRICE`/`ZILLOW_MAX_ACRES`);
  DELAY_RANGES zillow (2.5, 4.5). Zillow rows are non-foreclosure market
  listings: `server.property_category()` buckets `source='zillow'` into a
  per-state **Re** tab (Tax | Mtg | Re), and rows carry the Zillow URL —
  per-state **Re** tab (Tax | Mtg | Re), and rows carry the Zillow URL —
  linked from the tile's external-link button and the detail page's
  Source tab ("View Zillow listing" + source/listing ID).
  **Town/ZIP cross-reference**: `scraper/geo_lookup.py` + `town_county.json`
  (from realestate's `target_counties_zips.csv`, Census ZIP→county:
  4,917 ZIPs / 3,625 USPS cities, 6 states) resolves town/ZIP→county when
  coordinates can't; the zillow parser uses it to reject city-name
  collisions with no coordinates (Macon NC 27551 = Warren County, not
  Macon) and to re-hang listings whose town lies outside the searched
  county (Copperhill listings under an unicoi search → Polk; Johnson City
  37604 → Carter). When the town/ZIP maps to several target counties the
  first (alphabetically) is used — the searched county is excluded by the
  cross-reference, so keeping it would knowingly keep a wrong county.
  Also discovered: TNMap's statewide parcel layer currently has ZERO
  parcels for 9 counties flagged `disabled` upstream (Chester, Davidson,
  Hamilton, Hickman, **Knox**, Montgomery, Rutherford, Shelby, Williamson)
  — TNMap enrichment can never match rows there; Knox rows go through
  `kgis_enrich.py` instead.
  `zillow.enforce_thresholds()` runs after every zillow run (post-
  enrichment) and archives active rows outside the CURRENT thresholds —
  tightened defaults and GIS-corrected acreage can't leave strays; the
  initial pass archived 121 of 212 (119 over-$200k/under-5ac + 2 over-cap).
  Note: enrich_properties' below-MIN_ACRES auto-archive can fire on a
  wrong-parcel GIS match — 3 qualifying rows it archived were restored.
  The old curl_cffi zillow's failure counter
  (`data/logs/scraper_failures/zillow.json`, dated 2026-07-27) had to be
  removed to un-disable the name. 9 parser tests in `tests/test_zillow.py`.

- **2026-10-10 (hutchens_law RE-ADDED — the 8/28 removal note below is
  outdated)**: commit `055ed15` restored `scraper/hutchens_law.py`
  (`sales.hutchenslawfirm.com` RadGrid, static single-page table) as a
  `mortgage_foreclosure` source filtered to the 20 NC mountain counties,
  registered in `SCRAPER_MODULES` (runs on the cron/pass schedule as
  before). It is a REAP source (a listing absent from a successful run
  auto-archives as redeemed/sold/withdrawn) and is cross-linked to
  `nc_publicnotice` every run (`db.link_cross_source` hutchens↔nc
  pairing on parcel/court-case/address). The hutchens page publishes no
  parcel/acreage; rows are enriched in the post-run
  `enrich_properties(source='hutchens_law')` pass —
  `search_address_in_nc1map` resolves the street address to a OneMap
  parcel (house-number-guarded), fills acres + parcel_number + deep GIS
  links, and the below-MIN_ACRES auto-archive applies. Known data gap:
  addresses OneMap has no parcel for stay NULL-acres (e.g. #771
  "10 Ross Road", Transylvania — url-only link, no acreage match at the
  source; row since manually archived).

- **2026-10-20 (Haywood bid-module went live — detail-aware rewrite + OCR)**:
  The CivicPlus bid module ("no open postings" since 2026-09-25) now carries
  its first tax foreclosure: `bids.aspx?bidID=267` ("Tax Foreclosure sale
  November 19th @10:00 a.m.", published 10/13). The old scraper only parsed
  list-page link text (no closing date even — the title has no year). Rewrite:
  opens each `bids.aspx?bidID=N` detail, extracts the bid-fields tables
  (single-td label rows followed by separate single-td value rows — one td
  label/value per row, NOT 2-td rows), downloads the Description's
  DocumentCenter PDF, and OCRs it (scanned image, zero text layer) via
  `pdftoppm` 300dpi + `tesseract` (added to Dockerfile + live container;
  `pytesseract` pip-included but the scraper shells out to the CLI instead).
  Parsed from bidID=267: case 26CV000653-430, PIN 8711-00-6993 (new
  `county_static.PARCEL_RE` clause tolerates `Number ("PIN") 8711-00-6993`
  quote/paren separators for Haywood's 3-segment 4-2-4 PINs — the bare dash
  form stays 4-segment only), address "597 Laurel Branch Road" via a
  clause-anchored `described ... as` regex (attorney-footer-safe), notice
  acreage 1.21ac ("containing", fallback-only when OneMap lookup fails), sale
  date 11/19/2026 from Closing Date/Time, full notice text as
  raw_source_text. OneMap resolves the PIN via the McDowell middle-padded
  variant (871100006993) → GIS acres 1.00 **< MIN_ACRES 1.1 → row dropped
  pre-insert** per policy (plat says 1.21ac; GLS/recompute discrepancy —
  flag if you want these kept). Live verified: posting found → detail+PDF +
  OCR in ~26s → correct parse → gate drop logged as
  `dropped_below_min_acres`. 4 new tests in test_county_static.py (parsers,
  label fallback, acreage policy, bidID extraction); full suite 427 pass.

- **2026-10-18 (TN detail-view IP block — proxy back on)**: `tn_publicnotice`
  reported `found=0` on 26 of 27 runs since 2026-10-06. The grid still parsed
  (243 notices/run) but every detail returned the IPA app-level block
  "You are not permitted to view public notices from this computer. Please
  contact IPA." — no Turnstile, so the gate silently returned False and all
  239 details logged a contentless "detail content unavailable". Live probe:
  direct egress = blocked page (350 chars); via the configured proxy the page
  instead shows the normal Turnstile gate and the retried-click in-browser
  solver passes it in one shot (full trustee notice + PDF link). Fix: TN
  `use_proxy` default flipped back to `True` (supersedes the 2026-09-25
  proxy-off note — the failure_retry concern is moot since the click-retry
  fix; proxy details solve in-browser). Also: `_extract_notice_text` now logs
  a text snippet of the page when the gate fails, so block pages are visible
  in logs instead of a bare "content unavailable". NC/GA verified healthy the
  same day (NC 27 details/20 kept with full PDF text; GA 15 candidates all
  seen-or-non-foreclosure). Separate open item: `zls_nc` lost its "All"
  page-size option upstream — only 11 first-page rows are seen (was 241),
  so mountain listings on later pages are missed.
- **2026-10-06 (GIS viewer parcel location + McDowell PIN format)**:
  `/property/976` opened its GIS map at statewide zoom 7 with no parcel —
  (1) `static/gis_viewer.html` never queried the layer by parcel, only dropped
  a marker when a `center` param existed, so parcel-only NC links never located
  the parcel; and (2) McDowell notices print tax parcels `NNNN-NN-NNNN`
  (`0668-15-3806`) while OneMap stores 4-4-4 12-digit PINs (`066800153806`), so
  every lookup variant (raw/de-dashed) missed and the row kept NULL coords.
  Fix: `gis_viewer.html` now queries the parcel through `/gis/proxy` (parno OR
  altparno over raw/dash-stripped/middle-segment-zero-padded variants), zooms
  (`view.goTo`) and highlights the polygon, falling back to the old center pin
  or a not-found message; `_parcel_variants()` gained the padded variant and
  `NC1MapService.by_parcel()` now iterates `_parcel_variants` (was raw-only).
  Re-ran `backfill_links`: #976 resolved to `066800153806` (454 Woodsong Dr,
  4.39ac) + coords; #1286/#1288/#1289 coords/links filled. Verified live via
  camoufox (both new and legacy link forms show "Parcel 066800153806 — SUTTON
  AUSTIN VANCE"). McDowell key-format caveat documented in
  `nc_county_summary.md` §16.
- **2026-10-01 (NC "commonly known as" addresses)**: NC notices publish the
  street address only in a "commonly/also known as <addr>" clause after the
  metes-and-bounds legal description — the NC parser ignored it (address
  stayed NULL, no Maps link). Fix: `_KNOWN_AS_RE` +
  `extract_known_as_address()` in `scraper/nc_publicnotice.py`
  (`_extract_address` now tries the CKA clause before the generic
  "Address:" line); requires house number + NC state token so road aliases
  ("also known as Bristol Avenue") and person aliases ("formerly known as")
  stay rejected; colon form "COMMONLY KNOWN AS:" supported. Backfilled
  existing rows via `python3 -m scraper.backfill_cka` (re-extracts from
  `raw_source_text`; OneMap `siteadd` fallback for parcel-only rows;
  house-number-verified street query `siteadd LIKE '<num> <street>%'` —
  abbreviated street type — so CKA rows get parcel-deep GIS links without
  the wrong-parcel risk of generic address search): 36 addresses, 11 GIS
  links filled; live run then completed clean (13 found / 0 new, dupes
  kept their backfilled addresses). Remaining no-address actives: 968
  (metes-only tax notice, no CKA/PIN) and 975 (OneMap siteadd empty).
- **2026-09-28 (pre-insert MIN_ACRES gate)**: County pages publish no acreage, so county/trustee rows were inserted + Telegram-alerted with NULL acres and only deduped/archived after a later `--all` pass (the #953 case: 0.88ac filled post-insert, below threshold, active until manually archived). Fix: `nc_gis_lookup.fill_acres_in_memory()` runs the NC OneMap lookup BEFORE insert (raw + de-dashed parcel variants — OneMap stores McDowell-style parcels without separators); `county_static.enrich_and_filter_acres()` (4 county scrapers) and `TrusteeSaleScraper.run()` (all trustee scrapers) drop known-below-MIN_ACRES rows pre-insert; unknown acres still kept for the post-insert enricher. TN/GA enrichment stays DB-side (browser-based, too heavy pre-insert).
- **2026-09-25 (per-county search for NC/TN, like GA)**:
  `nc_publicnotice`/`tn_publicnotice` ran one statewide popular search and
  client-filtered, capping coverage (NC 2–3 candidates/run). Both now loop
  per-county checkbox searches like `ga_publicnotice`, via shared
  `_set_county_filter`/`_submit_county_search`/`_widen_grid` helpers in
  `publicnotice_base.py` (NC/TN Go button is `btnGo`, GA's is `btnGo1`).
  Grid-text county stays authoritative (the server filter leaks); TN
  attributes unparseable rows to the searched county only when the search
  ran clean, else drops them as before. Verified live: NC 20/20 counties →
  34 candidates → 13 found/9 new; TN 38 counties → 320 records → 95
  found/87 new, all details in-browser. Drive-by fixes: TN `unico`→`unicoi`
  typo (was silently dropping Unicoi rows everywhere), two-word county
  regex ("Van Buren" parsed as "Buren"), underscore/space normalization in
  the shared county filter, TN count comment 37→38.

- **2026-09-25 (in-browser Turnstile fixed — click ONCE was the bug)**:
  In-browser solving had failed 534/534 times (every detail fell through to
  paid 2captcha). Live probes showed the click machinery works fine — the bug
  was that `_solve_turnstile_in_browser` clicked **once at t+0**, before the
  widget iframe renders (~10-20s: api.js 302 + bundle + challenge HTML), then
  waited passively 90s for a token that needs interaction. Fix: retry the
  click while polling (shared NC/GA/TN base), timeout 90s→60s. Verified live
  with 2captcha rigged to explode: NC 2 details @9s each, GA gilmer @39s, TN
  direct-egress @32s. Supersedes the 2026-09-22 note that "2captcha is the
  only path" — it is now the fallback, not the primary.
  Companion find: TN routed via the IONOS datacenter proxy gets Cloudflare's
  `failure_retry` widget (clicks never yield a token), so `tn_publicnotice`
  default flipped to `use_proxy=False` (same rationale as the GA proxy-off
  commit); direct-egress TN solves in-browser. Keep 2captcha key configured.
- **2026-09-22 (GA/NC public-notice under-harvest audit)**: Investigated why
  `ga_publicnotice`/`nc_publicnotice` pull almost no new actives. Verified they
  **run clean** (no errors) but net-new is ~0 (NC 20 notices→1 new, GA 52→0 over
  14 runs). Root causes + fixes:
  1. **Broken pagination** (the big one): when the ASP.NET JS bundle is absent
     (daytime/datacenter challenge pages), `__doPostBack` is undefined and
     `_goto_next_page` returned `False` → grids capped at **page 1** only. Live
     probe: GA counties report 2–3 pages, `__doPostBack=undefined`; NC log
     `Page 1: 2 kept` then stuck. Fix: `_native_postback` native `__EVENTTARGET`
     form-submit fallback in `_goto_next_page` (shared NC/GA/TN). Verified live:
     NC went 2→**8** notices (reached page 2).
  2. **GA mortgage over-rejection**: GA bank sales advertise under a **"Security
     Deed" / "Power of Sale"** (GA's deed-of-trust equivalent), not NC/TN's
     "deed of trust"/"substitute trustee". `MORTGAGE_FC_PATTERNS` missed GA terms
     so every bank power-of-sale sale was dropped as "non-foreclosure". Added
     `GA_MORTGAGE_FC_PATTERNS` + `_is_mortgage_foreclosure` override. Verified
     from raw JSONL: 7 unique notices recover to the Mtg tab; grand-jury /
     unclaimed / court-order / sheriff-execution stay dropped.
  3. **Turnstile in Docker is unsolvable in-browser**: camoufox ships
     `DEFAULT_SCREEN='1x1x24'` (a bot signal, daijro/camoufox#574/#311). Forcing
     `CAMOUFOX_VIRTUAL_DISPLAY_SIZE=1920x1080x24` (set in `camoufox_context`)
     removes the signal but a datacenter-IP hard challenge still issues **no
     token** (widget renders, never solves). **The 2captcha fallback is the only
     path that actually passes GA/NC detail pages — 2captcha code was NOT deleted
     (earlier doc note was wrong); key is live and being used.** Cost/latency
     ~2 min + a solve per detail. NC sometimes passes in-browser, GA never does.
- **2026-08-29**: Fixed dashboard duplicates — same parcel re-posted under different notice listing IDs (NC republications, GA listing-key format change) created duplicate active rows. `_upsert_property` now also matches `(source, county, parcel_number)` (preferring active rows); 11 stale duplicate rows archived (143 active remain). Regression tests added.
- **2026-08-28**: Removed `hutchens_law` scraper (mortgage-only) and Caldwell County from all scrapers; stale DB rows archived. Cron schedule set to **4a & 4p America/New_York** (`--cron-hours 4,16`, env `CRON_HOURS`). All publicnotice scrapers now limit the search grid to notices **published in the last 7 days** (`LOOKBACK_DAYS` in `scraper/publicnotice_base.py`); newspaper Citizen-Times already used a 7-day rolling window.
- **2026-07-29**: ZLS NC scraper — filtered to NC mountain counties (21 counties), NC OneMap GIS enrichment
- **2026-07-29**: Kania Law scraper — county filtering to 21 NC mountain counties added
- **2026-07-29**: DB cleaned — archived 62 properties from non-qualifying counties (Rowan, Burke, etc.)
- **2026-07-29**: NC OneMap statewide parcel service replaces per-county portal lookups
- **2026-07-26**: Dashboard overhaul — parcel + address on tiles, topo/GIS links, last_seen ordering
- **2026-07-26**: GIS county portal registry for 21 NC mountain counties
- **2026-07-26**: ZLS-NC scraper — all-page size (241 rows), no pagination
- **2026-07-26**: Kania Law — ArcGIS LAND_UNITS acreage, 5-acre filter, GIS enrichment
- **GA in scope**: 7 N GA mountain counties (fannin, gilmer, lumpkin, rabun, towns, union, white) via `ga_publicnotice`; AGENTS.md scope updated to 6 states. `ga_publicnotice` drops non-sale proceedings (quiet-title / tax-redemption, excess-fund interpleaders, foreclosure of equity of redemption) and keeps only upcoming tax-sale foreclosures. **GA tax sales are held on the first Tuesday of every month** (computed from the notice's "first Tuesday in <Month> <Year>"), and **GA has no upset-bid period**. Bundled notices (e.g. White County lists many parcels per notice "by deed/page"; Towns County lists each tax-map parcel in its own block) are split into **separate per-parcel listings** keyed on `<county>:<parcel_number>`; duplicate postings of the same parcel collapse while distinct parcels stay separate.
- **2026-08-25**: `ga_county_summary.md` created — GA qPublic (Schneider Corp) app IDs / pages / KeyValue spacing per county; Lumpkin (`AppID=991`) and White (`AppID=982`) verified and added to `GA_QPUBLIC_APPS` in `scraper/gis_urls.py`. County knowledge bases now tracked in `nc_county_summary.md`, `ga_county_summary.md`, and `tn_county_summary.md` (see "County knowledge base" under GIS Integration).
- **2026-09-10/11**: Dashboard collapsed to per-state **Tax + Mtg tabs** (`property_category()`); TN/NC/newspaper scrapers retain mortgage/deed-of-trust sales as `mortgage_foreclosure` instead of dropping them; per-notice JSONL raw-text logs in `scraper/tmp/<source>_raw_<date>.jsonl`. Five trustee-sale scrapers added sharing `scraper/trustee_base.py` (`rlselaw`, `brockandscott`, `foreclosuretennessee`, `bellcarrington`, `logs_nc` — 49 Mtg rows on trial runs).
- **2026-09-11**: Turnstile solving moved in-browser (camoufox passive wait + checkbox click); 2captcha code deleted after `ERROR_ZERO_BALANCE` (passes off-peak, e.g. 4am cron: 51 classified notices). `pdfplumber` post-mortem: stale image predated the requirement so all PDFs silently fell back to truncated HTML — live-installed, `extract_pdf_text` now ERRORs when missing.
- **2026-09-11**: Append-only `audit_log` table (dashboard archive/unarchive now logged; detail pages show History); search matches parcel/case/listing-id. GA acreage from qPublic reports (`scraper/ga_gis_enrich.py`), TN TPAD/owner/acres (`scraper/tn_gis_enrich.py`); link-depth policy (parcel-deep or nothing; SC/AL get no viewer links). Camp Wahsega #341 + Fern Forest #42 unarchived. **MIN_ACRES 2.0 → 1.1** (55 archived rows sit in [1.1, 2.0)). Rabun Sky Valley lots report Acres 0 at source (kept NULL, not a bug).
- **County-count corrections**: NC mountain set is **20** (Caldwell removed 8/28, docs said 21); TN mountain set is **38** (docs said 37).
- **Dashboard auth**: HTTP Basic Auth on all routes except `/health` (+ static); `users` table with werkzeug hashes, auto-seeded `winmutt` on first request; Admin page (list/add/change-password) and `--add-user` / `--set-password` CLI.

## Tests

Integration tests cover DB operations, archive filtering, county filtering, and GIS enrichment:

```bash
python3 -m pytest tests/ -q                      # Full suite (6 files)
python3 -m pytest tests/test_scraper.py::TestArchiveBelowAcres -v  # Archive tests only
```

### Test Coverage
- **DB CRUD**: insert, dedup, get stats, scrape run tracking
- **Archive**: parameter ordering fix (critical bug), source filtering, acres threshold
- **ZLS NC**: county filtering (mountain vs coastal), GIS enrichment
- **Kania Law**: scraper initialization, county count verification
- **Dedup hash**: case-insensitive, with/without coordinates
- **Tax/Mtg tabs** (`test_tax_mtg_tabs.py`): category mapping, NC/GA/TN kind tagging, newspaper mortgage classifier, JSONL rawlog, in-browser Turnstile solver, GIS link routing, NC cross-state guard
- **Trustee sales** (`test_trustee_sales.py`): shared clean/price/date/address helpers, per-scraper row parsers with recon fixtures
- **Audit + search** (`test_audit_search.py`): audit_log history, parcel/case/listing-id search, status filter, detail History section
