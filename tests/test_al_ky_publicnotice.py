"""Tests for the AL + KY public-notice scrapers (pure parsing — no browser).

AL fixtures use real notice language captured live from
alabamapublicnotices.com (2026-10-22 probes); KY fixtures use the
Master-Commissioner / delinquent-tax phrasing circulated on
kypublicnotice.com pages.
"""
import re

from scraper.al_publicnotice import (
    ALPublicNoticeScraper,
    _dehyphenate,
    _matches_any,
    AL_MORTGAGE_FC_PATTERNS,
    AL_TAX_FC_PATTERNS,
)
from scraper.ky_publicnotice import (
    KYPublicNoticeScraper,
    _best_marker_pos,
    _is_publication_notice,
)


def _scraper():
    """Build the AL scraper without touching browser machinery."""
    return ALPublicNoticeScraper.__new__(ALPublicNoticeScraper)


def _ky():
    return KYPublicNoticeScraper.__new__(KYPublicNoticeScraper)


# --- AL: hyphen-tolerant classification ------------------------------------

AL_MORTGAGE_NOTICE = """
NOTICE OF MORT-GAGE FORECLOS-URE SALE
STATE OF ALABAMA, COUNTY OF DEKALB.
Default having been made in the payment of the indebtedness secured by that
certain Mortgage executed by JOSEPH S. DAVIS, which mortgage is now owned by
GLENN GREES BANKING CORP., under and by virtue of the power of sale contained
in said mortgage, the undersigned will sell at public outcry to the highest
bidder for cash, in front of the Courthouse of DeKalb County, Alabama,
on December 10, 2026, the following real estate situated in DeKalb County;
commonly known as 214 Gault Avenue, Fort Payne, Alabama.
"""

AL_TAX_NOTICE = """
DELINQUENT TAX NOTICE - TALLADEGA COUNTY
The tax collecting official of Talladega County, Alabama, will sell the
following described real estate at public auction for delinquent taxes owed
for the tax year 2025, pursuant to Section 40-10-180, Code of Alabama 1975:
known as 401 North Street, Talladega, Alabama.
Sale date will be on January 8, 2027.
"""

AL_PROBATE_NOTICE = """
PUBLIC NOTICE - IN THE PROBATE COURT OF COVINGTON COUNTY, ALABAMA
IN RE: THE ESTATE OF STEVE WINDHAM, DECEASED. CASE NO. PC-2026-144
NOTICE OF HEARING TO: All heirs and/or next of kin.
"""

AL_BID_ADVERTISEMENT = """  # live Talladega row #851 — city construction bid notice
ADVERTISEMENT FOR BIDS Sealed proposals will be received by the Finance
Department at City Hall of the City of Talladega AL until 10:00 a.m. on
Wednesday, October 28, 2026, at 255 South Street West, Talladega, AL, for
the construction of FY 2026 City-Wide Select Street Rehabilitation.
"""

AL_SITE_FOOTER = """("...") publication that is required by law in
accordance with the Code of Alabama. Use of this site is governed by our
terms of use!"""

AL_DOMESTIC_RELATIONS = """
IN THE CIRCUIT COURT OF TALLADEGA COUNTY, ALABAMA CASE NO.: 61-DR-2025-900267
NOTICE OF DOMESTIC RELATIONS ACTION filed by plaintiff.
"""

AL_METES_ONLY_MORTGAGE = """  # no labeled street clause -> no address at all
NOTICE OF MORTGAGE FORECLOSURE SALE
Default having been made in the payment of the indebtedness, the
undersigned will sell at public outcry to the highest bidder, in front of
the Courthouse of Talladega County, Alabama, on December 10, 2026, the
following real estate: the Northwest Quarter of the Northeast Quarter of
Section 9, Township 14 South, Range 3 East, containing 42.1 acres, lying
in Talladega County, Alabama. Property street address not published.
"""

AL_ABANDONED_VEHICLE = """
Notice of Sale — the following abandoned vehicle(s) will be sold on
November 20, 2026, if not claimed: 2004 Honda located at 3351 Dauphin
Island Pkwy, Mobile.
"""


def test_al_dehyphenate():
    assert _dehyphenate("MORT-GAGE FORECLOS-URE SALE") == "mortgage foreclosure sale"
    assert _dehyphenate("TALLADEGA-TOWN") == "talladegatown"


def test_al_classify_mortgage_hyphen_split():
    assert _scraper()._classify(AL_MORTGAGE_NOTICE) == "mortgage_foreclosure"


def test_al_classify_tax():
    assert _scraper()._classify(AL_TAX_NOTICE) == "tax_foreclosure"


