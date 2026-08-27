"""NC County GIS Viewer URLs for tax foreclosure properties.

All 100 NC counties are served by the **NC OneMap statewide parcel service**,
which provides a single, always-working ArcGIS map viewer for every parcel in
the state. We use that viewer as the canonical "GIS" link rather than the
individual county tax-portal pages, because:

  * County portal URLs are not standardized and almost none accept a parcel
    number via a URL query parameter (many 404 or land on a generic page).
  * NC OneMap's ArcGIS Map Viewer accepts `center=<lng>,<lat>&level=16` to
    zoom straight to a parcel's centroid, and loads the statewide parcel
    polygon layer so boundaries are visible.

``get_gis_viewer_url`` therefore returns a deep link into the NC OneMap
Map Viewer. When coordinates are known the link is centered on the parcel;
otherwise it opens the statewide parcel map (which still loads correctly).

Usage:
    url = get_gis_viewer_url(county, parcel, lng=..., lat=...)
"""
from urllib.parse import quote


# NC OneMap Parcels MapServer Layer 1 (polygons), loaded in ArcGIS Map Viewer.
# This is a single, statewide, human-viewable map that works for every NC
# county — no per-county portal guessing required.
#
# ``basemap=hybrid`` shows the parcel polygon over a satellite/aerial photo
# (Esri World Imagery) with labels, so the link opens directly on a photo of
# the property rather than a plain street map.
NC_ONEMAP_VIEWER_URL = (
    "https://www.arcgis.com/apps/mapviewer/index.html"
    "?url=https%3A%2F%2Fservices.nconemap.gov%2Fsecure%2Frest%2Fservices%2F"
    "NC1Map_Parcels%2FMapServer%2F1"
    "&basemap=hybrid"
)

# Path (same-origin, served by our Flask proxy) to the NC OneMap statewide
# parcel layer. The new ArcGIS Online Map Viewer ignores the legacy ``?url=``
# deep-link parameter, so instead we host our own minimal viewer
# (``static/gis_viewer.html``) that loads this layer through our server-side
# proxy at ``/gis/proxy/...``. The proxy forwards to nconemap.gov (CORS-free,
# server-to-server) so the browser never makes a cross-origin request.
NC_ONEMAP_PROXY_LAYER = "/gis/proxy/secure/rest/services/NC1Map_Parcels/MapServer/1"


# Reference registry of official county GIS/tax portal home pages. These are
# kept for documentation/human reference only; they are NOT used to build
# parcel deep links because the portals do not reliably accept parcel numbers
# via URL. The working parcel link is the NC OneMap viewer above.
#
# Hostnames verified to resolve (DNS) on 2026-08-23. Counties whose official
# .gov/.org domain could not be resolved from the scrape container are noted
# inline; the markdown summary (nc_county_summary.md) carries the fully
# verified, per-county detail links.
GIS_VIEWER_URLS = {
    "alleghany":    "https://www.alleghanycounty.org",
    "ashe":         "https://ashecounty.org",
    "avery":        "https://www.averycountync.gov",
    "buncombe":     "https://community.spatialest.com/nc/buncombe/#/Property-Search/",
    "burke":       "https://www.burkecounty.org",
    "catawba":      "https://www.catawbacountync.gov",
    "cherokee":     "https://www.cherokeecounty.org",
    "clay":         "https://www.claycountync.gov",
    "graham":       "https://grahamcounty.org",
    "haywood":      "https://www.haywoodcountync.gov",
    "henderson":    "https://lrcpwa.ncptscloud.com/Henderson/",
    "jackson":      "https://www.jacksoncounty.org",
    "madison":      "https://www.madisoncountync.gov",
    "mcdowell":     "https://mcdowellnc.gov",
    "mitchell":     "https://www.mitchellcountync.gov",
    "swain":        "https://www.swaincountync.gov",
    "transylvania": "https://gis.transylvaniacounty.org/portal/apps/sites/#/transylvania-county-hub-site",
    "watauga":      "https://gissvr.watgov.org/maps/",
    "wilkes":       "https://wilkescountync.com",  # official domain unverified from container
    "yancey":       "https://www.yanceycountync.gov",
}


