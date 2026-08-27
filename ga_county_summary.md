# GA County Property Record Card / Tax Lookup Systems

> Scope: the 7 N GA mountain counties tracked by the `ga_publicnotice` scraper
> (`fannin, gilmer, lumpkin, rabun, towns, union, white`). GA has **no
> statewide parcel/GIS hub** (data-hub.gio.georgia.gov returns 0 sources), so
> there is no automated GIS *enrichment* for GA — the `gis_url` field for GA
> properties is a clickable **qPublic (Schneider Corp) parcel link** only.
>
> GA tax sales are statutorily held on the **first Tuesday** of the month, and
> GA has **no upset-bid period**.

## General notes

- **Vendor for most counties:** Schneider Corp **qPublic** Beacon —
  `https://qpublic.schneidercorp.com/`. A per-county `AppID`/`LayerID` builds a
  direct parcel-detail deep link:
  `https://qpublic.schneidercorp.com/Application.aspx?AppID=<app>&LayerID=<layer>&PageTypeID=<pt>&PageID=<pid>&KeyValue=<parcel>`
- **`Q` token caveat:** a browser-clicked qPublic link also carries a
  per-parcel `&Q=<id>` session token. A mismatched `Q` returns **HTTP 403**, so
  we intentionally **omit `Q`** and use the `KeyValue`-only link (returns 200).
- **Fallback for unknown counties:** `App=<County>CountyGA&Layer=Parcels&PageType=Search`
  (generic parcel search landing — no direct parcel deep link).
- Registry lives in `scraper/gis_urls.py` → `GA_QPUBLIC_APPS`.

---

## 1. Gilmer County (Verified — qPublic app)
- **qPublic AppID:** 672
- **LayerID:** 11357
- **Parcel page:** PageTypeID=4, PageID=4736
- **KeyValue format:** parcel as-is, **no internal space**
  (e.g. `KeyValue=0123456`)
- **Direct link example:**
  `https://qpublic.schneidercorp.com/Application.aspx?AppID=672&LayerID=11357&PageTypeID=4&PageID=4736&KeyValue=<parcel>`
- **Regional System:** Schneider Corp qPublic

## 2. Lumpkin County (Verified — qPublic app, 2026-08-25)
- **qPublic AppID:** 991
- **LayerID:** 20168
- **Parcel page:** PageTypeID=1, PageID=8779
- **KeyValue format:** stored as `"035 278"` (district, single space, parcel)
  but qPublic wants **FOUR spaces** → `"035    278"`
  (verified canonical: `...&KeyValue=047++++363`)
- **Direct link example:**
  `https://qpublic.schneidercorp.com/Application.aspx?AppID=991&LayerID=20168&PageTypeID=1&PageID=8779&KeyValue=<parcel-4-spaces>`
- **Regional System:** Schneider Corp qPublic

## 3. Towns County (Verified — qPublic app, TWO pages)
- **qPublic AppID:** 846
- **LayerID:** 15440
- **Numeric / alphanumeric parcels** (e.g. `0009A041`):
  - PageTypeID=1, PageID=7007, **no internal space**
- **`YH`-prefixed parcels** (e.g. `YH02078`):
  - PageTypeID=4, PageID=7010, **internal space** → `"YH02 078"`
- **Direct link examples:**
  - `...?AppID=846&LayerID=15440&PageTypeID=1&PageID=7007&KeyValue=0009A041`
  - `...?AppID=846&LayerID=15440&PageTypeID=4&PageID=7010&KeyValue=YH02+078`
- **Regional System:** Schneider Corp qPublic

## 4. White County (Verified — qPublic app, 2026-08-25)
- **qPublic AppID:** 982
- **LayerID:** 19945
- **Parcel page:** PageTypeID=1, PageID=8773
- **KeyValue format:** parcel as-is, **single internal space**
  (e.g. `"018D 019"` → `KeyValue=018D%20019`)
- **Direct link example:**
  `https://qpublic.schneidercorp.com/Application.aspx?AppID=982&LayerID=19945&PageTypeID=1&PageID=8773&KeyValue=<parcel-1-space>`
- **Regional System:** Schneider Corp qPublic

## 5. Fannin County (NOT yet verified)
- **qPublic app:** unknown — falls back to generic search landing
  `https://qpublic.schneidercorp.com/Application.aspx?App=FanninCountyGA&Layer=Parcels&PageType=Search`
- **To do:** discover AppID/LayerID/PageID and KeyValue spacing.

## 6. Rabun County (NOT yet verified)
- **qPublic app:** unknown — falls back to generic search landing
  `https://qpublic.schneidercorp.com/Application.aspx?App=RabunCountyGA&Layer=Parcels&PageType=Search`
- **To do:** discover AppID/LayerID/PageID and KeyValue spacing.

## 7. Union County (NOT yet verified)
- **qPublic app:** unknown — falls back to generic search landing
  `https://qpublic.schneidercorp.com/Application.aspx?App=UnionCountyGA&Layer=Parcels&PageType=Search`
- **To do:** discover AppID/LayerID/PageID and KeyValue spacing.

---

## Verification log
- **2026-08-25** — Lumpkin (`AppID=991`) and White (`AppID=982`) qPublic app
  IDs / pages verified directly from working deep links; `gis_urls.py`
  `GA_QPUBLIC_APPS` updated and all GA records re-backed via
  `scraper/backfill_ga_gis.py`.
- Gilmer and Towns app IDs carried over from earlier discovery; direct-link
  format confirmed by `get_ga_gis_url` output.
- Fannin / Rabun / Union still on the generic qPublic search fallback.
