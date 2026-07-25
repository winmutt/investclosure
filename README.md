# investclosure

NC & TN foreclosure property scraper — scrapes public foreclosure notices from
ncnotices.com and tnpublicnotice.com, saves results to SQLite.

## Quick Start

```bash
# Bootstrap (one-command setup)
chmod +x bootstrap.sh && ./bootstrap.sh

# Or Docker
docker compose up --build

# Run
python3 -m scraper                  # Run both NC + TN
python3 -m scraper --scraper ncforeclosures   # NC only
python3 -m scraper --list                  # List scrapers
python3 -m scraper --status                # DB stats
python3 -m scraper --new                   # New properties
python3 -m scraper --archive               # Archive small parcels
python3 -m scraper --cron                  # Continuous mode (every 6h)
```

## Requirements

### System
- **Python 3.10+** (3.12 recommended)
- **Playwright Chromium** — installed automatically via `python3 -m playwright install chromium`
- **Docker/Docker Compose** — for containerized deployment (port 5001)

### Python Packages
```
playwright>=1.40    # Headless Chromium automation
requests>=2.31      # HTTP client for 2captcha API
```

### Services
| Service | Required? | Purpose |
|---|---|---|
| 2captcha.com | Yes | Solves reCAPTCHA v2 on foreclosure sites |
| HTTP proxy | No | Optional proxy for bypassing IP blocks |

### Environment Variables

All configurable via `.env` file or shell environment. See `.env.example`:

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

Docker Compose runs on port 5001 (configurable via `FLASK_PORT` env var).

```bash
docker compose up -d --build
```
