# investclosure

NC & TN foreclosure property scraper — scrapes public foreclosure notices,
enriches with county GIS parcel data, saves to SQLite with dashboard.

## Current Status

| Scraper | Working? | Coverage | Notes |
|---|---|---|---|
| `kania_law` | ✅ | ~8/21 counties | GIS enriched via NC1Map POST API |
| `hutchens_law` | ✅ | 26 NC counties | Address-based GIS enrichment |
| `newspaper_notices` | ✅ | NC mountain | State-wide newspaper notices |
| `zls_nc` | ❌ | 21 NC mountain | 0 qualifying mountain records |
| `nc_publicnotice` | ❌ | NC mountain | Banned/unreliable |

### Active Properties (as of 2026-08-03)

| Source | Total | GIS Enriched | Notes |
|---|---|---|---|
| Kania Law | 51 | 12 | Parcel lookup via nparno/altparno strategies |
| Hutchens Law | 23 | 6 | Address matching (street format normalization) |
| Newspaper | 3 | 1 | Mountain counties |
| **Total** | **77** | **19** | 25% enrichment rate |

### Active County Distribution

| County | Count | Notes |
|---|---|---|
| Burke | 8 | Correctly mapped via `altparno + cntyfips` |
| Buncombe | 6 | Mixed (some addresses not in NC1Map) |
| Cherokee | 3 | Long parcel numbers match `nparno` strategy |
| Alleghany | 2 | NC1Map dominant county |
| Ashe | 2 | GIS enriched |
| Polk | 1 | GIS enriched |
| Henderson | 1 | Address-based enrichment |
| Swain | 1 | Address-based enrichment |
| Watauga | 1 | New notice |

## Two-Phase Workflow

The scraper uses a separation of scraping and enrichment phases:

### Phase 1: Scrape (fast, no GIS)
```bash
python3 -m scraper --scraper kania_law   # Scrape listing sites only
python3 -m scraper --scraper hutchens_law
python3 -m scraper --all                 # Run all scrapers
```
Records stored with `acres_source = 'placeholder'`.

### Phase 2: Enrich (GIS lookup)
```bash
python3 -m scraper --enrich              # Enrich all placeholder records
python3 -m scraper --enrich-source kania_law  # Enrich specific source only
```
Queries NC1Map for `acres` data, stores results in DB.

### Full Pipeline
```bash
# Scrape all → enrich → archive
python3 -m scraper --all
python3 -m scraper --enrich
python3 -m scraper --archive
```

## Quick Start

```bash
# Container (recommended)
podman compose up --build       # Build and run
docker compose up --build       # Docker

# Scrape (fast)
python3 -m scraper --scraper kania_law    # Kania Law only
python3 -m scraper --scraper hutchens_law # Hutchens Law only  
python3 -m scraper --all                  # Run all scrapers

# Enrich (GIS lookup)
python3 -m scraper --enrich                       # Enrich all placeholder records
python3 -m scraper --enrich-source kania_law      # Enrich specific source

# DB commands
python3 -m scraper --list            # List scrapers
python3 -m scraper --status          # DB stats
python3 -m scraper --new             # New properties
python3 -m scraper --archive         # Archive small parcels

# Continuous mode
python3 -m scraper --cron            # Every 360 minutes
```

## Requirements

### System
- **Python 3.10+** (3.12 recommended)
- **Playwright Chromium** — installed automatically via `playwright install chromium`
- **Docker** or **Podman** — for containerized deployment (port 5001)

### Python Packages
```
curl-cffi>=0.5       # Modern HTTP client with TLS fingerprinting
beautifulsoup4>=4.12 # HTML parsing
playwright>=1.40     # Headless Chromium automation
```

### Services
| Service | Required? | Purpose |
|---|---|---|
| 2captcha.com | No | Solves reCAPTCHA (disabled for Kania Law) |
| HTTP proxy | No | Optional proxy for bypassing IP blocks |

### Environment Variables

All configurable via `.env` file or shell environment. See `.env.example`:

| Variable | Default | Purpose |
|---|---|---|
| `INVESTCLOSURE_DATA_DIR` | `./data` | Base data directory |
| `INVESTCLOSURE_DB_PATH` | `./data/investclosure.db` | SQLite database |
| `INVESTCLOSURE_BACKUPS_DIR` | `./data/backups` | DB backups |
| `INVESTCLOSURE_LOGS_DIR` | `./data/logs` | Log files |
| `INVESTCLOSURE_MIN_ACRES` | `5.0` | Minimum acreage filter |
| `INVESTCLOSURE_MAX_ACRES` | `1000.0` | Maximum acreage filter |
| `INVESTCLOSURE_PROXY` | | Proxy `host:port` (optional) |

## Scrapers

| Scraper | Source | Counties | Captcha | GIS Enrichment |
|---|---|---|---|---|
| `kania_law` | kaniabailbond.com | 21 NC mountain | None | NC OneMap POST API |
| `newspaper_notices` | NC newspapers | Mountain NC | None | NC OneMap POST API |
| `hutchens_law` | sales.hutchenslawfirm.com | 26 NC | None | Address-based NC1Map |
| `zls_nc` | zls-nc.com/listings | 21 NC mountain | None | NC OneMap POST API |

### Selection Criteria

Properties filtered to **21 NC mountain counties** (elevation >1700ft, within 250mi of Atlanta):