def test_al_classify_non_foreclosures():
    assert _scraper()._classify(AL_PROBATE_NOTICE) is None
    assert _scraper()._classify(" ") is None


def test_al_classify_ignores_site_footer_chrome():
    # regression: every AL detail page carries the site footer ("...required
    # by law in accordance with the Code of Alabama...") — the bare
    # "code of alabama" tax signal used to flip ALL mortgage notices to tax.
    page = AL_MORTGAGE_NOTICE + "\n" + AL_SITE_FOOTER
    assert _scraper()._classify(page) == "mortgage_foreclosure"


def test_al_grid_skip_adds_bids_and_domestic():
    sk = _scraper()._grid_skip
    assert sk(AL_BID_ADVERTISEMENT) is True
    assert sk(AL_DOMESTIC_RELATIONS) is True
    assert sk(AL_MORTGAGE_NOTICE) is False


def test_al_extract_address_labeled_only():
    # metes-and-bounds notices with no commonly-known-as clause must yield
    # NO address (the generic fallback once produced "1944 and which plat is
    # record" / "1 and 2 of Blackstone Est" / "9 East" garbage)
    assert _scraper()._extract_address(AL_METES_ONLY_MORTGAGE, "talladega") is None


def test_al_mortgage_patterns_exist():
    # sanity: AL mortgage set covers power-of-sale wording
    flat = _dehyphenate("under and by virtue of the POWER OF SALE contained")
    assert _matches_any(flat, AL_MORTGAGE_FC_PATTERNS)
    flat2 = _dehyphenate("sale under power foreclosure notice")
    assert _matches_any(flat2, AL_MORTGAGE_FC_PATTERNS)
    # tax statutory cite survives the hyphen-stripper via the raw-copy scan
    tax = "sale for delinquent taxes under Section 40-10-180 of the Code of Alabama"
    assert _scraper()._classify(tax) == "tax_foreclosure"


def test_al_grid_skip():
    sk = _scraper()._grid_skip
    assert sk(AL_ABANDONED_VEHICLE) is True
    assert sk(AL_MORTGAGE_NOTICE) is False
    assert sk(AL_PROBATE_NOTICE) is True


def test_al_extract_address_prefers_labeled_and_rejects_venue():
    addr = _scraper()._extract_address(
        "the following real estate situated in DeKalb County; commonly known "
        "as 214 Gault Avenue, Fort Payne, Alabama.", "dekalb")
    assert addr and "214 Gault Avenue" in addr
    # "in front of the Courthouse of DeKalb County" alone is a venue, not a
    # property — generic extractor is null-safe on courthouse text
    venue = "sale conducted in front of the Courthouse of DeKalb County, Alabama"
    assert _scraper()._extract_address(venue, "dekalb") in (None,)


def test_al_extract_sale_date_prefers_anchored():
    iso = _scraper()._extract_sale_date(AL_MORTGAGE_NOTICE)
    assert iso == "2026-12-10"
    iso2 = _scraper()._extract_sale_date(
        "Deed dated March 30, 2023; record Instrument No. 20230330; will sell "
        "at public outcry on December 10, 2026.")
    assert iso2 == "2026-12-10"


def test_al_extract_parcel():
    assert _scraper()._extract_parcel(
        "Parcel ID number is 27-08-01-0-002-022.000 as recorded.") \
        == "27-08-01-0-002-022.000"
    assert _scraper()._extract_parcel("no parcel info available") is None


def test_al_extract_sale_date_rejects_trailing_deed_anchor():
    """#885 regression: "on February 7, 2005, said mortgage recorded ..." —
    the deed anchor trails the date, and the on-page OCR hyphen-splits the
    true auction month ("Novem- ber 19, 2026")."""
    text = (
        "Default having been made in the payment of the indebtedness "
        "secured by that certain mortgage executed by Kelly Ray Dobbins "
        "originally in favor of Ameriquest Mortgage Company, on February "
        "7, 2005, said mortgage recorded in the Office of the Judge of "
        "Probate of DeKalb County; the undersigned under and by virtue of "
        "the power of sale contained in said mortgage, will sell at public "
        "outcry to the highest bidder for cash, in front of the main "
        "entrance of the Courthouse at Fort Payne, DeKalb County, Alabama, "
        "on Novem- ber 19, 2026, during the legal hours of sale."
    )
    assert _scraper()._extract_sale_date(text) == "2026-11-19"


def test_al_extract_sale_date_hud_notice_leaves_recitals_only():
    from datetime import date as _d
    # No sale-anchored date at all -> no date (never a deed-loan date)
    text = (
        "NOTICE OF FORECLOSURE SALE Default having been made... mortgage "
        "executed on January 15, 2012, recorded January 22, 2012 in Mlg "
        "Book 2012 Page 45."
    )
    assert _scraper()._extract_sale_date(text) is None


