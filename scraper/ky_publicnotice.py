"""KY Public Notice foreclosure scraper — kypublicnotice.com (KPA / Newz Group).

kypublicnotice.com is NOT the ASP.NET "Public Notice" platform used by
NC/TN/GA/AL: it is a CodeIgniter/PHP app by Newz Group with

* a plain POST search form (``/index.php/main/search``) with a ``counties``
  select, a hidden ``page`` offset that persists filters across pagination,
  and 25 rows per page (capped at 500 results per search),
* result cards keyed by hidden ``filename{id}`` / ``pubdate{id}`` / ``link{id}``
  inputs listing the publication, city, and "County: <Name>",
* each notice is a NEWSPAPER-PAGE PDF under
  ``https://kypublicnotice.com/KYLegals/<year>/<filename>`` (freely
  downloadable; a full page — front/classified/legal pages — with a text
  layer in most cases, OCR fallback for scans).

A "page" may bundle several unrelated newspaper items, so the classifier
requires strong sale markers (Master Commissioner's sale, judicial sale,
order of sale...) and the extracted address/parcel must sit near one of
those anchors to be attributed to the property. Kentucky judicial
foreclosures (circuit-court Master Commissioner sales) go to the Mtg tab;
tax/judicial sales for delinquent property taxes (KRS 134 / Commonwealth
revenue warrants) go to the Tax tab.

Kentucky publishes no statewide parcel service in our registry (GIS link
policy: Maps links only, like AL/SC).
"""
from __future__ import annotations
import logging
import re
from datetime import date, timedelta
from typing import List, Optional

from .base import BaseForeclosureScraper, PropertyData
from .config import (
    config,
    KYFORECLOSURES_BASE_URL,
    KY_FORECLOSURE_COUNTIES,
)
from .publicnotice_base import (
    LOOKBACK_DAYS,
    MORTGAGE_FC_PATTERNS,
    STREET_TAIL_BODY,
    HWYN_TAIL_BODY,
    extract_pdf_text,
    normalize_notice_text,
)
from .rawlog import log_raw

logger = logging.getLogger(__name__)

COUNTY_SET = set(KY_FORECLOSURE_COUNTIES)
SEARCH_URL = KYFORECLOSURES_BASE_URL + "/index.php/main/search"

# Kentucky judicial-sale markers. A newspaper legal page only qualifies when
# one of these appears; random classified-ad pages never do.
_KY_SALE_MARKERS = [
    r"master\s+commissioner",
    r"commissioner'?s?\s+sale",
    r"judicial\s+sale",
    r"order\s+of\s+sale",
    r"decree\s+and\s+order\s+of\s+sale",
    r"foreclosure\s+(?:sale|judgment)",
    r"offered\s+for\s+sale\s+at\s+public\s+auction",
    r"sell(?:s)?\s+at\s+public\s+(?:auction|sale)",
    r"notice\s+of\s+sale",
    r"sale\s+of\s+real\s+(?:property|estate)",
]
# Court *service* publications — no single parcel, no auction (mirrors TN's
# service-publication guard). Generic "summons" is NOT a signal: a real
# Master-Commissioner page may cite a summons; those pages without any sale
# marker are dropped by the classifier anyway.
_KY_PUBLICATION_PATTERNS = [
    r"service\s+by\s+publication",
    r"notice\s+of\s+publication",
    r"last\s+known\s+address\s+of\s+a\s+defendant",
]
# Personal-property / lien-contract ads are not land sales.
_KY_PERSONAL_PROPERTY = [
    r"abandoned\s+(?:vehicle|vessel|mobile home unit)",
    r"self\s*storage|mini\s*storage.{0,40}\blien\b",
    r"sale\s+of\s+(?:a\s+)?(?:198\d|20\d\d)\s+\w+",
]

# Mortgage/judicial-foreclosure signals (KY is a mortgage+judgment state;
# sales run through the circuit court's Master Commissioner).
KY_MORTGAGE_PATTERNS = MORTGAGE_FC_PATTERNS + [
    r"master\s+commissioner",
    r"decree\s+and\s+order\s+of\s+sale",
    r"foreclos(?:ure|e)s?",  # "foreclosure" / "foreclose"
    r"judgment\s+(?:and\s+)?order\s+of\s+sale",
    r"mortgag(?:e|or)",  # careful: also matched by tax notices' boilerplate
    r"writ\s+of\s+execution",
]
# Tax-foreclosure signals: delinquent-property-tax judicial sales
# (KRS ch. 134 / §426.5x), filed by the Commonwealth / county attorney.
# NOTE: bare "Commonwealth of Kentucky" and "KRS 426" are NOT tax signals —
# the Master-Commissioner caption cites the Commonwealth and its ads
# boilerplate-mention "right of redemption ... pursuant to KRS 426.530" for
# plain mortgage sales too (live page 70224 misclassified on those).
KY_TAX_PATTERNS = [
    r"delinquent\s+prop(?:e)?rty\s+tax(?:es)?",
    r"delinquent\s+tax(?:es)?",
    r"department\s+of\s+revenue",
    r"revenue\s+cabinet",
    r"k\.?r\.?s\.?\s*134",
    r"tax\s+sale",
    r"for\s+the\s+use\s+and\s+benefit",
    r"ex\s+rel\.",
]