# Per-county qPublic app identifiers (AppID/LayerID/PageID) for the GA
# mountain counties we track. These let us build a DIRECT parcel-detail deep
# link instead of a generic search landing. Discovered 2026-08-23 from the
# live qPublic apps. White verified 2026-08-25 (AppID=982&LayerID=19945&
# PageTypeID=1&PageID=8773&KeyValue=018D%20019).
#
# NOTE: qPublic is Cloudflare-WAF-protected and returns HTTP 403 to automated
# requests, so the scraper cannot *fetch* parcel data from it — only a human
# browser can open these links. They are used purely as clickable GIS links.
# Per-county qPublic app identifiers (AppID/LayerID) for the GA mountain
# counties we track. Each county lists one or more "pages"; ``page_type_id``
# and ``page_id`` identify the parcel-viewing page. The ``space`` flag marks
# whether the parcel key carries an internal space in QPublic, ``space_at`` is
# the fallback split position (used when the parcel arrives without a space),
# and ``space_count`` is how many spaces QPublic expects between the segments.
#
# Lumpkin County stores parcels as "035 278" (district, single space, parcel)
# but QPublic's KeyValue wants FOUR spaces: "035    278" (verified canonical
# map URL AppID=991&LayerID=20168&PageTypeID=1&PageID=8779&KeyValue=047++++363).
# NOTE: a browser-clicked Lumpkin link also carries a per-parcel "&Q=<id>"
# token (e.g. Q=870085544 for parcel 047 363). That Q is a session/parcel token
# -- a mismatched Q returns HTTP 403, so we intentionally OMIT Q and rely on the
# KeyValue-only link, which returns HTTP 200 and resolves to the parcel map.
#
# Towns County uses TWO different pages:
#   * numeric / alphanumeric parcels (e.g. 0009A041) -> PageTypeID=1, PageID=7007
#   * "YH"-prefixed parcels (e.g. YH02078)          -> PageTypeID=4, PageID=7010
#     with an internal space ("YH02 078").
GA_QPUBLIC_APPS = {
    "gilmer": {"app_id": 672, "layer_id": 11357, "pages": [
        {"page_type_id": 4, "page_id": 4736, "space": False}]},
    "lumpkin": {"app_id": 991, "layer_id": 20168, "pages": [
        {"page_type_id": 1, "page_id": 8779, "space": True,
         "space_at": 3, "space_count": 4}]},
    "towns":  {"app_id": 846, "layer_id": 15440, "pages": [
        {"page_type_id": 1, "page_id": 7007, "space": False},
        {"page_type_id": 4, "page_id": 7010, "space": True,
         "space_at": 4, "space_count": 1}]},
    "white":  {"app_id": 982, "layer_id": 19945, "pages": [
        {"page_type_id": 1, "page_id": 8773, "space": True,
         "space_at": 3, "space_count": 1}]},
}


def get_ga_gis_url(county: str, parcel: str = "") -> str:
    """Georgia parcel viewer URL via Schneider Corp qPublic.

    For counties in ``GA_QPUBLIC_APPS`` this builds a direct parcel-detail
    deep link (``PageTypeID=4&KeyValue=<parcel>``) when a parcel is known, or
    the county's parcel-search page otherwise. Other GA counties fall back to
    the generic ``App=<County>CountyGA`` search landing.
    """
    c = (county or "").strip().lower()
    if c.endswith("county"):
        c = c[: -len("county")].strip()
    parcel = (parcel or "").strip()

    cfg = GA_QPUBLIC_APPS.get(c)
    if cfg:
        pages = cfg["pages"]
        # Towns picks the page by parcel prefix: "YH"-prefixed parcels use the
        # detailed page (PageTypeID=4, internal space); everything else uses the
        # standard parcel page (PageTypeID=1, no space).
        if c == "towns" and parcel.upper().startswith("YH"):
            page = pages[1]
        else:
            page = pages[0]
        base = (f"https://qpublic.schneidercorp.com/Application.aspx"
                f"?AppID={cfg['app_id']}&LayerID={cfg['layer_id']}")
        if parcel:
            kv = parcel
            if page.get("space"):
                space_count = page.get("space_count", 1)
                parts = kv.split()
                if len(parts) > 1:
                    # Parcel already carries an internal space (e.g. Lumpkin
                    # "035 278"); QPublic wants e.g. three spaces -> "035   278".
                    kv = (" " * space_count).join(parts)
                else:
                    space_at = page.get("space_at", 4)
                    if len(kv) > space_at:
                        kv = kv[:space_at] + (" " * space_count) + kv[space_at:]
            kv = quote(kv).replace("%20", "+")
            return (f"{base}&PageTypeID={page['page_type_id']}"
                    f"&PageID={page['page_id']}&KeyValue={kv}")
        return f"{base}&PageTypeID=2&PageID={page['page_id']}"
    cc = c.title().replace(" ", "")
    if not cc:
        return "https://qpublic.schneidercorp.com/"
    return (
        "https://qpublic.schneidercorp.com/Application.aspx"
        f"?App={cc}CountyGA&Layer=Parcels&PageType=Search"
    )


def get_nconemap_viewer_url(lng: float = None, lat: float = None,
                            parcel: str = None, county: str = None) -> str:
    """Build a same-origin GIS viewer URL for the NC OneMap parcel layer.

    The link points at our hosted ``static/gis_viewer.html`` which loads the
    NC OneMap layer through the server-side ``/gis/proxy/...`` route (avoiding
    the broken ArcGIS Online ``?url=`` deep link and any cross-origin CORS
    issues). When parcel coordinates are known the viewer is centered/zoomed on
    the parcel; otherwise it opens the statewide parcel layer.
    """
    svc = quote(NC_ONEMAP_PROXY_LAYER, safe="")
    url = f"/static/gis_viewer.html?service={svc}"
    if lng is not None and lat is not None:
        try:
            url += f"&center={float(lng):.6f},{float(lat):.6f}&level=16"
        except (TypeError, ValueError):
            pass
    if parcel:
        url += f"&parcel={quote(str(parcel))}"
    return url


def get_gis_viewer_url(county: str, parcel: str,
                        lng: float = None, lat: float = None,
                        state: str = None) -> str:
    """Generate a working GIS viewer URL for a parcel.

    For Georgia (``state='GA'``) properties, returns the county's qPublic
    (Schneider Corp) parcel-search page instead, since NC OneMap does not
    cover Georgia. All other states (NC, TN, ...) return a same-origin NC
    OneMap viewer link centered on the parcel when coordinates are available.
    """
    if state and str(state).strip().upper() == "GA":
        return get_ga_gis_url(county, parcel)

    return get_nconemap_viewer_url(lng, lat, parcel, county)
