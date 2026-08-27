# TN County Property / Tax Lookup Systems & Tax Foreclosure Handling

Target set: `TN_FORECLOSURE_COUNTIES` (38 east-TN mountain counties, config.py:207).

## Key finding
Most TN counties conduct delinquent-property-tax sales **once a year** (or per court decree), usually run by the **Chancery Court Clerk & Master** (or the **County Trustee** in a few). These sales are published on the county Clerk & Master / Trustee site (often a PDF "Properties for Sale" list) — **not** on tnpublicnotice.com, and the `tn_publicnotice` scraper only reads page 1 of the statewide "Tax Sales" feed, so it misses most annual county-direct sales. Blount County (Clerk & Master, annual June sale, PDF list) is the canonical example.

## 1. Anderson County
- **Website:** https://andersoncountytn.gov/
- **Property Search:** https://acassessor.maps.arcgis.com/apps/webappviewer/index.html?id=ceca75ab630048669e3b90abb301b09a
- **Tax Lookup:** https://secure.tennesseetrustee.org/?entity=anderson&state=TN
- **GIS:** https://acassessor.maps.arcgis.com/apps/webappviewer/index.html?id=ceca75ab630048669e3b90abb301b09a
- **Tax Sale / Delinquent:** https://andersoncountyclerkandmaster.com/delinquent-taxes/
- **Regional System:** Esri ArcGIS (assessor); Tennessee Trustee (BIS) for tax; GovEase for the sale
- **Tax Foreclosure:** Chancery Court Clerk & Master — periodic online auction (via GovEase) as delinquent-tax suits reach judgment

## 2. Bledsoe County
- **Website:** https://bledsoetn.com/
- **Property Search:** https://assessment.cot.tn.gov/RE_Assessment/ (statewide; no local portal found)
- **Tax Lookup:** https://secure.tennesseetrustee.org/?entity=bledsoe&state=TN
- **GIS:** https://tnmap.tn.gov/assessment/ (statewide; no local portal found)
- **Tax Sale / Delinquent:** unknown
- **Regional System:** Tennessee Trustee (BIS) for tax; TNMap (state Comptroller) for property/GIS
- **Tax Foreclosure:** County Trustee (possibly Chancery Court Clerk & Master) — cadence unknown

## 3. Blount County
- **Website:** https://www.blounttn.gov/
- **Property Search:** https://assessment.cot.tn.gov/TPAD/Search
- **Tax Lookup:** https://blount-tn.mygovonline.com/mod.php?mod=propertytax&mode=public_lookup
- **GIS:** http://www.blountgis.org/
- **Tax Sale / Delinquent:** https://www.blounttn.gov/2029/Delinquent-Property-Tax-Sale
- **Regional System:** mygovonline.com (GovEase/eGov) for tax; BlountGIS (ArcGIS) for maps
- **Tax Foreclosure:** Blount County Clerk & Master — one sale per year (annual), in-person auction (2026 sale June 4)

## 4. Campbell County
- **Website:** https://campbellcountytn.gov/
- **Property Search:** https://assessment.cot.tn.gov/RE_Assessment/
- **Tax Lookup:** https://citisenportal.com/Search/Campbell%20County%20Trustee
- **GIS:** https://tnmap.tn.gov/assessment/ (statewide; local unknown)
- **Tax Sale / Delinquent:** Campbell County Clerk & Master (office page referenced from trustee; dedicated sale URL unknown)
- **Regional System:** CitiSen Portal (Tyler Technologies) for tax; TNMap (state) for property/GIS
- **Tax Foreclosure:** Clerk & Master of Campbell County — annual auction (each spring, e.g. May 8 2026)