_KY_COUNTY_MARKER_RE = re.compile(r"county:\s*([A-Za-z]+)", re.IGNORECASE)
_KY_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}
_KY_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})",
    re.IGNORECASE)
# Sale-intent anchors immediately before a candidate date.
_KY_SALE_ANCHORS = (
    "will be sold", "be offered for sale", "public auction",
    "public sale", "sale will", "will sell", "cry of sale",
    "date of sale", "auction date", "sale date", "offered for sale",
)
_KY_DEED_ANCHORS = (
    "dated", "recorded", "executed", "conveyed", "of record",
    "instrument", "judgment entered", "final judgment",
)
# Window (chars) around the strongest sale marker within which extracted
# addresses/parcels are attributed to the property being sold.
_ANCHOR_WINDOW = 700
# PDF-extraction junk ("...SUNSet ocuntoy" runs) — no real street name has a
# 20+ letter run (same rule the shared extractor uses).
_LONG_RUN_RE = re.compile(r"[A-Za-z]{20,}")


def _best_marker_pos(text: str) -> Optional[int]:
    """Position of the first (strongest) sale marker occurrence, or None."""
    low = (text or "").lower()
    for pat in _KY_SALE_MARKERS:
        m = re.search(pat, low)
        if m:
            return m.start()
    return None


def _is_publication_notice(text: str) -> bool:
    low = (text or "").lower()
    return any(re.search(p, low) for p in _KY_PUBLICATION_PATTERNS)


def _in_anchor_window(pos: int, marker: Optional[int]) -> bool:
    if marker is None:
        return False
    return 0 <= abs(pos - marker) < _ANCHOR_WINDOW


