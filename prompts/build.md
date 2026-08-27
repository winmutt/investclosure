# Prompt: Build investclosure

Build a standalone foreclosure property scraper for NC and TN public notice sites.
This is a **new project** in `/opt/opencode/src/investclosure/`.
Do NOT import from or reference the land-scout project.

## Project overview

Scrapes foreclosure notices from two ASP.NET WebForms sites:
- **ncnotices.com** — 21 NC mountain counties
- **tnpublicnotice.com** — 95 TN counties

Both sites use session-based URLs and reCAPTCHA on detail pages, solved via 2captcha API.
Results are saved to a local SQLite database with deduplication.

## File structure

```
investclosure/
├── scraper/__init__.py          # Empty package marker
├── scraper/__main__.py          # python -m scraper entry point
├── scraper/base.py              # BaseForeclosureScraper (captcha, acreage, chromium detection)
├── scraper/config.py            # Standalone config — all paths/thresholds env-overridable
├── scraper/db.py                # SQLite CRUD (schema inline, no external file)
├── scraper/nc_publicnotice.py   # NC scraper (21 counties)
├── scraper/ga_publicnotice.py   # GA scraper (7 N mountain counties)
├── scraper/tn_publicnotice.py   # TN scraper (37 counties)
├── scraper/run.py               # CLI runner
├── .env.example
├── .gitignore
├── bootstrap.sh                 # One-command setup script
├── requirements.txt
├── Dockerfile                   # Port 5001 (configurable via FLASK_PORT)
├── docker-compose.yml
├── AGENTS.md
└── README.md
```

## Key design decisions

1. **Standalone** — No dependencies on land-scout. `scraper` is a self-contained Python package.
2. **All paths configurable** — File-system paths via `INVESTCLOSURE_*` env vars. No hardcoded `/opt/...` paths.
3. **Deferred API key validation** — `TWO_CAPTCHA_API_KEY` is optional at import time. Validated at runtime in `BaseForeclosureScraper.run()`.
4. **SQLite schema inline** — Schema stored in `db.py.SCHEMA` string, applied via `executescript()`. No separate `.sql` file.
5. **No frozen dataclass** — `config.Config` is a mutable dataclass so `__init__` can assign fields.
6. **No PII/secrets in git** — `.gitignore` excludes `.env`, `*.db`, `*.log`, `data/`, `reports/`.

## Implementation details

### 1. `scraper/__init__.py`
Empty file. Just `touch scraper/__init__.py`.

### 2. `scraper/__main__.py`
```python
from scraper.run import main
if __name__ == "__main__":
    main()
```

### 3. `scraper/config.py`

Must contain:
- Helper functions: `_required()`, `_opt_str()`, `_opt_float()`, `_opt_int()`
- Site constants (env-overridable):
  - `NCFORECLOSURES_BASE_URL = "https://www.ncnotices.com"` (env: `NCFORECLOSURES_BASE_URL`)
  - `NCFORECLOSURES_CAPTCHA_SITE_KEY = "6LeRyw8sAAAAAJRiqeOxNIyHIZ0c4y6sL-qWbt63"`
  - `NCFORECLOSURES_POPULAR_SEARCH_VALUE = "6"`
  - `TNFORECLOSURES_BASE_URL = "https://www.tnpublicnotice.com"`
  - `TNFORECLOSURES_CAPTCHA_SITE_KEY = "6LdtSg8sAAAAADTdRyZxJ2R2sS82pKALNMvMqSyL"`
  - `TNFORECLOSURES_POPULAR_SEARCH_VALUE = "4"`
- County lists:
  - `NC_FORECLOSURE_COUNTIES` (21 counties, lowercase): alleghany, ashe, avery, buncombe, burke, catawba, cherokee, clay, graham, haywood, henderson, jackson, madison, mcdowell, mitchell, swain, transylvania, watauga, wilkes, yancey
  - `TN_FORECLOSURE_COUNTIES` (95 counties, lowercase): anderson, bedford, benton, bledsoe, blount, bradley, campbell, cannon, carroll, carter, cheatham, chester, claiborne, clay, cocke, coffee, crockett, cumberland, davidson, decatur, dekalb, dickson, dyer, fayette, fentress, franklin, gibson, giles, grainger, greene, grundy, hamblen, hamilton, hancock, hardeman, hardin, hawkins, haywood, henderson, henry, hickman, houston, humphreys, jackson, jefferson, johnson, knox, lake, lauderdale, lawrence, lewis, lincoln, loudon, macon, madison, marion, marshall, maury, mcminn, mcnairy, meigs, monroe, montgomery, moore, morgan, obion, overton, perry, pickett, polk, putnam, rhea, roane, robertson, rutherford, scott, sequatchie, sevier, shelby, smith, stewart, sullivan, sumner, tipton, troubsdale, unaioi, union, vanburen, warren, washington, wayne, weakley, white, williamson, wilson
  - `TNFORECLOSURES_COUNTY_INDICES` dict mapping each TN county to 0-based index (anderson=0, bedford=1, ..., wilson=94)
