# investclosure

NC & TN foreclosure property scraper — scrapes public foreclosure notices from
ncnotices.com and tnpublicnotice.com, saves results to SQLite.

## Quick Start

```bash
# 1. Clone
git clone https://github.com/<user>/investclosure.git
cd investclosure

# 2. Configure
cp .env.example .env
# Edit .env — set TWO_CAPTCHA_API_KEY (required for captcha solving)

# 3. Run
python -m scraper                  # Run both NC + TN scrapers
python -m scraper --scraper ncforeclosures   # NC only
python -m scraper --list                 # List scrapers
python -m scraper --status               # DB stats
python -m scraper --new                  # New properties
python -m scraper --archive              # Archive small parcels
python -m scraper --cron                 # Continuous mode
python -m scraper --cron --interval 60   # Every 60 min

# 4. Docker
docker compose up --build
```

## Configuration

All settings configurable via environment variables (or `.env` file):

| Variable | Default | Purpose |
|---|---|---|
| `TWO_CAPTCHA_API_KEY` | *(required)* | 2captcha key for solving reCAPTCHA |
| `INVESTCLOSURE_DATA_DIR` | `./data` | Base data directory |
| `INVESTCLOSURE_DB_PATH` | `./data/investclosure.db` | SQLite database |
| `INVESTCLOSURE_BACKUPS_DIR` | `./data/backups` | DB backups |
| `INVESTCLOSURE_LOGS_DIR` | `./data/logs` | Log files |
| `INVESTCLOSURE_MIN_ACRES` | `10.0` | Minimum acreage filter |
| `INVESTCLOSURE_MAX_ACRES` | `1000.0` | Maximum acreage filter |
| `INVESTCLOSURE_PROXY` | | Proxy `host:port` (optional) |
| `NCFORECLOSURES_BASE_URL` | `https://www.ncnotices.com` | NC site URL |
| `TNFORECLOSURES_BASE_URL` | `https://www.tnpublicnotice.com` | TN site URL |

## Scrapers

| Scraper | Source | Counties |
|---|---|---|
| `ncforeclosures` | ncnotices.com | 21 NC mountain counties |
| `tnforeclosures` | tnpublicnotice.com | 95 TN counties |

Both use Playwright (headless Chromium) with reCAPTCHA solving via 2captcha.

## DB Schema

Two tables: `properties` (active/scraped listings) and `scrape_runs` (run tracking).
Dedup via SHA-256 on address+coords and source+listing_id.

## Deployment

Docker Compose runs on port 5001 (configurable via `FLASK_PORT`).

```bash
docker compose up -d --build
```

See `Dockerfile` for full dependency list.