## 5. Carter County
- **Website:** https://www.cartercountytn.gov/
- **Property Search:** https://assessment.cot.tn.gov/RE_Assessment/SelectCounty.aspx?map=true&SelectCounty=010
- **Tax Lookup:** https://www.citisenportal.com
- **GIS:** https://tnmap.tn.gov/assessment/
- **Tax Sale / Delinquent:** https://www.cartercountytn.gov/government/elected_officials/clerk___master.php
- **Regional System:** CitiSen Portal (Tyler Technologies) for tax; TNMap (state) for property/GIS
- **Tax Foreclosure:** Clerk & Master (Chancery Court) — as delinquent tax suits reach judgment (periodic/advertised sale)

## 6. Claiborne County
- **Website:** https://claibornecountytn.gov/
- **Property Search:** https://assessment.cot.tn.gov/RE_Assessment/ (statewide; local unknown)
- **Tax Lookup:** https://claiborne-tn.renewgov.com
- **GIS:** https://tnmap.tn.gov/assessment/ (statewide; no local portal found)
- **Tax Sale / Delinquent:** unknown
- **Regional System:** renewgov.com / mygovonline (GovEase) for tax; TNMap (state) for property/GIS
- **Tax Foreclosure:** County Trustee — cadence unknown; yearly sale set by delinquent-tax attorney, not more than once a year

## 7. Cocke County
- **Website:** https://www.cockecountytn.gov
- **Property Search:** https://assessment.cot.tn.gov/tpad
- **Tax Lookup:** https://cocke.tennesseetrustee.org
- **GIS:** https://tnmap.tn.gov/assessment
- **Tax Sale / Delinquent:** https://www.cockecircuit.com (Circuit Court delinquent-tax sale lists/PDFs)
- **Regional System:** Tennessee Trustee (BIS) tax platform; state TNMap/ArcGIS parcel viewer
- **Tax Foreclosure:** Cocke County Circuit Court (via delinquent-tax attorney) — annual sale

## 8. Coffee County
- **Website:** https://www.coffeecountytn.gov
- **Property Search:** https://assessment.cot.tn.gov/tpad
- **Tax Lookup:** https://coffee.tennesseetrustee.org
- **GIS:** https://tnmap.tn.gov/assessment
- **Tax Sale / Delinquent:** https://www.coffeecountytn.gov/163/Delinquent-Property-Taxes (online via GovEase)
- **Regional System:** Tennessee Trustee (BIS) tax platform; GovEase auction
- **Tax Foreclosure:** Chancery Court Clerk & Master — annual online sale

## 9. Cumberland County
- **Website:** https://cumberlandcountytn.gov
- **Property Search:** https://assessment.cot.tn.gov/tpad
- **Tax Lookup:** https://secure.tennesseetrustee.org (entity=cumberland)
- **GIS:** https://cumberlandgis.maps.arcgis.com/apps/webappviewer/index.html?id=a6ea68995c2349e9a177366288589be7
- **Tax Sale / Delinquent:** https://cumberlandcountytn.gov/documents/ (DELINQUENT TAX PROPERTIES PDF, bid sheets)
- **Regional System:** County ArcGIS GIS viewer; Tennessee Trustee (BIS) tax platform
- **Tax Foreclosure:** Chancery Court Clerk & Master — annual sale

## 10. Fentress County
- **Website:** https://fentresscountytn.gov
- **Property Search:** https://assessment.cot.tn.gov/tpad
- **Tax Lookup:** https://fentress.tennesseetrustee.org
- **GIS:** https://tnmap.tn.gov/assessment
- **Tax Sale / Delinquent:** Fentress County Chancery Court — Clerk & Master (Linda Smith); auctions hosted on HiBid
- **Regional System:** Tennessee Trustee (BIS) tax platform; HiBid auction
- **Tax Foreclosure:** Chancery Court Clerk & Master (Linda Smith) — annual/online auction

## 11. Grainger County
- **Website:** https://www.graingercountytn.com
- **Property Search:** https://assessment.cot.tn.gov/tpad
- **Tax Lookup:** https://www.graingercountytrustee.com (also CitiSen portal)
- **GIS:** https://tnmap.tn.gov/assessment
- **Tax Sale / Delinquent:** https://www.graingercountytn.com/county-officials/clerk-master/
- **Regional System:** Tennessee Trustee (BIS) tax platform; CitiSen payment portal
- **Tax Foreclosure:** Chancery Court Clerk & Master (Vickie B. Greenlee) — annual sale

