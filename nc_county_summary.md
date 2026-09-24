# NC County Property Record Card / Tax Lookup Systems

## 1. Buncombe County (Verified)
- **Website:** https://buncombecounty.org
- **Property Search:** https://community.spatialest.com/nc/buncombe/#/Property-Search/
- **Tax Online:** https://tax.buncombecounty.org/
- **GIS / Buncomap:** https://gis.buncombecounty.org/buncomap/
- **Tax Department:** https://buncombecounty.org/581/Tax-Department
- **Land Records:** https://registerofdeeds.buncombecounty.org/
- **Regional System:** SpatialEst

## 2. Transylvania County (Verified)
- **Website:** https://transylvaniacounty.org
- **Tax Administration:** https://transylvaniacounty.org/departments/tax-administration
- **GIS:** https://gis.transylvaniacounty.org/portal/apps/sites/#/transylvania-county-hub-site
- **Regional System:** ESRI/ArcGIS

## 3. Henderson County (Verified)
- **Website:** https://www.hendersoncountync.gov
- **GISWeb:** https://hendersoncountync.gov/gis
- **Property Lookup:** https://lrcpwa.ncptscloud.com/Henderson/
- **Tax Bill Lookup:** https://bcpwa.ncptscloud.com/hendersontax/
- **Tax Department:** https://hendersoncountync.gov/tax
- **Regional System:** NC PTS Cloud

## 4. Watauga County (Verified)
- **Website:** https://www.wataugacounty.org
- **GIS:** https://gissvr.watgov.org/maps/
- **Tax Records Search:** http://tax.watgov.org/WataugaNC/Search/
- **Tax Bill Search:** https://www.wataugacounty.org/App_Pages/Dept/Tax/searchbills.aspx
- **Tax Department:** https://www.wataugacounty.org/App_Pages/Dept/Tax/home.aspx
- **Regional System:** Avineon

## 5. Burke County (Verified)
- **Website:** https://www.burkecounty.org
- **Property / Tax Search:** https://www.burkecounty.org/ (GIS/property subpage not separately verified)
- **Note:** The previously listed `www.burkecountync.org` domain is dead (redirects to an unrelated site); the correct official domain is `burkecounty.org`.
- **Regional System:** (unverified)

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