- `Config` dataclass (NOT frozen):
  - Fields: `TWO_CAPTCHA_API_KEY`, `MIN_ACRES` (default 10.0), `MAX_ACRES` (default 1000.0), `MAX_PRICE` (default 0), `PROXY_URL`, `DELAY_RANGE`, `CAPTCHA_ENABLED`, `PROXY_ENABLED`, `data_dir`, `db_path`, `backups_dir`, `logs_dir`
  - Path resolution priority: explicit constructor args > env vars (`INVESTCLOSURE_DATA_DIR`, etc.) > defaults (`./data/`, etc.)
  - `TWO_CAPTCHA_API_KEY` read via `_opt_str("")` (NOT `_required()`) — validated at runtime
  - Ensure directories exist after init: `self.backups_dir.mkdir()`, `self.logs_dir.mkdir()`
- Singleton: `config = Config()`

### 4. `scraper/base.py`

Must contain:
- `PropertyData` TypedDict with all standard fields: source, source_listing_id, url, address, city, county, state, zip_code, latitude, longitude, price, acres, description, property_type ("foreclosure"), image_url, parcel_number, auction_date, close_date, upset_bid, foreclosure_key
- `BaseForeclosureScraper` ABC (abstract base class):
  - `SOURCE_NAME`, `BASE_URL` class attributes
  - `__init__()`: search_type, delay, use_proxy, solve_captcha, delay_range=(delay, delay*2)
  - `scrape()`: abstract, must raise NotImplementedError
  - `run()`: validates TWO_CAPTCHA_API_KEY at runtime (exits with fatal message if missing), prints header, calls self.scrape(), filters by county and acreage, returns qualifying properties
  - `_extract_acreage(text)`: regex patterns for acre extraction from notice text
  - `_extract_session(url)`: extract ASP.NET session ID via regex
  - `_solve_captcha(url, site_key)`: POST to 2captcha, poll for solution
  - `_inject_token_and_submit(page, token)`: JS eval to set g-recaptcha-response and __doPostBack
  - `_find_chromium()`: static method to find chromium executable via playwright cache and PATH

### 5. `scraper/db.py`

Must contain:
- `SCHEMA` string with `CREATE TABLE IF NOT EXISTS properties` and `CREATE TABLE IF NOT EXISTS scrape_runs`
  - properties table includes: source, source_listing_id, url, address, city, county, state, zip_code, latitude, longitude, price_cents, acres, description, property_type, listing_date, auction_date, close_date, upset_bid, foreclosure_key, first_seen, last_seen, seen_count, dedup_hash, status (default 'active'), tags, notes, scraped_at
  - scrape_runs table: source, started_at, finished_at, properties_found, properties_new, properties_duplicate, status, error_message
  - Indexes on: source, county+state, status, dedup_hash, first_seen
- `_ensure_db(db_path)`: connect, PRAGMA journal_mode=WAL, PRAGMA foreign_keys=ON, executescript(SCHEMA), row_factory=Row
- `compute_dedup_hash(address, city, county, state, zip_code, lat, lon)`: SHA-256 of normalized fields
- `_upsert_property()`: check source+listing_id first, then dedup_hash, return "new"/"duplicate"
- `insert_property()`: wrapper for _upsert_property
- `start_scrape_run(conn, source)`: insert run record
- `update_scrape_run(conn, run_id, ...)`: update run record
- `get_new_since(conn, since_date, source=None, limit=100)`: fetch new properties
- `get_all_active(conn, limit=100, source=None)`: fetch active properties
- `archive_below_acres(conn, min_acres, source=None)`: update status to 'archived'
- `get_stats(conn)`: aggregate stats dict

### 6. `scraper/nc_publicnotice.py`

Must contain:
- `NCPublicNoticeScraper(PublicNoticeScraper)`:
  - `SOURCE_NAME = "nc_publicnotice"`
  - `BASE_URL = NCFORECLOSURES_BASE_URL`
  - `_get_target_counties()`: returns set(NC_FORECLOSURE_COUNTIES)
  - `scrape()`: Playwright flow:
    1. Launch headless chromium with playwright (handle ImportError)
    2. Navigate to self.BASE_URL, extract session ID
    3. Select "Foreclosure" via select[name="ctl00$ContentPlaceHolder1$as1$ddlPopularSearches"] with value NCFORECLOSURES_POPULAR_SEARCH_VALUE
    4. Wait 8s for page load
    5. Run JS to parse GridView rows: extract pk_id, sp_case (regex: 2[456]SP\d+[\w-]*), county (regex patterns for NORTH CAROLINA + COUNTY, SUPERIOR/DISTRICT Court DIVISION + COUNTY), detail_url from onclick
    6. Filter records to target counties
    7. For each record: navigate detail page, check for captcha, solve if needed, extract notice text, get acres
    8. Build PropertyData dict with source, source_listing_id, url, county (lowercase), state "NC", acres, description[:2000]
    9. Return list of properties
