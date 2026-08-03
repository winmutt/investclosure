#!/usr/bin/env bash
# Entry point for investclosure — runs web server and scraper.

set -euo pipefail

# Create data directories
mkdir -p /app/data /app/data/backups /app/data/logs /app/reports

# Start the Flask web server
echo "Starting dashboard on http://0.0.0.0:${FLASK_PORT:-5001}..."
python -m scraper.server &
SERVER_PID=$!
echo "Web server PID: $SERVER_PID"

# Wait for server to be ready
for i in $(seq 1 30); do
    if curl -s "http://localhost:${FLASK_PORT:-5001}/health" > /dev/null 2>&1; then
        echo "Dashboard ready at http://localhost:${FLASK_PORT:-5001}"
        break
    fi
    sleep 1
done

# Run scraper in cron mode in the background (non-blocking)
echo "Starting scraper (interval: ${SCRAPE_INTERVAL:-360} minutes)..."
python -m scraper --cron --interval "$SCRAPE_INTERVAL" &
SCRAPER_PID=$!
echo "Scraper PID: $SCRAPER_PID"

# Keep the container alive listening for the web server
wait $SERVER_PID

exit 0
