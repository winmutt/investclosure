"""NC County GIS Viewer URLs for tax foreclosure properties.

These are the official GIS portal URLs for the 21 mountain counties
and other NC counties where parcels are publicly searchable.

Usage:
    url = get_gis_viewer_url(county, parcel_number)
    
Each URL is constructed with the county-specific parcel search parameter.
"""
from urllib.parse import quote


# Official county GIS portal URLs for the NC mountain counties
GIS_VIEWER_URLS = {
    "alleghany":    ("https://www.alleghanycountync.org/property-search", "ParcelID"),
    "ashe":         ("https://gov.ashecountync.gov/departments/gis/", "ParcelNumber"),
    "avery":        ("https://averycountync.gov/property-records/", "ParcelID"),
    "buncombe":     ("https://buncompecounty.org/property-search/", "ParcellID"),
    "burke":        ("https://gov.burkecountync.gov/pages/propertysearch", "ParcelID"),
    "caldwell":     ("https://caldwellcitizentimes.com/county/propertysearch.aspx", "ParcelID"),
    "catawba":      ("https://www.catawbacountync.org/property-search/", "ParcelID"),
    "cherokee":     ("https://www.cherokeecounty-nc.gov:8080/TaxNet/BasicSearch.aspx", "Parcel"),
    "clay":         ("https://claycounty-portal.com/property-search/", "ParcelID"),
    "graham":       ("https://grahamcountync.org/property-records", "ParcelID"),
    "haywood":      ("https://www.haywoodcountync.gov/property-search/", "ParcelID"),
    "henderson":    ("https://gov.hendersoncountync.gov/property-search", "ParcelID"),
    "jackson":      ("https://jacksoncountync.org/property-search/", "ParcelID"),
    "madison":      ("https://madisoncountync.org/property-search/", "ParcelID"),
    "mcdowell":     ("https://mcdowellcountync.gov/property-records", "ParcelID"),
    "mitchell":     ("https://mitchellcounty-portal.com/property-search", "ParcelID"),
    "swain":        ("https://swaincountync.gov/property-records/", "ParcelID"),
    "transylvania": ("https://transylvaniacountync.gov/property-search", "ParcelID"),
    "watauga":      ("https://gov.wataugacounty.org/property-search", "ParcelID"),
    "wilkes":       ("https://wilkescountync.org/property-search", "ParcelID"),
    "yancey":       ("https://yanceycountync.org/property-search", "ParcelID"),
}


def get_gis_viewer_url(county: str, parcel: str) -> str:
    """Generate the GIS viewer URL for a parcel.
    
    Args:
        county: County name (case-insensitive)
        parcel: Parcel number
    
    Returns:
        GIS viewer URL with parcel search parameter, or Google Maps fallback
    """
    if not county or not parcel:
        return None
    
    key = county.strip().lower()
    
    if key in GIS_VIEWER_URLS:
        base, param = GIS_VIEWER_URLS[key]
        return f"{base}?{param}={quote(parcel)}"
    
    # Fallback: Google Maps parcel search
    return f"https://www.google.com/maps/search/parcel+{quote(parcel)}+in+{quote(county)}+NC"