## 12. Greene County
- **Website:** https://greenecountytngov.com
- **Property Search:** https://assessment.cot.tn.gov/tpad
- **Tax Lookup:** https://greene.tennesseetrustee.org
- **GIS:** https://tnmap.tn.gov/assessment
- **Tax Sale / Delinquent:** http://greeneville.com/courtsale/ (Clerk & Master delinquent-tax sale site; online via GovEase)
- **Regional System:** Tennessee Trustee (BIS) tax platform; GovEase auction
- **Tax Foreclosure:** Chancery Court Clerk & Master (Bland Justis) — annual sale (online/in-person)

## 13. Grundy County
- **Website:** https://www.grundycountytn.net
- **Property Search:** https://assessment.cot.tn.gov/tpad
- **Tax Lookup:** https://grundycountytrustee.com
- **GIS:** https://maps.grundyco.org/webappbuilder/propertyviewer/
- **Tax Sale / Delinquent:** https://www.grundycountytn.net/officials/ (Chancery Court Clerk & Master delinquent-tax suits/auctions)
- **Regional System:** County ArcGIS GIS viewer; Tennessee Trustee (Catalis) tax platform
- **Tax Foreclosure:** Chancery Court Clerk & Master — annual sale

## 14. Hamblen County
- **Website:** https://www.hamblencountytn.gov/
- **Property Search:** https://hamblen-tn.mygovonline.com/mod.php?mod=propertytax&mode=public_lookup
- **Tax Lookup:** https://www.hamblencountytn.gov/taxes/ (pay via TNPayments.com/Hamblen)
- **GIS:** https://mh-gis.maps.arcgis.com/ (Morristown-Hamblen GIS Partnership)
- **Tax Sale / Delinquent:** https://www.hamblencountychancery.org/#/delinquent (Clerk & Master delinquent tax)
- **Regional System:** ArcGIS/Esri (MHGIS) for GIS; myGovOnline (eGov) for tax records
- **Tax Foreclosure:** Chancery Court Clerk & Master — annual (online via GovEase)

## 15. Hamilton County
- **Website:** https://hamiltontn.gov/
- **Property Search:** https://assessor.hamiltontn.gov/
- **Tax Lookup:** https://tpti.hamiltontn.gov/ (Trustee Property Tax Inquiry)
- **GIS:** https://gismaps.hamiltontn.gov/hcgis (HCGIS / Geocortex Esri)
- **Tax Sale / Delinquent:** https://cmpti.hamiltontn.gov/ (Clerk & Master Delinquent Property Tax Inquiry); sale listings via CivicSource
- **Regional System:** Geocortex/Esri ArcGIS (GIS); custom tpti/cmpti portals; CivicSource for tax-sale listings
- **Tax Foreclosure:** Chancery Court Clerk & Master — annual (e.g. June 4 2026; online via CivicSource)

## 16. Hancock County
- **Website:** https://www.hancockcountytn.com/
- **Property Search:** https://www.hancockcountytn.com/Assessor-or-Property.php (state viewer: https://tnmap.tn.gov/assessment/)
- **Tax Lookup:** https://www.hancockcountytn.com/local_public_service_directory/trustee.php (pay via tnpayments.com)
- **GIS:** https://tnmap.tn.gov/assessment/ (TN Property Viewer — no standalone county GIS found)
- **Tax Sale / Delinquent:** https://www.hancockcountytn.com/local_public_service_directory/clerk_and_master.php (Clerk & Master; "NO TAX SALE FOR 2025")
- **Regional System:** TNMap (TN State) for GIS; courtfeepay.com / tnpayments.com for payments
- **Tax Foreclosure:** Chancery Court Clerk & Master — annual as needed (none held 2025)

