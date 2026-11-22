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

# ---- Dashboard: gunicorn (production WSGI server) -------------------------
# Bind address. Keep 0.0.0.0 only while the published port is the access path;
# docker-compose.realip.yml keeps 0.0.0.0 but on the pasta network (real client
# IP visible). A "127.0.0.1:5001" bind is loopback-only — SSH tunnel in.
WSGI_BIND="${INVESTCLOSURE_BIND:-0.0.0.0:${FLASK_PORT:-5001}}"
WSGI_WORKERS="${INVESTCLOSURE_WORKERS:-2}"
WSGI_THREADS="${INVESTCLOSURE_THREADS:-8}"
# GIS tiles are proxied server-side and the dynamic LiDAR/ortho services take
# 0.2-2s each, so a pan can hold a request open far past gunicorn's 30s default.
WSGI_TIMEOUT="${INVESTCLOSURE_REQUEST_TIMEOUT:-120}"
# xff is logged so an access log line names the client once a proxy sets the
# header (INVESTCLOSURE_TRUST_PROXY=1); %h alone is the Podman bridge IP.
WSGI_LOGFORMAT='%(h)s %(t)s "%(r)s" %(s)s %(b)s %(D)sus xff="%(<{X-Forwarded-For}i)s"'

if [ "${INVESTCLOSURE_DEV_SERVER:-0}" = "1" ]; then
    echo "Starting dashboard (Werkzeug DEV server — no hardening, single thread)..."
    python -m scraper.server &
    SERVER_PID=$!
elif command -v gunicorn > /dev/null 2>&1; then
    echo "Starting dashboard on http://${WSGI_BIND} (gunicorn, ${WSGI_WORKERS} gthread workers x ${WSGI_THREADS} threads)..."
    gunicorn --bind "${WSGI_BIND}" \
        --worker-class gthread \
        --workers "${WSGI_WORKERS}" \
        --threads "${WSGI_THREADS}" \
        --timeout "${WSGI_TIMEOUT}" \
        --graceful-timeout 30 \
        --keep-alive 4 \
        --limit-request-line 4094 \
        --limit-request-fields 50 \
        --limit-request-field_size 8190 \
        --access-logfile - \
        --error-logfile - \
        --access-logformat "${WSGI_LOGFORMAT}" \
        scraper.server:app &
    SERVER_PID=$!
else
    # Only reachable if the image predates gunicorn in requirements.txt.
    echo "WARNING: gunicorn not installed — falling back to the Werkzeug dev server." >&2
    echo "         Rebuild the image (requirements.txt changed) to get it back." >&2
    python -m scraper.server &
    SERVER_PID=$!
fi
echo "Web server PID: ${SERVER_PID}"

# Readiness probe. This image has no curl — the previous loop called it 30
# times, failed 30 times and slept 30s before starting the scraper.
for i in $(seq 1 30); do
    if python -c "
import urllib.request
urllib.request.urlopen('http://127.0.0.1:${FLASK_PORT:-5001}/health', timeout=2).read()
" > /dev/null 2>&1; then
        echo "Dashboard ready at http://127.0.0.1:${FLASK_PORT:-5001}"
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
