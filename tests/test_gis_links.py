"""Card GIS links, hosted-viewer URLs and the viewer's layer markup.

Link-depth policy: a card's GIS button must land on the property — a stored
parcel-deep link (GA qPublic / TN TPAD) first, otherwise the hosted viewer,
which draws NC's parcel layer and centres anywhere else on coordinates.
"""
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from scraper.gis_urls import (
    HOSTED_VIEWER_PATH,
    NC_ONEMAP_PROXY_LAYER,
    get_hosted_viewer_url,
    get_nconemap_viewer_url,
)
from scraper.server import _gis_button_url, _viewer_map_url

VIEWER = (Path(__file__).resolve().parent.parent
          / "static" / "gis_viewer.html").read_text(encoding="utf-8")


def _qs(url: str) -> dict:
    return parse_qs(urlparse(url).query)


class TestHostedViewerUrl:
    def test_nc_link_carries_the_proxied_parcel_layer(self):
        url = get_hosted_viewer_url(-83.612368, 35.248915, "05430T001", state="NC")
        assert url.startswith(HOSTED_VIEWER_PATH + "?")
        params = _qs(url)
        assert params["service"] == [NC_ONEMAP_PROXY_LAYER]
        assert params["center"] == ["-83.612368,35.248915"]
        assert params["level"] == ["16"]
        assert params["parcel"] == ["05430T001"]

    def test_other_states_get_the_viewer_without_a_parcel_layer(self):
        url = get_hosted_viewer_url(-84.18, 34.86, state="GA")
        assert "service=" not in url
        assert "state=GA" in url
        assert "center=-84.180000,34.860000" in url

    def test_nconemap_helper_delegates_so_stored_links_keep_their_shape(self):
        assert (get_nconemap_viewer_url(-82.9, 35.4, "8711-00-6993", "Haywood")
                == get_hosted_viewer_url(-82.9, 35.4, "8711-00-6993", state="NC"))

    def test_bad_coordinates_are_dropped_not_rendered(self):
        assert "center=" not in get_hosted_viewer_url("n/a", "n/a", state="NC")


class TestCardGisButton:
    def test_stored_external_link_wins_over_the_hosted_viewer(self):
        row = {"state": "GA", "county": "Lumpkin", "latitude": 34.57,
               "longitude": -84.18,
               "gis_url": "https://www.acuit.org/GIS/LumpkinGA/Parcel/Report/?id=1234"}
        assert _gis_button_url(row) == row["gis_url"]

    def test_stored_hosted_link_is_used_verbatim(self):
        stored = HOSTED_VIEWER_PATH + "?center=-82.88,35.32&level=16"
        assert _gis_button_url({"state": "NC", "gis_url": stored}) == stored

    def test_nc_row_without_a_link_builds_the_parcel_viewer(self):
        url = _gis_button_url({"state": "NC", "county": "Macon", "latitude": 35.24,
                               "longitude": -83.61, "parcel_number": "0543-01-02"})
        assert url.startswith(HOSTED_VIEWER_PATH)
        params = _qs(url)
        assert params["service"] == [NC_ONEMAP_PROXY_LAYER]
        assert params["parcel"] == ["0543-01-02"]

    def test_non_nc_coordinates_still_get_a_map(self):
        url = _gis_button_url({"state": "TN", "county": "Knox",
                               "latitude": 35.96, "longitude": -83.92})
        assert url.startswith(HOSTED_VIEWER_PATH) and "state=TN" in url

    def test_nothing_known_means_no_button(self):
        assert _gis_button_url({"state": "SC", "county": "Oconee"}) is None
        assert _viewer_map_url({"state": "SC", "county": "Oconee"}) is None

    def test_external_link_only_row_gets_no_embedded_map(self):
        """Third-party viewers block framing, so the detail page shows no iframe."""
        row = {"state": "TN", "county": "Knox", "latitude": 35.96,
               "longitude": -83.92, "gis_url": "https://tnmap.tn.gov/assessment/"}
        assert _viewer_map_url(row) is None
        assert _gis_button_url(row) == row["gis_url"]


class TestViewerLayerMarkup:
    """static/gis_viewer.html — four basemaps, LiDAR default, water overlay."""

    def test_lidar_is_the_default_basemap(self):
        assert 'const DEFAULT_BASE = "lidar";' in VIEWER

    def test_all_four_basemaps_are_offered(self):
        for key in ("lidar", "imagery", "topo", "street"):
            assert f'data-base="{key}"' in VIEWER, key
        for key in ("lidar", "imagery", "topo", "street"):
            assert f"{key}: build{key.capitalize()}" in VIEWER or \
                   f"{key}: build" in VIEWER, key

    def test_lidar_stacks_usgs_relief_over_esri_hillshade(self):
        assert "USGSShadedReliefOnly" in VIEWER
        assert "Elevation/World_Hillshade" in VIEWER

    def test_nc_ortho_is_absolute_and_proxied(self):
        # WebTileLayer resolves a root-relative template to a bogus host, so
        # the template must be built from location.origin.
        ortho_block = VIEWER[VIEWER.index("NC_ORTHO_PATH"):VIEWER.index("esriImagery")]
        assert "location.origin + NC_ORTHO_PATH" in ortho_block
        assert "/gis/proxy/" in ortho_block

    def test_water_overlay_uses_usgs_nhd_at_both_scales(self):
        assert "hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer" in VIEWER
        for layer in ("/10", "/4", "/12", "/6"):
            assert f'NHD + "{layer}"' in VIEWER, layer
        assert 'data-overlay="water"' in VIEWER

    def test_preferences_are_persisted(self):
        assert '"mountains-basemap"' in VIEWER
        assert '"mountains-water"' in VIEWER