`alleghany, ashe, avery, buncombe, burke, cherokee, clay, graham, haywood, henderson, jackson, madison, mcdowell, mitchell, polk, macon, swain, transylvania, watauga, yancey`

- **Kania Law**: NC tax foreclosure auctions — scrapes ALL records from API, filters to qualifying counties
- **Hutchens Law**: NC foreclosure listing with 26 NC counties (+franklin, macon, polk, rutherford)
- **ZLS-NC / Zacchaeus Legal Services**: NC tax foreclosure listings — filters to mountain counties only

**Excluded from NC**: Rowan, Rutherford, Cleveland, Catawba, Gaston (foothills, <1700ft), Stokes, Davie, Harnett (outside 250mi radius).

**GA data unavailable**: Georgia's state data hub (data-hub.gio.georgia.gov) contains 0 parcel data sources. Would require per-county research.

### GIS Integration

- **NC OneMap Parcels API**: POST queries to `services.nconemap.gov/secure/rest/services/NC1Map_Parcels/MapServer/1/query`
  - Requires `application/x-www-form-urlencoded` form data
  - Returns JSON with parcel data
- **Primary fields**:
  - `gisacres` — GIS acreage (Double type)
  - `parno` — parcel number (String)
  - `altparno` — alternate parcel number (short numbers like Burke 32724)
  - `nparno` — national parcel number (format: `37{fips}_{parno}`)
  - `cntyfips` — county FIPS code (NC1Map specific)
  - `cntyname` — county name
  - `siteadd` — site address (abbreviated format)
  - `sourceref` — deed book/page reference
- **Lookup strategies**:
  1. `cntyfips='XXX' AND nparno='37XXX_YYY'` — National parcel query
  2. `cntyfips='XXX' AND altparno='YYY'` — Alternate parcel query (Burke, etc.)
  3. `cntyfips='XXX' AND parno='YYY'` — Primary parcel query
  4. `cntyfips='XXX' AND siteadd LIKE '%STREET%TYPE%'` — Address-based (Hutchens)
- **NC1Map FIPS codes**: NC1Map uses different codes than Census Bureau
  - Cherokee=039 (NC1Map), NOT 035 (Census)
  - Clay=043 (NC1Map), NOT 037 (Census)
  - Burke=023 (NC1Map)
- **Address matching**: Hutchens records use normalized street format (e.g., "OVERLOOK DR" → "OVE DR")

### Kania Law Enrichment

Kania Law properties are enriched with NC OneMap parcel data via multi-strategy lookup:

```python
from scraper.nc_gis_lookup import enrich_kania_record
from scraper.nc_gis_lookup import enrich_properties

# Real-time enrichment
enriched = enrich_kania_record(kania_record)

# Batch enrichment (after scrape)
result = enrich_properties(source="kania_law")
# Returns: {"enriched": N, "skipped_no_parcel": M, "failed": P}
```

### Hutchens Law Enrichment

Hutchens records have deed book/page but no parcel number. Enrichment uses address normalization:

```python
from scraper.nc_gis_lookup import enrich_hutchens_properties

# Enrich all Hutchens records
result = enrich_hutchens_properties()
# Returns: {"enriched": N, "skipped_no_address": M, "failed": P}
```

Address normalization: "16 Overlook Drive" → "OVERLOOK DR" → query NC1Map with LIKE pattern.

Enriched fields:
| Field | Description | Source |
|---|---|---|
| `acres` | GIS-sourced acreage (from gisacres) | NC1Map Parcels |
| `owner_name` | Property owner name | NC1Map Parcels |
| `land_use` | Land use code/description | NC1Map Parcels |
| `gis_url` | NC OneMap parcel viewer URL | NC1Map |
| `google_maps_url` | Google Maps link | Google Maps |

## DB Schema

Single `properties` table with dedup via SHA-256 on address+coords and source+listing_id.
Sort order: `ORDER BY last_seen DESC` so recently-updated properties bubble to top of dashboard.

Key columns:
- `id`, `source`, `source_listing_id`, `address`, `county`, `state`
- `price_cents`, `acres`, `acres_source`, `land_use`
- `description`, `property_type`, `parcel_number`
- `gis_url`, `google_maps_url`, `google_maps_topo_url`
- `auction_date`, `close_date`, `upset_bid`
- `first_seen`, `last_seen`, `seen_count`, `status`
- `gis_county`, `owner_name`, `tags`, `notes`, `scraped_at`

## Recent Updates

- **2026-08-03**: Two-phase workflow — scrape (fast) + enrich (GIS lookup) phases
- **2026-08-03**: NC1Map parcel lookup uses `nparno` (National format), `altparno` (Alternate format), and `parno` strategies with `cntyfips` filter
- **2026-08-03**: Hutchens Law address-based enrichment (normalized street format)
- **2026-08-03**: Burke parcels correctly resolved via `altparno + cntyfips=023`
- **2026-08-03**: 19/77 properties enriched (Kania: 12, Hutchens: 6, Newspaper: 1)
- **2026-08-02**: Removed Zillow scraper (banned) and broken foreclosure scrapers
- **2026-07-29**: Kania Law — county filtering to 21 NC mountain counties
- **2026-07-26**: Dashboard overhaul — parcel + address on tiles, topo/GIS links
