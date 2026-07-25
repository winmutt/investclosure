# AGENTS.md

For full project context, available commands, and architecture, read **[README.md](./README.md)** first.

## Quick Reference

| File | Purpose |
|---|---|
| `scraper/base.py` | BaseForeclosureScraper — captcha solving, acreage parsing, chromium detection |
| `scraper/config.py` | Central config — all paths/thresholds env-overridable, no frozen dataclass |
| `scraper/db.py` | SQLite CRUD — `insert_property()`, `get_stats()`, `archive_below_acres()` |
| `scraper/ncforeclosures.py` | NC ForeclosureScraper — ncnotices.com, 21 counties |
| `scraper/tnforeclosures.py` | TN ForeclosureScraper — tnpublicnotice.com, 95 counties |
| `scraper/run.py` | CLI runner — `python3 -m scraper --list` for commands |
| `.env.example` | Template for all environment variables |
| `data/` | Runtime data — SQLite DB, backups, logs |

## Scrapers

| Scraper | Source | Target | Captcha |
|---|---|---|---|
| `ncforeclosures` | ncnotices.com | 21 NC mountain counties | 2captcha |
| `tnforeclosures` | tnpublicnotice.com | 95 TN counties | 2captcha |

Both are ASP.NET WebForms scrapers with the same architecture:
1. Navigate to site, extract ASP.NET session ID from URL
2. Select "Foreclosure" from dropdown
3. Parse GridView rows (Python JS evaluation to extract pk_id, sp_case, county)
4. For each target county: navigate detail page, solve captcha, extract acreage from notice text

## CLI Commands

```bash
python3 -m scraper --list            # List available scrapers
python3 -m scraper --scraper ncforeclosures   # Run NC only
python3 -m scraper --scraper tnforeclosures   # Run TN only
python3 -m scraper                   # Run both (default)
python3 -m scraper --status          # Show DB stats
python3 -m scraper --new             # Show new properties since last run
python3 -m scraper --archive         # Archive below MIN_ACRES
python3 -m scraper --cron            # Continuous mode (every 360 min)
python3 -m scraper --cron --interval 60      # Every 60 min
```

## Configuration

All paths configurable via env vars — **no hardcoded paths**:

| Variable | Default | Purpose |
|---|---|---|
| `INVESTCLOSURE_DATA_DIR` | `./data` | Base data directory |
| `INVESTCLOSURE_DB_PATH` | `./data/investclosure.db` | SQLite database path |
| `INVESTCLOSURE_BACKUPS_DIR` | `./data/backups` | DB backups directory |
| `INVESTCLOSURE_LOGS_DIR` | `./data/logs` | Log files directory |
| `INVESTCLOSURE_MIN_ACRES` | `10.0` | Minimum acreage filter |
| `INVESTCLOSURE_MAX_ACRES` | `1000.0` | Maximum acreage filter |
| `TWO_CAPTCHA_API_KEY` | *(required)* | 2captcha API key |
| `INVESTCLOSURE_PROXY` | | Proxy `host:port` |

## Run standalone from Python

```python
from scraper.ncforeclosures import NCForeclosureScraper
from scraper.tnforeclosures import TNForeclosureScraper

# Run NC scraper
props = NCForeclosureScraper().run()

# Run with custom settings
scraper = TNForeclosureScraper(
    solve_captcha=True,
    use_proxy=True,
    delay=2.0,  # seconds between requests
)
props = scraper.run()
```

## Debugging

- **Captcha failures**: Check 2captcha balance at https://2captcha.com/in/login, ensure balance > $0.10
- **Chromium not found**: Playwright manages chromium at `~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome`
- **ASP.NET session issues**: ncnotices.com/tnpublicnotice.com session expires; scraper handles gracefully
- **Rate limiting**: Both sites block aggressive scraping; respect `INVESTCLOSURE_PROXY` if needed
- **Logs**: Written to `INVESTCLOSURE_LOGS_DIR/investclosure.log`

## Testing

No test suite yet — the primary test is running scrapers against live sites and verifying
property counts in the DB match expectations.

```bash
# Dry run — no captcha solving
python3 -c "
from scraper.ncforeclosures import NCForeclosureScraper
s = NCForeclosureScraper(solve_captcha=False)
props = s.run()
print(f'Found {len(props)} properties (no captcha)')
"
```

## Recent Updates

- **2026-07-25**: Initial standalone project split from land-scout
  - Two scrapers: NC (21 counties), TN (95 counties)
  - All paths configurable via env vars
  - SQLite DB with dedup and scraping status tracking
  - CLI with --status, --new, --archive, --cron commands
