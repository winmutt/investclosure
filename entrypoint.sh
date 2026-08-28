#!/usr/bin/env bash
set -euo pipefail
mkdir -p /app/data /app/data/backups /app/data/logs /app/reports

# Virtual display for Camoufox (headed Firefox + software WebGL via llvmpipe)
export DISPLAY="${DISPLAY:-:99}"
export LIBGL_ALWAYS_SOFTWARE=1
if [ ! -e "/tmp/.X11-unix/X${DISPLAY#:}" ] && command -v Xvfb > /dev/null 2>&1; then
    Xvfb "${DISPLAY}" -screen 0 1920x1080x24 -nolisten tcp > /dev/null 2>&1 &
    sleep 1
    echo "Xvfb started on ${DISPLAY}"
fi
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
    echo "Starting scraper (daily at ${CRON_HOURS:-4,16} America/New_York)..."
    python -m scraper --cron --interval "${SCRAPE_INTERVAL:-360}" --cron-hours "${CRON_HOURS:-4,16}" &
    echo "Scraper PID: $!"
fi
wait $SERVER_PID
exit 0
