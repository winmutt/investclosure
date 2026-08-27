"""One-off: fetch the PDF for a single ncnotices.com listing (962670 / 26CV000327-040)."""
import sys, os, io, time, logging, html as _html
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("fetch_one")

sys.path.insert(0, "/app")
from scraper.ncforeclosures import (
    NCForeclosureScraper, DOWNLOAD_LINK, NCNOTICES_TURNSTILE_SITE_KEY, NCNOTICES_EMAIL, NCNOTICES_PASSWORD,
)
from scraper import ncforeclosures as ncf
from urllib.parse import urljoin

TARGET_ID = "962670"
DB_PATH = "/app/data/investclosure.db"
PDF_OUT = "/app/data/26CV000327-040.pdf"

scraper = NCForeclosureScraper()

from camoufox.sync_api import Camoufox
with Camoufox(headless="virtual", humanize=False) as browser:
    page = browser.new_page()
    page.set_viewport_size({"width": 1600, "height": 1000})
    page.set_default_timeout(45000)
    if not scraper._login(page):
        print("LOGIN FAILED")
        sys.exit(1)
    sid = scraper._extract_session(page.url)
    url = f"{scraper.BASE_URL}/(S({sid}))/Details.aspx?SID={sid}&ID={TARGET_ID}"
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2000)
    if not scraper._pass_detail_gate(page, TARGET_ID):
        print("DETAIL GATE FAILED")
        sys.exit(1)
    # download PDF bytes
    el = page.query_selector(DOWNLOAD_LINK)
    if not el:
        print("NO PDF LINK")
        sys.exit(1)
    href = _html.unescape(el.get_attribute("href"))
    abs_href = urljoin(page.url, href)
    resp = page.request.get(abs_href, timeout=60000)
    data = resp.body()
    print("PDF bytes:", len(data), "header:", data[:4])

def html_unescape(s): return _html.unescape(s)

# save PDF
with open(PDF_OUT, "wb") as f:
    f.write(data)
print("saved PDF ->", PDF_OUT)

# extract text
text = ncf._extract_pdf_text(data)
print("PDF text chars:", len(text) if text else 0)

if text:
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE properties SET raw_source_text=?, description=? WHERE id=239",
        (text, text[:2000]),
    )
    conn.commit()
    conn.close()
    print("DB updated for id=239")