## 17. Hawkins County
- **Website:** https://www.hawkinscountytn.gov/
- **Property Search:** https://hawkins-tn.mygovonline.com/?mod=propertytax&mode=public_lookup
- **Tax Lookup:** https://hawkins.renewgov.com (Trustee / RenewGov)
- **GIS:** https://tnmap.tn.gov/assessment/ (TN Property Viewer — no standalone county GIS found)
- **Tax Sale / Delinquent:** https://www.hawkinscountytn.gov/chancery_court_clerk_master.html (Clerk & Master); sales via https://www.govease.com/auctions
- **Regional System:** myGovOnline / RenewGov (eGov) for tax; GovEase for tax sale; TNMap for GIS
- **Tax Foreclosure:** Chancery Court Clerk & Master — annual (online via GovEase; e.g. Aug 26 2025)

## 18. Jefferson County
- **Website:** https://jeffersoncountytn.gov/
- **Property Search:** https://jeffersoncountytn.gov/property-assessor/ (state viewer: https://tnmap.tn.gov/assessment/)
- **Tax Lookup:** https://jeffersoncountytn.gov/county-trustee/ (Trustee; pay via tennesseetrustee.org)
- **GIS:** https://tnmap.tn.gov/assessment/ (TN Property Viewer — no standalone county GIS found)
- **Tax Sale / Delinquent:** https://jeffersoncountytn.gov/chancery-court/ (Clerk & Master; public auction at courthouse, ≤ once/year)
- **Regional System:** TNMap (TN State) for GIS/property; in-person/Trustee tax collection; no third-party sale platform
- **Tax Foreclosure:** Chancery Court Clerk & Master — annual (public in-person auction, not more than once/year)

## 19. Johnson County
- **Website:** https://www.johnsoncountytn.gov/
- **Property Search:** https://www.johnsoncountytn.gov/administrative-offices (Assessor; tax records via https://johnson.tennesseetrustee.org/)
- **Tax Lookup:** https://johnson.tennesseetrustee.org/ (Tennessee Trustee / eGov); also https://www.johnsoncountytaxoffice.org/search
- **GIS:** https://tnmap.tn.gov/assessment/ (TN Property Viewer — no standalone county GIS found)
- **Tax Sale / Delinquent:** https://johnsoncountytnchancerycourt.info/ (Clerk & Master; auctions listed at /auctions)
- **Regional System:** Tennessee Trustee (eGov) for tax; TNMap for GIS; GovEase likely for sale
- **Tax Foreclosure:** Chancery Court Clerk & Master — annual (online via GovEase)

## 20. Knox County
- **Website:** https://www.knoxcounty.org/
- **Property Search:** https://propertyinfo.knoxcountytn.gov/ (Property Records & Taxation)
- **Tax Lookup:** https://trustee.knoxcounty.org/ (Trustee)
- **GIS:** https://www.kgis.org/KGISMaps/Map.htm (KGIS)
- **Tax Sale / Delinquent:** https://trustee.knoxcounty.org/services/tax-sale (Trustee tax-sale page; sale conducted by Clerk & Master)
- **Regional System:** KGIS (custom Esri ArcGIS) for GIS; Tyler-style property info portal
- **Tax Foreclosure:** Chancery Court Clerk & Master — annual (e.g. June 2 2026; public auction)

## 21. Marion County
- **Website:** https://marioncountytn.net/
- **Property Search:** https://assessment.cot.tn.gov/RE_Assessment/ (TN Comptroller statewide)
- **Tax Lookup:** https://secure.tennesseetrustee.org/?entity=marion&state=TN
- **GIS:** https://experience.arcgis.com/experience/1f97aefa540e426a8c4dcab939963ce3 (Marion County ArcGIS)
- **Tax Sale / Delinquent:** unknown (Clerk & Master handles delinquent taxes; no public tax-sale page found)
- **Regional System:** Tennessee Trustee (BIS) for tax; ESRI/ArcGIS for GIS; TN Comptroller TPAD for assessment
- **Tax Foreclosure:** Chancery Court Clerk & Master — cadence unknown