def test_al_row_county_from_grid_text():
    row = ("Fort Payne Times-Journal Wednesday, October 21, 2026 "
           "County: DeKalb NOTICE OF MORT-GAGE FORECLOS-URE SALE...")
    assert _scraper()._county_from_grid_text(row) == "dekalb"


AL_COUNTERY_CATALOG = {
    "blount", "cherokee", "cleburne", "dekalb", "talladega",
}
def test_al_county_set_matches_config():
    # 5 AL mountain counties; verify scraper targets exactly these
    from scraper.config import AL_FORECLOSURE_COUNTIES
    assert AL_FORECLOSURE_COUNTIES == [
        "blount", "cherokee", "cleburne", "dekalb", "talladega"]
    assert _scraper()._get_target_counties() == set(AL_FORECLOSURE_COUNTIES)


# --- KY: marker-gated classification + anchor-window extraction -------------

KY_MORTGAGE_PAGE = """
THE HARLAN DAILY ENTERPRISE - October 21, 2026 - LEGAL NOTICES
MASTER COMMISSIONER'S SALE
BY VIRTUE OF A DECREE AND ORDER OF SALE OF
THE HARLAN CIRCUIT COURT, rendered at the October 5, 2026 term in the
case of FIRST COMMUNITY BANK vs. JOHN DOE and JANE DOE, a judgment for
$78,400.00 and an order of sale having been made in said cause, the
 undersigned will offer for sale at public auction, at the
Harlan County Courthouse, Harlan, Kentucky, on Tuesday, November 17, 2026,
at 11:00 o'clock a.m. local time, the following described real property
situated lying and being in Harlan County, Kentucky, and being more
particularly described as follows:

BEING AND DESCRIBED AS: Tract 1: 1.4 acres more or less, situated on
U.S. 421, commonly known as 1512 Highway 421 South, Harlan, KY 40831.
KRS 387.860. Notice is given to all parties in interest and to any
persons having liens or claims against the property.

Given under my hand this 20th day of October, 2026.
"""

KY_TAX_PAGE = """
TRI-CITY NEWS — Cumberland, KY — Wednesday, October 14, 2026
COMMONWEALTH OF KENTUCKY, COUNTY OF KNOX
DEPARTMENT OF REVENUE, in rem FOR DELINQUENT PROPERTY TAXES
against the following described lands, pursuant to KRS 134.540 and
KRS 426.720, ORDER OF SALE will be satisfied by the sale of said
real property at public auction to the highest bidder at the Knox
County Courthouse in Barbourville, Kentucky, at 10:00 a.m. on
November 9, 2026:

Being a tract of land commonly known as 200 Old Highway 5, Barbourville,
Knox County, Kentucky, containing 1.8 acres more or less.
"""

KY_ABANDONED_VEHICLE_PAGE = """
NOTICE OF SALE
The following abandoned vehicle will be sold on November 5, 2026:
1998 Ford F-150, VIN 1FT..., if not claimed. Featured Self Storage,
114 Main Street, Middlesboro, KY.
"""

KY_CLASSIFIED_PAGE = """
HARLAN AND SURROUNDING AREAS 1512 Hwy 421 South Harlan, KY 40831 606-621-0005
BATHROOM REMODEL BATH & SHOWER UPDATES in as little as ONE DAY!
TAX HELP - End your tax worries today. Priority Tax 1-855-671-3561
"""

# Regression from live page 70224 (Whitley): a Master-Commissioner MORTGAGE
# sale page whose columns also carry a "right of redemption ... KRS 426.530"
# boilerplate and a stray "Commonwealth of Kentucky, 34th judicial circuit"
# caption from another column used to classify as tax_foreclosure.
KY_MC_REDHEMPTION_PAGE = """
CLASSIFIED DISPLAY ADVERTISEMENTS - NEWS JOURNAL - OCTOBER 21, 2026
way up. Credit Cards. Apartments MASTER COMMISSIONER SALES Some
restrictions apply. Medical Bills. Car loans: right of redemption will
exist pursuant to krs 426.530
In order to comply with the orders of the Whitley Circuit Court, the
Master Commissioner will sell the property described in the following
action(s) on MONDAY, OCTOBER 26, 2026 at the hour of 12:00 noon, at the
door of the Whitley County Judicial Center: a public hearing jorie
k. malone, et al, defendants. pursuant to a judgment and order of sale
entered by the court: vanderbilt mortgage and finance inc vs. jim doe
civil action 25-ci-512, tract on KY 26 commonly known as 10376 Highway
26, Rockholds, whitley county, ky 40759;
commonwealth of kentucky, 34th judicial circuit, whitley circuit court
"""

