#!/usr/bin/env bash
# Start the scraper in cron mode with configurable interval.
# Usage: ./start.sh [interval_in_minutes]

set -euo pipefail

INTERVAL="${1:-${SCRAPE_INTERVAL:-360}}"
exec python -m scraper --cron --interval "$INTERVAL"
