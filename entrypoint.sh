#!/usr/bin/env bash
set -euo pipefail
mkdir -p /app/data /app/data/backups /app/data/logs /app/reports
echo "Starting dashboard on http://0.0.0.0:${FLASK_PORT:-5001}..."
python -m scraper.server &
SERVER_PID=$!
echo "Web server PID: $SERVER_PID"
for i in $(seq 1 30); do
    if curl -s "http://localhost:${FLASK_PORT:-5001}/health" > /dev/null 2>&1; then
        echo "Dashboard ready at http://localhost:${FLASK_PORT:-5001}"
        break
    fi
    sleep 1
done
if [ "${RUN_CRON:-true}" = true ]; then
    echo "Starting scraper (interval: 360 minutes)..."
    python -m scraper --cron --interval 360 &
    echo "Scraper PID: $!"
fi
wait $SERVER_PID
exit 0