## 22. McMinn County
- **Website:** https://www.mcminncountytn.gov/
- **Property Search:** http://tn.mcminn.geopowered.com/ (GeoPowered property/GIS); also https://assessment.cot.tn.gov/RE_Assessment/
- **Tax Lookup:** https://secure.tennesseetrustee.org/?entity=mcminn&state=TN
- **GIS:** http://tn.mcminn.geopowered.com/ (GeoPowered); also https://tnmap.tn.gov/assessment/
- **Tax Sale / Delinquent:** unknown
- **Regional System:** Tennessee Trustee (BIS) for tax; GeoPowered for GIS/property; TN Comptroller TPAD for assessment
- **Tax Foreclosure:** County Trustee / Chancery Court Clerk & Master — annual (date varies)

## 23. Monroe County
- **Website:** https://monroetn.gov/
- **Property Search:** https://assessment.cot.tn.gov/TPAD/Search
- **Tax Lookup:** https://secure.tennesseetrustee.org/?entity=monroe&state=TN
- **GIS:** https://monroetn.gov/geographic-information-system-gis/ ; https://tnmap.tn.gov/assessment/
- **Tax Sale / Delinquent:** unknown
- **Regional System:** Tennessee Trustee (BIS) for tax; TN Comptroller TPAD for assessment; ESRI/ArcGIS (tnmap) for GIS
- **Tax Foreclosure:** Chancery Court Clerk & Master — annual (date varies)

## 24. Morgan County
- **Website:** https://www.morgancountytn.gov/
- **Property Search:** https://assessment.cot.tn.gov/RE_Assessment/
- **Tax Lookup:** https://secure.tennesseetrustee.org/?entity=morgan&state=TN
- **GIS:** https://tnmap.tn.gov/assessment/
- **Tax Sale / Delinquent:** https://www.morgancountytn.gov/court-ordered-tax-sales/ (Court Ordered Tax Sales / Back Tax Properties)
- **Regional System:** Tennessee Trustee (BIS) for tax; TN Comptroller TPAD for assessment; ESRI/ArcGIS (tnmap) for GIS
- **Tax Foreclosure:** County Back Tax Committee / County Mayor (deeds executed by County Mayor) — periodic, as advertised

## 25. Overton County
- **Website:** https://overtoncountytn.gov/
- **Property Search:** https://assessment.cot.tn.gov/RE_Assessment/
- **Tax Lookup:** https://secure.tennesseetrustee.org/?entity=overton&state=TN
- **GIS:** https://tnmap.tn.gov/assessment/
- **Tax Sale / Delinquent:** unknown
- **Regional System:** Tennessee Trustee (BIS) for tax; TN Comptroller TPAD for assessment; ESRI/ArcGIS (tnmap) for GIS
- **Tax Foreclosure:** Chancery Court Clerk & Master (or County Trustee) — annual (date varies)

## 26. Pickett County
- **Website:** https://www.dalehollow.com/government.htm
- **Property Search:** https://assessment.cot.tn.gov/RE_Assessment/
- **Tax Lookup:** https://secure.tennesseetrustee.org/?entity=pickett&state=TN
- **GIS:** https://tnmap.tn.gov/assessment/
- **Tax Sale / Delinquent:** unknown (third-party references tax sales; no official county page found)
- **Regional System:** Tennessee Trustee (BIS) for tax; TN Comptroller TPAD for assessment; ESRI/ArcGIS (tnmap) for GIS
- **Tax Foreclosure:** County Trustee (Tax Commissioner) — annual (date varies)

## 27. Polk County
- **Website:** https://www.polkgovernment.com/
- **Property Search:** https://assessment.cot.tn.gov/RE_Assessment/ ; https://www.polkgovernment.com/property-assess.php
- **Tax Lookup:** https://secure.tennesseetrustee.org/?entity=polk&state=TN (also CitiSenPortal)
- **GIS:** https://tnmap.tn.gov/assessment/
- **Tax Sale / Delinquent:** unknown
- **Regional System:** Tennessee Trustee (BIS) for tax; CitiSenPortal for trustee payments; TN Comptroller TPAD for assessment; ESRI/ArcGIS (tnmap) for GIS
- **Tax Foreclosure:** County Trustee — annual (date varies)