class KYPublicNoticeScraper(BaseForeclosureScraper):
    """Scraper for KY public-notice pages maintained by kypublicnotice.com."""

    SOURCE_NAME = "ky_publicnotice"
    BASE_URL = KYFORECLOSURES_BASE_URL

    def __init__(
        self,
        delay: float = 1.5,
        use_proxy: bool = False,
        lookback_days: int = LOOKBACK_DAYS,
    ):
        # NOT a WebForms scraper: BaseForeclosureScraper's WebForms-oriented
        # fields (search_type / session handling) don't apply; only the
        # county/acreage filter path from its run() is reused.
        self.delay = delay
        self.delay_range = (delay, delay * 2)
        self.use_proxy = use_proxy
        self.lookback_days = lookback_days

    def _window_days(self) -> int:
        return getattr(self, "lookback_days", LOOKBACK_DAYS)

    def _get_target_counties(self) -> set[str]:
        return COUNTY_SET

    # ---- classification ---------------------------------------------------

    @classmethod
    def _classify(cls, text: str) -> Optional[str]:
        """Tax vs mortgage vs drop on the (normalized) full-page text.

        Newspaper pages bundle unrelated items, so classification is gated on
        the strongest sale marker: strong tax signals 700 chars around the
        marker make it a tax sale, otherwise a mortgage signal in the same
        window makes it a mortgage sale. Whole-page matches are only used as
        a fallback (the interleaved columns can put a tax phrase elsewhere on
        the page).
        """
        low = normalize_notice_text(text or "").lower()
        if not low or (_marker := _best_marker_pos(low)) is None:
            return None
        near = lambda pats: any(
            m is not None and abs(m.start() - _marker) < _ANCHOR_WINDOW
            for p in pats for m in [re.search(p, low)]) if pats else False
        if near(KY_TAX_PATTERNS):
            return "tax_foreclosure"
        if near(KY_MORTGAGE_PATTERNS):
            return "mortgage_foreclosure"
        if any(re.search(p, low) for p in KY_TAX_PATTERNS):
            return "tax_foreclosure"
        if any(re.search(p, low) for p in KY_MORTGAGE_PATTERNS):
            return "mortgage_foreclosure"
        return None

    @classmethod
    def _is_postable(cls, text: str) -> bool:
        """Drop publication/personal-property notices at card level."""
        if _is_publication_notice(text):
            return False
        low = normalize_notice_text(text or "").lower()
        return not any(re.search(p, low) for p in _KY_PERSONAL_PROPERTY)

    # ---- extraction --------------------------------------------------------

    @classmethod
    def _extract_address(cls, text: str, county: str = "") -> Optional[str]:
        """Property address near the strongest sale marker (or via `known as`)."""
        from .courthouses import is_courthouse_address
        if not text:
            return None
        body = normalize_notice_text(text)
        marker = _best_marker_pos(body.lower())
        # Two address shapes: "<n> <street> ... <suffix>" and "... Highway
        # 26" (the highway word carries the number, so a suffix-ending
        # alternation can never capture it).
        addr_body = (
            rf"\d{{1,5}}\s+[A-Za-z0-9.]+(?:\s+[A-Za-z0-9.]+){{0,4}}\s*"
            rf"{STREET_TAIL_BODY}"
            rf"|\d{{1,5}}\s+(?:OLD\s+)?{HWYN_TAIL_BODY}"
        )
        anchor_re = re.compile(
            rf"((?:commonly\s+|also\s+)?known\s+as\s*[:\-]?\s*"
            rf"|located\s+(?:at|on|in)\s*[:\-]?\s*"
            rf"|(?:property|address|street\s+address)\s*[:-]\s*)"
            rf"?({addr_body})",
            re.IGNORECASE,
        )
        for m in anchor_re.finditer(body):
            if not _in_anchor_window(m.start(), marker):
                continue
            addr = re.sub(r"\s+", " ", m.group(2).strip())
            if addr and not _LONG_RUN_RE.search(addr) \
                    and not is_courthouse_address(addr, county, "KY"):
                return addr[:120]
        # labeled "Property Address:" etc. (not necessarily near the marker
        # when the marker lands in the caption) as a looser fallback. The
        # capture is suffix-anchored: the newspaper page may jam the label
        # against unrelated column content on the same line ("Property
        # Address: 10376 Highway 26, Rockholds, Pursuant to KRS ..." — a
        # bare [^\n]{4,80} run once captured that whole line).
        labeled = re.search(
            rf"(?:property\s+address|address\s+of\s+(?:the\s+)?property"
            rf"|street\s+address)\s*[:-]\s*({addr_body})",
            body, re.IGNORECASE)
        if labeled:
            addr = re.sub(r"\s+", " ", labeled.group(1)).strip(" ,.;")
            if addr and not _LONG_RUN_RE.search(addr) \
                    and not is_courthouse_address(addr, county, "KY"):
                return addr[:120]
        return None

    @classmethod
    def _extract_sale_date(cls, text: str) -> Optional[str]:
        """Auction date: prefer sale-anchored mentions; ties go to the later
        date; deed-recital anchors are rejected (mirrors the TN extractor)."""
        if not text:
            return None
        flat = normalize_notice_text(text)
        best = None  # (anchored, iso)
        for m in _KY_DATE_RE.finditer(flat):
            mon = _KY_MONTHS.get(m.group(1).title())
            if not mon:
                continue
            try:
                iso = date(int(m.group(3)), mon, int(m.group(2))).isoformat()
            except (TypeError, ValueError):
                continue
            before = flat[max(0, m.start() - 70):m.start()].lower()
            anchored = any(a in before for a in _KY_SALE_ANCHORS)
            if not anchored and any(
                    a in flat[max(0, m.start() - 45):m.start()].lower()
                    for a in _KY_DEED_ANCHORS):
                continue
            if best is None or (anchored, iso) >= best:
                best = (anchored, iso)
        return best[1] if best else None

    @classmethod
    def _extract_parcel(cls, text: str, marker: Optional[int]) -> Optional[str]:
        """Assessor parcel only when it sits near a sale marker (KY page scan
        often shows unrelated parcels of *other* notices — never trusted)."""
        if not text:
            return None
        flat = normalize_notice_text(text)
        pat = re.compile(
            r"(?:parcel|tax\s*map|p\.v\.a\.|pva\s*(?:number|id|no))"
            r"[^:;]{0,30}[:#]?\s*([0-9][0-9A-Za-z\-\.\/,\s]{3,29})",
            re.IGNORECASE)
        for m in pat.finditer(flat):
            if _in_anchor_window(m.start(1), marker):
                pin = re.sub(r"[,\s]+", "", m.group(1))[:40]
                if pin:
                    return pin
        return None

    # ---- browser flow ------------------------------------------------------

    def scrape(self) -> List[PropertyData]:
        print(f"\n  KY PUBLIC NOTICE FORECLOSURES - {len(COUNTY_SET)} target counties")
        print(f"  Proxy: {'enabled' if self.use_proxy else 'disabled'}")
        print(f"  Lookback: last {self._window_days()} days")

        properties: List[PropertyData] = []
        proxy_cfg = {"server": config.PROXY_URL} if self.use_proxy and config.PROXY_URL else None

        from .base import camoufox_context
        with camoufox_context(proxy=proxy_cfg) as page:
            page.set_viewport_size({"width": 1920, "height": 1080})
            try:
                print("  [1/4] Connecting to kypublicnotice.com ...", end=" ",
                      flush=True)
                page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
                print("ok")

                print("  [2/4] County searches ...")
                cutoff = date.today() - timedelta(days=self._window_days())
                all_cards = []
                seen_files = set()
                for county in sorted(self._search_names()):
                    cards = self._walk_county_cards(page, county, cutoff)
                    fresh = [c for c in cards
                             if c["filename"] not in seen_files]
                    seen_files.update(c["filename"] for c in fresh)
                    all_cards.extend((c | {"county": county}) for c in fresh)
                    print(f"    {county}: {len(cards)} page(s) in window, "
                          f"{len(fresh)} new")

                print(f"  Found {len(all_cards)} notice pages in window")
                if not all_cards:
                    return []

                print("  [3/4] Downloading + classifying notice PDFs ...")
                kept = 0
                for i, card in enumerate(all_cards):
                    url = card["link"] or (
                        f"{KYFORECLOSURES_BASE_URL}/KYLegals/"
                        f"{card['pubdate'][:4]}/{card['filename']}")
                    try:
                        resp = page.request.get(url, timeout=60000)
                        data = resp.body() if resp.ok else None
                    except Exception as e:
                        logger.warning("KY PDF download failed for %s: %s",
                                       card["filename"], e)
                        data = None
                    text = extract_pdf_text(data) if data else None
                    if not text:
                        log_raw(self.SOURCE_NAME,
                                listing_id=card["filename"],
                                county=card.get("county"), state="KY",
                                decision="dropped_no_text",
                                reason="PDF text extraction failed",
                                raw_text=(card.get("excerpt") or ""),
                                url=url)
                        print(f"    [{i+1}/{len(all_cards)}] {card['filename']} "
                              f"(no text)")
                        continue

                    if not self._is_postable(text):
                        log_raw(self.SOURCE_NAME,
                                listing_id=card["filename"],
                                county=card.get("county"), state="KY",
                                decision="dropped_publication",
                                reason="court service publication / "
                                       "personal-property ad",
                                raw_text=text, url=url)
                        print(f"    [{i+1}/{len(all_cards)}] {card['filename']} "
                              f"(publication)")
                        continue

                    kind = self._classify(text)
                    if not kind:
                        log_raw(self.SOURCE_NAME,
                                listing_id=card["filename"],
                                county=card.get("county"), state="KY",
                                decision="dropped_non_foreclosure",
                                reason="no judicial-sale marker or no tax/mtg "
                                       "signal",
                                raw_text=text, url=url)
                        print(f"    [{i+1}/{len(all_cards)}] {card['filename']} "
                              f"(skipped)")
                        continue

                    marker = _best_marker_pos(normalize_notice_text(text).lower())
                    county = (card.get("county") or "").lower().strip()
                    prop = self._build_property(card, text, kind, county,
                                                marker, url)
                    if prop:
                        kept += 1
                        properties.append(prop)
                        log_raw(self.SOURCE_NAME,
                                listing_id=card["filename"],
                                county=card.get("county"), state="KY",
                                decision="kept_tax" if kind == "tax_foreclosure"
                                else "kept_mortgage",
                                reason="judicial-sale page classified",
                                raw_text=text, url=url)
                        print("-> kept")
                    else:
                        print("(no key)")
            finally:
                pass

        print(f"  [4/4] {len(properties)} qualifying rows")
        return properties

    def _search_names(self) -> List[str]:
        """County names as the KY site select labels them (Title case)."""
        return sorted(c.replace("_", " ").title() for c in COUNTY_SET)

    def _walk_county_cards(self, page, county: str, cutoff: date) -> List[dict]:
        """POST the county search and walk paginated result pages while
        cards stay inside the publication window (cards sort newest-first)."""
        page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=30000)
        ok = page.select_option('select[name="counties"]', county)
        if not ok:
            logger.error("KY county %r not in the search form", county)
            return []
        # Avoid the bootstrap datepicker participating in the submit; the JS
        # submit path equals the Search button (filters persist via POST).
        page.evaluate("() => { const f = document.querySelector('#searchform');"
                      " if (f) f.submit(); }")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=30000)
        except Exception:
            pass
        page.wait_for_timeout(3500)

        cards: List[dict] = []
        offset = 0
        for _ in range(40):  # 25/page → 40 pages far past any lookback
            page_cards = self._parse_cards(page, county)
            if not page_cards:
                break
            stale = [c for c in page_cards
                     if self._pubdate(c) is not None and self._pubdate(c) < cutoff]
            if stale:
                page_cards = [c for c in page_cards
                              if self._pubdate(c) is None or self._pubdate(c) >= cutoff]
                cards.extend(page_cards)
                break
            cards.extend(page_cards)
            if len(page_cards) < 25:
                break
            offset += 25
            page.evaluate(
                """(off) => {
                    const p = document.querySelector('input[name="page"]');
                    if (p) p.value = String(off);
                    const f = document.querySelector('#searchform');
                    if (f) f.submit();
                }""", offset)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=30000)
            except Exception:
                pass
            page.wait_for_timeout(3500)
        return cards

    @staticmethod
    def _pubdate(card: dict) -> Optional[date]:
        raw = card.get("pubdate") or ""
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None

    @staticmethod
    def _parse_cards(page, county_hint: str) -> List[dict]:
        """Result cards: hidden filename/pubdate/pubid/link fields + county.

        Cards are keyed by the hidden ``filename{id}`` inputs; the "County:"
        label appears once per card (in document order), so county labels are
        paired with filename inputs positionally. The search form itself
        prints a "County:" label (showing the selected county), so when more
        labels than cards are found the first one is the form and is dropped.
        """
        raw = page.evaluate("""() => {
            const inputs = Array.from(document.querySelectorAll(
                'input[id^="filename"]'));
            const bodyText = document.body.innerText || '';
            const labels = Array.from(bodyText.matchAll(
                /County:\\s*([A-Za-z]+)/g)).map(m => m[1]);
            // Drop the form's own label when it adds one extra.
            let counties = labels;
            if (labels.length > inputs.length) counties = labels.slice(1);
            return inputs.map((inp, i) => {
                const n = inp.id.replace(/^filename/, '');
                const ga = (name) => {
                    const el = document.querySelector('input[id="' + name + n + '"]');
                    return el ? el.value : null;
                };
                const excerptEl = document.querySelector('#synopsis' + n);
                return {
                    filename: inp.value,
                    n: n,
                    pubdate: ga('pubdate'),
                    pubid: ga('pubid'),
                    link: ga('link'),
                    county: counties[i] || null,
                    excerpt: excerptEl ? (excerptEl.innerText || '')
                        .replace(/\\s+/g, ' ').trim().slice(0, 500) : null,
                };
            });
        }""")
        # The card's county label is authoritative when present (a Tri-City
        # News card can name Harlan from a Bell-county search leak).
        for c in raw:
            if not c.get("county"):
                c["county"] = county_hint
        return raw

    def _build_property(self, card: dict, text: str, kind: str,
                        county: str, marker: Optional[int], url: str) -> Optional[PropertyData]:
        acres = self._extract_acreage(text)
        address = self._extract_address(text, county)
        parcel = self._extract_parcel(text, marker)
        auction_date = self._extract_sale_date(text)
        key = parcel or address or card["filename"].replace(".pdf", "")
        log = None
        src = self.SOURCE_NAME
        desc = text[:2000]
        prop: PropertyData = {
            "source": src,
            "source_listing_id": card["filename"].replace(".pdf", ""),
            "url": url,
            "address": address,
            "city": None,
            "county": county,
            "state": "KY",
            "zip_code": None,
            "latitude": None,
            "longitude": None,
            "price": None,
            "acres": acres,
            "description": desc,
            "property_type": kind,
            "image_url": None,
            "auction_date": auction_date,
            "parcel_number": parcel,
            "raw_source_text": text,
            "raw_paragraph": text,
        }
        if address:
            try:
                from .nc_gis_lookup import build_google_maps_url
                prop["google_maps_url"] = build_google_maps_url(
                    None, None, address, None, county, state="KY")
            except Exception:
                pass
        return prop


def scrape_with_enrichment(
    solve_captcha: bool = False,
    enrich: bool = False,
    lookback_days: int = LOOKBACK_DAYS,
) -> List[PropertyData]:
    """Run the KY public-notice scraper.

    Kentucky publishes no statewide parcel service our registry covers (GIS
    link policy: Maps links only), so ``enrich`` is accepted for API parity
    but performs no enrichment.
    """
    scraper = KYPublicNoticeScraper(lookback_days=lookback_days)
    return scraper.run()