KY_SUMMONS_PAGE = """
SUMMONS — Commonwealth of Kentucky, Bell Circuit Court, civil action
filed October 6, 2026; service by publication upon John Doe, whose last
known address is unknown. You are required to appear.
"""


def test_ky_best_marker_pos():
    assert _best_marker_pos(KY_MORTGAGE_PAGE.lower()) is not None
    assert _best_marker_pos("nothing here") is None
    assert "master" in KY_MORTGAGE_PAGE.lower()  # marker long enough


def test_ky_classify_mortgage():
    assert _ky()._classify(KY_MORTGAGE_PAGE) == "mortgage_foreclosure"


def test_ky_classify_tax():
    assert _ky()._classify(KY_TAX_PAGE) == "tax_foreclosure"


def test_ky_classify_requires_marker():
    assert _ky()._classify(KY_CLASSIFIED_PAGE) is None
    assert _ky()._classify("") is None


def test_ky_mc_redemption_boilerplate_is_mortgage():
    # Live regression: the Whitley Master-Commissioner page stays Mtg despite
    # stray "Commonwealth" caption / KRS 426 redemption boilerplate columns.
    assert _ky()._classify(KY_MC_REDHEMPTION_PAGE) == "mortgage_foreclosure"
    # ...but the strong tax marker page still classifies tax.
    assert _ky()._classify(KY_TAX_PAGE) == "tax_foreclosure"


def test_ky_postable_filters_personal_property_and_publications():
    postable = _ky()._is_postable
    assert postable(KY_ABANDONED_VEHICLE_PAGE) is False
    assert postable(KY_SUMMONS_PAGE) is False
    assert postable(KY_MORTGAGE_PAGE) is True


def test_ky_extract_address_anchor_window():
    addr = _ky()._extract_address(KY_MORTGAGE_PAGE, "harlan")
    assert addr and "1512 Highway 421 South" in addr
    tax_addr = _ky()._extract_address(KY_TAX_PAGE, "knox")
    # The tax notice's address sits inside the anchor window; the *courthouse*
    # sale-venue address is rejected.
    assert "200 Old Highway 5" in (tax_addr or "")


def test_ky_extract_sale_date_anchors():
    assert _ky()._extract_sale_date(KY_MORTGAGE_PAGE) == "2026-11-17"
    assert _ky()._extract_sale_date(KY_TAX_PAGE) == "2026-11-09"


def test_ky_extract_sale_date_rejects_trailing_deed_anchor():
    """Anchor-trails-date recitals: "on March 3, 2014, a certain Mortgage
    was executed" is the loan date, never the auction."""
    text = (
        "NOTICE OF SALE Default having been made, ... on March 3, 2014, "
        "a certain Mortgage was executed by JOHN DOE; the undersigned "
        "Master Commissioner will offer for sale at public auction on "
        "Tuesday, November 17, 2026, at 11:00, the following described "
        "real property."
    )
    assert _ky()._extract_sale_date(text) == "2026-11-17"


def test_ky_extract_parcel_requires_marker_window():
    page = ("A parcel number printed far from any sale marker: "
            "Parcel 123-22-33.0 not attributed because there is no sale "
            "marker to anchor it.")
    assert _ky()._extract_parcel(
        page, _best_marker_pos(page.lower())) is None


def test_ky_labeled_address_stops_at_junk():
    # live row #895 interleave: the "Property Address:" label sits where the
    # newspaper columns run together — capture must stop at the street
    # suffix, not run 80 chars into the neighbouring column.
    page = ("Parcel/Map... satisfied in full. Property Address: 10376 "
            "Highway 26, Rockholds, Pursuant to KRS wburg.kyschools.us. KY "
            "40759. Parcel/Map ID#: 125-00-00-054.06")
    addr = _ky()._extract_address(page, "whitley")
    assert addr == "10376 Highway 26"


def test_ky_county_set_matches_config():
    from scraper.config import KY_FORECLOSURE_COUNTIES
    assert KY_FORECLOSURE_COUNTIES == [
        "bell", "harlan", "knox", "perry", "whitley"]
    assert _ky()._get_target_counties() == set(KY_FORECLOSURE_COUNTIES)


def test_ky_publication_notice_patterns():
    assert _is_publication_notice(KY_SUMMONS_PAGE) is True
    assert _is_publication_notice("notice of publication upon nonresident") is True
    assert _is_publication_notice(KY_MORTGAGE_PAGE) is False