## 28. Roane County
- **Website:** https://roanecountytn.gov/
- **Property Search:** https://co-roane-tn.smartgovcommunity.com/Parcels/ParcelHome
- **Tax Lookup:** http://roane.tennesseetrustee.org/search.php
- **GIS:** https://roanecountytn.gov/gis/
- **Tax Sale / Delinquent:** https://roanecountytn.gov/clerk-and-master/
- **Regional System:** Granicus SmartGov Community (parcels) + Tennessee Trustee / CitiSen Portal (tax)
- **Tax Foreclosure:** Clerk & Master (Chancery Court) — annual tax sale (spring; next Mar 6 2027)

## 29. Scott County
- **Website:** https://scottcounty.com/
- **Property Search:** https://scottcounty.com/government/assessor-of-property/
- **Tax Lookup:** https://citisenportal.com/Trustee/StaticSearch/Scott%20County%20Trustee
- **GIS:** https://tnmap.tn.gov/assessment/ (state TN Property Viewer; no county-specific GIS found)
- **Tax Sale / Delinquent:** unknown
- **Regional System:** CitiSen Portal (tax) + custom scottcounty.com / in-house assessor
- **Tax Foreclosure:** Clerk & Master (Chancery Court) — annual delinquent tax sale

## 30. Sequatchie County
- **Website:** https://sequatchiecountytn.gov/
- **Property Search:** https://seqassessor.com/
- **Tax Lookup:** https://tennesseetrustee.org/index.php?entity=sequatchie&page=Y&state=TN
- **GIS:** https://tnmap.tn.gov/assessment/ (state viewer; county page https://seqassessor.com/gis-mapping/ under construction)
- **Tax Sale / Delinquent:** https://sequatchiecountytn.gov/directory/government/clerk-and-master/
- **Regional System:** seqassessor.com (custom assessor site) + Tennessee Trustee
- **Tax Foreclosure:** Clerk & Master (Chancery Court) — annual delinquent / back-tax sale

## 31. Sevier County
- **Website:** https://seviercountytn.gov/
- **Property Search:** https://seviercountytn.gov/government/county_officials/property_assessor.php
- **Tax Lookup:** https://tennesseetrustee.org/?entity=sevier&state=TN
- **GIS:** https://sevier-county-gis-public-sc-gis.hub.arcgis.com/
- **Tax Sale / Delinquent:** https://www.seviercountytn.gov/government/county_officials/county_trustee/tax_sale_information.php
- **Regional System:** ESRI ArcGIS Hub (GIS) + Tennessee Trustee
- **Tax Foreclosure:** Clerk & Master (Chancery Court) — annual tax sale (list published 20–30 days prior)

## 32. Sullivan County
- **Website:** https://sullivancountytn.gov/
- **Property Search:** https://sullivancountytn.gov/property-assessor/
- **Tax Lookup:** https://sullivantntrustee.gov/property-tax/
- **GIS:** https://tnmap.tn.gov/assessment/ (state viewer; no county-specific GIS found)
- **Tax Sale / Delinquent:** https://sullivantnchancery.com/delinquent-tax-sales/
- **Regional System:** Tennessee Trustee + county trustee site (sullivantntrustee.gov)
- **Tax Foreclosure:** Chancery Court Clerk & Master — two sales per year

## 33. Unicoi County
- **Website:** https://unicoicountytn.com/
- **Property Search:** https://unicoicountytn.com/assessor-of-property/
- **Tax Lookup:** https://secure.tennesseetrustee.org/index.php?entity=unicoi&page=Y&state=TN
- **GIS:** unknown (no county-specific viewer; Unicoi excluded from state TN Property Viewer)
- **Tax Sale / Delinquent:** unknown (Clerk & Master, phone 423-743-9541)
- **Regional System:** Tennessee Trustee + CitiSen Portal (tax) + custom assessor site
- **Tax Foreclosure:** Clerk & Master (Chancery Court) — annual delinquent tax sale

