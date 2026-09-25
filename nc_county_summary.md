# NC County Property Record Card / Tax Lookup Systems

## 1. Buncombe County (Verified)
- **Website:** https://buncombecounty.org
- **Property Search:** https://community.spatialest.com/nc/buncombe/#/Property-Search/
- **Tax Online:** https://tax.buncombecounty.org/
- **GIS / Buncomap:** https://gis.buncombecounty.org/buncomap/
- **Tax Department:** https://buncombecounty.org/581/Tax-Department
- **Land Records:** https://registerofdeeds.buncombecounty.org/
- **Regional System:** SpatialEst
- **Tax-foreclosure listing (self-hosted):** YES — https://taxforeclosures.buncombenc.gov/ (live 2026-09-25, "Current Tax Foreclosure Listings") + Trumba iCal feed. No Turnstile. Covered by `buncombe_tax` scraper.
- **Legal notices:** Asheville Citizen-Times Gannett hub (https://www.citizen-times.com/public-notices, live, no Turnstile on listing/API). Covered by `newspaper_notices` Citizen-Times API sub-scraper.
- **Turnstile:** No (county + newspaper). Only ncnotices.com detail pages gate.
- **Scraper recommendation:** DONE — `buncombe_tax` + Citizen-Times API.

## 2. Transylvania County (Verified)
- **Website:** https://transylvaniacounty.org
- **Tax Administration:** https://transylvaniacounty.org/departments/tax-administration
- **GIS:** https://gis.transylvaniacounty.org/portal/apps/sites/#/transylvania-county-hub-site
- **Regional System:** ESRI/ArcGIS
- **Tax-foreclosure listing (self-hosted):** NO — Tax Administration page states tax sales are posted in the **Transylvania Times**; contact Tabitha Wiggins for email notification. No listing, no Turnstile (verified 2026-09-25).
- **Legal notices:** Transylvania Times AdPerfect marketplace (https://marketplace.transylvaniatimes.com/brevard-nc/public-notices/search, live, no Turnstile). NOT ncnotices.com.
- **Turnstile:** No (county + newspaper).
- **Scraper recommendation:** DONE — `newspaper_notices` Transylvania Times sub-scraper. No new county scraper needed.

## 3. Henderson County (Verified)
- **Website:** https://www.hendersoncountync.gov
- **GISWeb:** https://hendersoncountync.gov/gis
- **Property Lookup:** https://lrcpwa.ncptscloud.com/Henderson/
- **Tax Bill Lookup:** https://bcpwa.ncptscloud.com/hendersontax/
- **Tax Department:** https://hendersoncountync.gov/tax
- **Regional System:** NC PTS Cloud
- **Tax-foreclosure listing (self-hosted):** NO — Tax Dept page mentions foreclosure only as a collection method ("garnishments, attachments, and foreclosure"). No listing, no Turnstile (verified 2026-09-25).
- **Legal notices:** Hendersonville Times-News Gannett hub (https://www.blueridgenow.com/public-notices, live 35KB, mentions foreclosure + tax sale, no Turnstile). Gannett platform shared with Citizen-Times.
- **Turnstile:** No (county + newspaper).
- **Scraper:** DONE — `newspaper_notices` BlueRidgeNow Gannett-API sub-scraper (shared `_try_gannett_api` core, own state file; first backfill 2026-09-25: 54 mountain-county notices incl. a Madison parcel).

## 4. Watauga County (Verified)
- **Website:** https://www.wataugacounty.org
- **GIS:** https://gissvr.watgov.org/maps/
- **Tax Records Search:** http://tax.watgov.org/WataugaNC/Search/
- **Tax Bill Search:** https://www.wataugacounty.org/App_Pages/Dept/Tax/searchbills.aspx
- **Tax Department:** https://www.wataugacounty.org/App_Pages/Dept/Tax/home.aspx
- **Regional System:** Avineon
- **Tax-foreclosure listing (self-hosted):** NO — Tax home page has no foreclosure content (verified 2026-09-25).
- **Legal notices:** Watauga Democrat BLOX classifieds (https://www.wataugademocrat.com/classifieds/community/public_notices/, live, foreclosure notices present, no Turnstile). NOT ncnotices.com.
- **Turnstile:** No (county + newspaper).
- **Scraper recommendation:** DONE — `newspaper_notices` Watauga Democrat sub-scraper. No new county scraper needed.

## 5. Burke County (Verified 2026-09-25)
- **Website:** https://burkenc.org/ (CivicPlus — confirmed live 2026-09-25; `burkecounty.org` lands on the Chamber of Commerce site, `burkecountync.gov` / `co.burke.nc.us` NXDOMAIN)
- **Tax portal:** https://www.burkenctax.com/ (pay-taxes only, no foreclosure links)
- **Property / Tax Search:** via burkenctax.com (GIS/property subpage not separately verified)
- **Regional System:** (unverified)
- **Tax-foreclosure listing (self-hosted):** NO — no foreclosure content on burkenc.org or burkenctax.com. No Turnstile.
- **Legal notices:** Morganton News Herald (https://morganton.com/ads/, live, no foreclosure content on hub, no Turnstile). NOT ncnotices.com.
- **Turnstile:** No (county + newspaper).
- **Scraper recommendation:** None possible — newspaper-only county (Kania + ncnotices.com coverage).

## 6. Swain County (Verified 2026-09-24)
- **Website:** https://www.swaincountync.gov
- **Tag Office (foreclosure listings):** https://www.swaincountync.gov/tag-office/
  - Elementor toggle titled `FORECLOSURE LISTINGS` (`#elementor-tab-content-4992`); reads "None at this time." when no sale is pending (confirmed live 2026-09-24).
  - Monitored by the `swain_county` scraper (`scraper/swain_county.py`) — alerts once per distinct toggle text via content-hashed `source_listing_id`.
- **Tax Assessor (foreclosure info):** https://www.swaincountync.gov/tax-office/
  - `Notice of Foreclosure Sales` toggle links to a static info doc (`/download/tax-notice-of-foreclosure/`, WordPress Download Manager `?wpdmdl=9278`, file `HOJUN.pdf`) — a 2-page **scanned-image PDF with no extractable text** (48KB, uploaded 2024-08-02), describing the process, not live per-parcel listings. Captured as reference URL only.
- **Document Center:** https://www.swaincountync.gov/documents/ (Tax Office category holds assessor shapefiles/forms; no live sale list found)
- **Tax Collections contacts:** TaxOffice@swaincountync.gov, (828) 488-9273 ext 2224/2236; 101 Mitchell Street, Bryson City, NC 28713
- **Regional System:** NC OneMap statewide parcel service (Swain FIPS 173)
- **Audit note 2026-09-25:** Re-probe confirms NO listing under `/tax-office/` or `/foreclosures` (both 404 or info-only); the live listing is the `/tag-office/` Elementor toggle above. Delinquent-tax advertising runs in the Smoky Mountain Times (per Tax Office page). No Turnstile anywhere on county site.
- **Legal notices:** Smoky Mountain Times (https://www.smokymountaintimes.com/, no Turnstile). NOT ncnotices.com.
- **Scraper recommendation:** DONE — `swain_county` scraper monitors the tag-office toggle.

---

## County audit 2026-09-25 — method

All 20 NC mountain counties probed live via the scraper's camoufox (stealth Firefox) machinery (`scraper/tmp/nc_audit.py`, rounds 2–4 as `nc_audit2/3/4.py`), ~2 req/sec. For each county: (a) county homepage + tax-office candidate URLs checked for a self-hosted foreclosure **listing** (parcel-level sale list, not info/process pages); (b) the county's newspaper legal-notice hub checked for Turnstile/Cloudflare gating and for ncnotices.com linkage. Raw evidence: `scraper/tmp/nc_county_audit.json` (20 counties), `nc_audit2.json` (Swain/Burke/Graham/ncnotices/Clay), `nc_audit3.json` (nav-link extraction), `nc_audit4.json` (Clay/Swain content).

**Headline result:** 5 counties self-host a parcel-level foreclosure listing (Ashe, Buncombe, Haywood, McDowell, Macon) + Swain via its tag-office toggle (pre-existing section 6). **Zero** county or newspaper pages showed a Turnstile or Cloudflare challenge — the Turnstile gate lives only on ncnotices.com detail pages (homepage itself loads clean; detail gate uses the 2captcha fallback). No newspaper legal hub redirects to ncnotices.com as its host; at most Gannett hubs and three weeklies reference/link ncnotices.com content.

## 7. Alleghany County (Verified 2026-09-25)
- **Website:** https://alleghanycounty-nc.gov/ (`www.` redirects to apex)
- **Tax office:** https://alleghanycounty-nc.gov/county-taxes/ (live, pay-taxes + record-cards only)
- **Tax-foreclosure listing (self-hosted):** NO — no foreclosure content on tax page; `/county-taxes/tax-foreclosures/` is 404. No Turnstile.
- **Legal notices:** Alleghany News (https://www.alleghanynews.com/ — `/classifieds/` redirects to homepage; no legal hub found, no Turnstile). NOT ncnotices.com.
- **Turnstile:** No (county + newspaper).
- **Scraper recommendation:** Newspaper-only county. Covered by Kania + ncnotices.com. No county scraper possible (nothing to scrape).

## 8. Ashe County (Verified 2026-09-25, scraper live)
- **Website:** https://www.ashecountygov.com/
- **Tax-foreclosure listing (self-hosted):** PARTIAL — info page + linked parcel PDF, currently STALE ("Pending Tax Foreclosures as of 4/27/2018"). No Turnstile.
- **Legal notices:** Jefferson Post domain dead (NXDOMAIN 2026-09-25); fallback is ncnotices.com + Kania. No Turnstile on county side.
- **Turnstile:** No.
- **Scraper:** DONE — `scraper/ashe_county.py` (freshness-guarded: parses the PDF only when its "Updated" stamp is <365d old, else monitors silently; live run 2026-09-25 correctly returned 0 on the 2018 PDF).

## 9. Avery County (Verified 2026-09-25, newspaper sub-scraper live)
- **Website:** https://www.averycountync.gov/
- **Tax-foreclosure listing (self-hosted):** NO — `/tax/` and `/departments/tax` both 404; no listing. No Turnstile.
- **Legal notices:** Avery Journal-Times TownNews hub (https://www.averyjournal.com/classifieds/community/public_notices/, live, foreclosure-sale notices present e.g. `26SP000067-050`, no Turnstile). NOT ncnotices.com.
- **Turnstile:** No (county + newspaper).
- **Scraper:** DONE — `newspaper_notices` Avery Journal sub-scraper (`aj_` ids, TownNews pattern; live run 2026-09-25 clean, 0 qualifying today).

## 10. Clay County (Verified 2026-09-25)
- **Website:** https://www.claync.us/ (county gov) + https://tax.claync.us/ (tax office)
- **Tax-foreclosure listing (self-hosted):** NO — https://tax.claync.us/foreclosures is an info page only (no parcels; refers delinquents to **Kania Law Firm 828-252-8010**). No Turnstile.
- **Legal notices:** Clay County Progress (https://claycountyprogress.com/, live, page HTML references ncnotices.com, no Turnstile). Content-shared with ncnotices, but self-hosted.
- **Turnstile:** No.
- **Scraper recommendation:** No county scraper (info page, no parcels). Clay parcels arrive via Kania Law (its foreclosure counsel) + ncnotices.com.

## 11. Graham County (Verified 2026-09-25)
- **Website:** https://grahamcounty.org/ (apex works; `www.` and `grahamcountync.gov` NXDOMAIN)
- **Tax office:** https://grahamcounty.org/tax_collector.html (BT taxpayer portal links, no foreclosure list)
- **Tax-foreclosure listing (self-hosted):** NO. No Turnstile.
- **Legal notices:** Graham Star (https://grahamstar.com/, live, page HTML references ncnotices.com, no Turnstile). Content-shared with ncnotices, but self-hosted.
- **Turnstile:** No.
- **Scraper recommendation:** Newspaper-only county. No county scraper possible.

## 12. Haywood County (Verified 2026-09-25, scraper live)
- **Website:** https://www.haywoodcountync.gov/
- **Tax-foreclosure listing (self-hosted):** INFO + bid module — https://www.haywoodcountync.gov/337/Tax-Foreclosures is process info; live sales post to the CivicPlus bid module (`Bids.aspx?CatID=17`, currently "no open bid postings"). No Turnstile.
- **Legal notices:** The Mountaineer TownNews hub (https://www.themountaineer.com/classifieds/, live, no Turnstile). NOT ncnotices.com.
- **Turnstile:** No.
- **Scraper:** DONE — `scraper/haywood_county.py` (bid-module monitor; live run 2026-09-25 correctly returned 0).

## 13. Jackson County (Verified 2026-09-25)
- **Website:** https://www.jacksonnc.org/
- **Tax-foreclosure listing (self-hosted):** NO — `/tax-collector` and `/departments/tax` both 404 (CivicEngage). No Turnstile.
- **Legal notices:** Sylva Herald BLOX legals (https://www.thesylvaherald.com/classifieds/community/announcements/legal/, live, no Turnstile). NOT ncnotices.com.
- **Turnstile:** No.
- **Scraper recommendation:** DONE — `newspaper_notices` Sylva Herald sub-scraper. No county scraper possible.

## 14. Macon County (Verified 2026-09-25, scraper live)
- **Website:** https://www.maconnc.org/ (redirects to GIS host landing)
- **Tax-foreclosure listing (self-hosted):** NO — https://maconnc.org/maconcountyforeclosures.html is FAQ-only (no parcel rows; sales advertise in the Franklin Press). No Turnstile.
- **Legal notices:** Franklin Press (https://thefranklinpress.com/news-category/classifieds/231/, live, page HTML references ncnotices.com, no Turnstile). Content-shared with ncnotices, but self-hosted.
- **Turnstile:** No.
- **Scraper:** DONE — `scraper/macon_county.py` (FAQ monitor, emits rows only if parcel content appears; live run 2026-09-25 correctly returns 0 after a ZIP-code false-positive fix).

## 15. Madison County (Verified 2026-09-25)
- **Website:** https://www.madisoncountync.gov/
- **Tax-foreclosure listing (self-hosted):** NO — `/tax` is a stub page, `/departments/tax-collector` 404. No Turnstile.
- **Legal notices:** Madison News-Record domain dead (NXDOMAIN 2026-09-25). Falls back to ncnotices.com + Kania.
- **Turnstile:** No (county side clean).
- **Scraper recommendation:** Newspaper-only county with dead paper domain. No county scraper possible.

## 16. McDowell County (Verified 2026-09-25, scraper live)
- **Website:** https://mcdowellnc.gov/ (`mcdowellgov.com` redirects here)
- **Tax-foreclosure listing (self-hosted):** YES — https://mcdowellnc.gov/departments/tax-collections/tax-foreclosures/upcoming-tax-foreclosure-sales with upcoming/upset/pending tables (live upset row 2026-09-25: parcels 1739-00-31-2535 / 1739-00-21-7533, $15,750, file 26CV000538-580). No Turnstile.
- **Legal notices:** McDowell News (https://mcdowellnews.com/ads/, live hub, no foreclosure content on hub, no Turnstile). NOT ncnotices.com.
- **Turnstile:** No.
- **Scraper:** DONE — `scraper/mcdowell_county.py` (parses all three tables, splits bundled parcels; live run 2026-09-25 yielded 2 parcel rows, 1 new + 1 cross-source dup, GIS-enriched).

## 17. Mitchell County (Verified 2026-09-25)
- **Website:** https://www.mitchellcountync.gov/
- **Tax-foreclosure listing (self-hosted):** NO — Tax Assessor and Tax Collector pages carry no foreclosure content. No Turnstile.
- **Legal notices:** Mitchell News legals moved to https://www.newstopicnews.com/mitchell/classified/legals/ — entry URL updated in `newspaper_notices`, but the target renders thin (1101 chars, login-wall-like) on 2026-09-25; sub-scraper runs without error but yields nothing. Needs a follow-up render/patron check.
- **Turnstile:** No.
- **Scraper recommendation:** No county-office scraper possible. Mitchell newspaper coverage degraded until the newstopic render is resolved.

## 18. Polk County (Verified 2026-09-25)
- **Website:** https://www.polknc.gov/
- **Tax-foreclosure listing (self-hosted):** NO — `/tax` and `/departments/tax-collector` both 404. No Turnstile.
- **Legal notices:** Tryon Daily Bulletin `/classifieds/` is 404 (no legal hub found, no Turnstile). NOT ncnotices.com.
- **Turnstile:** No.
- **Scraper recommendation:** Newspaper-only county with no reachable legal hub. Covered by Kania + ncnotices.com. No county scraper possible.

## 19. Yancey County (Verified 2026-09-25)
- **Website:** https://www.yanceycountync.gov/
- **Tax-foreclosure listing (self-hosted):** NO — Tax Department page (8.5KB) hosts the **2025 TAX FORECLOSURE PROCESS (PDF)** + tax scroll/rate docs, but no parcel-level sale listing. No Turnstile.
- **Legal notices:** Yancey Common Times Journal domain dead (yanceycommon.com NXDOMAIN 2026-09-25). Falls back to ncnotices.com + Kania.
- **Turnstile:** No (county side clean).
- **Scraper recommendation:** No county scraper (process docs only, no parcels). Optionally ingest the yearly tax-scroll PDF as a delinquent-list reference.

## 20. Cherokee County (Verified 2026-09-25)
- **Website:** https://www.cherokeecounty-nc.gov/ (CivicEngage)
- **Tax-foreclosure listing (self-hosted):** NO — `/tax/` and `/departments/tax-collector` both 404. No Turnstile.
- **Legal notices:** Cherokee Scout (https://www.thecherokeescout.com/classifieds/, empty body render, no Turnstile). NOT ncnotices.com.
- **Turnstile:** No.
- **Scraper recommendation:** Newspaper-only county. Covered by Kania + ncnotices.com. No county scraper possible.

---

## Summary table (2026-09-25 camoufox audit)

| # | County | Self-hosted listing? | Legal-notice host | ncnotices redirect? | Turnstile anywhere? | Scraper action |
|---|---|---|---|---|---|---|
| 1 | Buncombe | YES (Trumba) | Citizen-Times (Gannett API) | No (own API) | Detail-pages only (ncnotices) | DONE (`buncombe_tax` + CT API) |
| 2 | Transylvania | NO (says: Transylvania Times) | Transylvania Times (AdPerfect) | No | No | DONE (TT sub-scraper) |
| 3 | Henderson | NO (mention only) | BlueRidgeNow (Gannett hub) | No (own hub) | No | CANDIDATE (Gannett-API sub-scraper) |
| 4 | Watauga | NO | Watauga Democrat (BLOX) | No | No | DONE (WD sub-scraper) |
| 5 | Burke | UNRESOLVED (domain) | Morganton News Herald | No | No | HOLD (confirm gov domain) |
| 6 | Swain | YES (tag-office toggle) | Smoky Mountain Times | No | No | DONE (`swain_county`) |
| 7 | Alleghany | NO | Alleghany News (no legal hub) | No | No | None possible |
| 8 | Ashe | YES (pending-foreclosures) | Paper domain dead → ncnotices/Kania | n/a | No | PRIORITY 1 (new scraper) |
| 9 | Avery | NO | Avery Journal (TownNews) | No | No | CANDIDATE (TownNews sub-scraper) |
| 10 | Clay | NO (info page, Kania referral) | Clay County Progress | Links only | No | None (via Kania) |
| 11 | Graham | NO | Graham Star | Links only | No | None possible |
| 12 | Haywood | YES (`/337/Tax-Foreclosures`) | The Mountaineer (TownNews) | No | No | PRIORITY 2 (new scraper) |
| 13 | Jackson | NO | Sylva Herald (BLOX) | No | No | DONE (SH sub-scraper) |
| 14 | Macon | YES (`maconcountyforeclosures.html`) | Franklin Press | Links only | No | PRIORITY 3 (new scraper) |
| 15 | Madison | NO | Paper domain dead → ncnotices/Kania | n/a | No | None possible |
| 16 | McDowell | YES (`.../tax-foreclosures`) | McDowell News | No | No | PRIORITY 4 (new scraper) |
| 17 | Mitchell | NO | Mitchell News (URL moved, 404) | No | No | HOLD (fix MN sub-scraper URL) |
| 18 | Polk | NO | Tryon Bulletin (hub 404) | No | No | None possible |
| 19 | Yancey | NO (process PDF only) | Paper domain dead → ncnotices/Kania | n/a | No | None (scroll PDF optional) |
| 20 | Cherokee | NO | Cherokee Scout | No | No | None possible |

**New county-specific scrapers to add (all Turnstile-free static pages):** `ashe_county` → `haywood_county` → `macon_county` → `mcdowell_county`, all following the `swain_county` static-fetch pattern with NC OneMap enrichment. **Newspaper extensions:** Henderson (BlueRidgeNow Gannett API), Avery (TownNews). **Fixes:** Mitchell News entry URL; Burke gov-domain confirmation.

## Build log 2026-09-25 — all implemented and live-verified

- `scraper/county_static.py` (new, shared): camoufox fetch/table/link helpers, parcel/case/money/date regexes, tax-row builder with NC GIS links, monitor logging.
- `scraper/mcdowell_county.py` (new): 3-table parser, bundled-parcel split — live 2 rows (1 new + 1 dup), GIS-enriched.
- `scraper/ashe_county.py` (new): PDF-link finder + pdfplumber parser with 365-day freshness guard — live 0 rows (2018 PDF correctly ignored).
- `scraper/haywood_county.py` (new): CivicPlus bid-module monitor — live 0 rows (no open postings).
- `scraper/macon_county.py` (new): FAQ table monitor with ZIP-safe parcel matching — live 0 rows.
- `scraper/newspaper_notices.py`: Gannett API generalized (`_try_gannett_api`, per-site state files) + BlueRidgeNow sub-scraper (54 notices backfill) + Avery Journal sub-scraper + Mitchell URL fix. Full run: 59 found, 11 new, 9 GIS-enriched (incl. Madison parcel).
- `scraper/run.py`: all four county scrapers registered (18 total). `tests/test_county_static.py`: 11 unit tests.
- Caution: `PARCEL_RE` must never match bare digit runs (ZIP false positive `macon_28734` caught and fixed 2026-09-25; junk row #954 in DB awaiting archive approval).