- `scrape_all()`: convenience function

### 7. `scraper/tn_publicnotice.py`

Identical structure to nc_publicnotice.py (both subclass `PublicNoticeScraper`) with:
- `SOURCE_NAME = "tn_publicnotice"`
- `BASE_URL = TNFORECLOSURES_BASE_URL`
- `_get_target_counties()`: returns set(TN_FORECLOSURE_COUNTIES)
- Same _search_foreclosures but use TNFORECLOSURES_POPULAR_SEARCH_VALUE
- Different county regex patterns in JS (TENNESSEE + COUNTY, COUNTY + TENNESSEE, County Courthouse patterns)
- Different sp_case regex (\d+SP\d+[-\w]*) instead of 2[456]SP\d+[\w-]*
- `state="TN"` in PropertyData

### 8. `scraper/run.py`

Must contain:
- `SCRAPERS = {"nc_publicnotice": NCPublicNoticeScraper, "ga_publicnotice": GAPublicNoticeScraper, "tn_publicnotice": TNPublicNoticeScraper}`
- `run_scraper(conn, name, cls)`: start_logging, scraper.run(), iterate props checking for source+listing_id duplicate, end_logging
- `cmd_list()`, `cmd_run(scraper_name)`, `cmd_run_all()`, `cmd_status()`, `cmd_new()`, `cmd_archive(threshold)`, `cmd_cron(minutes=360)`
- `main()`: argparse with --scraper, --all, --list, --status, --new, --archive, --threshold, --cron, --interval
- Logging to config.logs_dir/investclosure.log

### 9. `.gitignore`
Must exclude: playwright, .env, *.db, *.db-journal, *.db-wal, *.db-shm, data/, *.log, __pycache__/, *.pyc, .pytest_cache/, .stestr/, .mypy_cache/, .tox/, .eggs/, *.egg-info/, dist/, build/, .venv/, venv/, .env.*, *.key, *.pem, *.crt, .DS_Store, Thumbs.db

### 10. `bootstrap.sh`
Executable bash script that:
1. Creates .env from .env.example (interactive if TWO_CAPTCHA_API_KEY missing)
2. Creates data directories
3. Installs pip deps (playwright, requests)
4. Installs Playwright Chromium (or skips for Docker mode via --no-docker flag)
5. Runs `python -m scraper --list` as verification

### 11. `requirements.txt`
```
playwright>=1.40
requests>=2.31
```

### 12. `Dockerfile`
python:3.12-slim base. Install chromium via playwright install. COPY scraper/ to /app/. CMD ["python", "-m", "scraper"]. EXPOSE 5001.

### 13. `docker-compose.yml`
service `investclosure` with build: ., container_name: investclosure, ports: 5001:5001, env_file: .env, volumes: ./data:/app/data, restart: unless-stopped

### 14. `.env.example`
Template with all known env vars, TWO_CAPTCHA_API_KEY set to placeholder, data paths set to defaults.

### 15. `AGENTS.md`
Reference doc with: CLI commands table, scraper details (source, counties, captcha), all-configurable paths table, standalone Python usage example, debugging tips.

### 16. `README.md`
Project description, quick start (bootstrap, docker, run commands), requirements section (system deps, python packages, services), config table, scrapers table, db schema note, deployment instructions.

## Testing criteria

After building, verify:
1. `cd /opt/opencode/src/investclosure && python -m scraper --list` outputs both scrapers
2. `python -m scraper --status` runs without error (shows empty stats)
3. No import errors when loading any module
4. `.gitignore` excludes sensitive files

## Important constraints

- Do NOT import from land-scout or any other project
- Do NOT hardcode paths - all paths must be configurable via env vars
- Do NOT use frozen=True dataclass (Config needs mutable __init__)
- Do NOT require TWO_CAPTCHA_API_KEY at import time (deferred validation)
- Do NOT commit any secrets, credentials, or PII
- Both scrapers use Playwright + headless Chromium, not Selenium
- Schema is inline in db.py, no external SQL files
- No Flask/web server needed (CLI-only, Docker exposes port 5001 for monitoring)