## 34. Union County
- **Website:** https://www.unioncountytn.gov/
- **Property Search:** https://www.unioncountytn.gov/assessor-of-property/
- **Tax Lookup:** https://tennesseetrustee.org/?entity=union&state=TN
- **GIS:** https://tnmap.tn.gov/assessment/ (state viewer; no county-specific GIS found)
- **Tax Sale / Delinquent:** https://www.unioncountytnclerkandmaster.com/delinquent-tax/
- **Regional System:** Tennessee Trustee + custom county sites (unioncountytn.gov, unioncountytnclerk.com)
- **Tax Foreclosure:** Clerk & Master (Chancery Court) — annual delinquent tax sale (after 2 yrs delinquency)

## 35. Van Buren County
- **Website:** https://vanburencountytn.gov/
- **Property Search:** https://tnmap.tn.gov/assessment/ (state TNMap viewer; assessment via TN Comptroller)
- **Tax Lookup:** https://citisenportal.com/Search/Van%20Buren%20County%20Trustee
- **GIS:** https://tnmap.tn.gov/assessment/
- **Tax Sale / Delinquent:** unknown (no dedicated page found; delinquent taxes turned over to Chancery Court ~April 1)
- **Regional System:** CitiSen Portal (tax payments); TNMap / TN Comptroller TPAD (assessment & GIS)
- **Tax Foreclosure:** County Trustee — annual (delinquent roll referred to Chancery Court each spring)

## 36. Warren County
- **Website:** https://www.warrencountytn.gov/
- **Property Search:** https://www.assessment.cot.tn.gov/RE_Assessment/SelectCounty.aspx?map=true&SelectCounty=089
- **Tax Lookup:** https://warren-tn.mygovonline.com/mod.php?mod=propertytax&mode=public_lookup
- **GIS:** https://tnmap.tn.gov/assessment/
- **Tax Sale / Delinquent:** https://www.warrencountytn.gov/trustee.asp
- **Regional System:** myGovOnline / eGov (tax portal); TN Comptroller TPAD (assessment); TNMap (GIS)
- **Tax Foreclosure:** County Trustee — annual

## 37. Washington County
- **Website:** https://www.washingtoncountytn.org/
- **Property Search:** https://tnmap.tn.gov/assessment/ (assessor page: https://www.washingtoncountytn.org/233/Property-Assessor)
- **Tax Lookup:** https://washington-tn.mygovonline.com/mod.php?mod=propertytax&mode=public_lookup
- **GIS:** https://www.washingtoncountytn.org/176/Geographic-Information-Systems-GIS (parcel/zoning: https://gis.washingtoncountytn.org/ZoningMap/)
- **Tax Sale / Delinquent:** https://washingtoncountycourtsales.com/tax-sale-information/ (Clerk & Master's Office)
- **Regional System:** myGovOnline / eGov (tax); TNMap (GIS); tax sales via washingtoncountycourtsales.com
- **Tax Foreclosure:** Chancery Court Clerk & Master — periodic/continuous (auctions of 2018-and-prior delinquent parcels)

## 38. White County
- **Website:** https://www.whitecountytn.gov/
- **Property Search:** https://tnmap.tn.gov/assessment/ (state TNMap viewer)
- **Tax Lookup:** https://citisenportal.com/Search/White%20County%20Trustee
- **GIS:** https://tnmap.tn.gov/assessment/
- **Tax Sale / Delinquent:** unknown (no official county page found; third-party catalogs exist, e.g. cosl.org)
- **Regional System:** CitiSen Portal (tax payments); TNMap / TN Comptroller TPAD (assessment & GIS)
- **Tax Foreclosure:** County Trustee — annual (delinquent tax sales conducted by Trustee)
