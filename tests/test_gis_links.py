"""Card GIS links, hosted-viewer URLs and the viewer's layer markup.

Link-depth policy: a card's GIS button must land on the property — a stored
parcel-deep link (GA qPublic / TN TPAD) first, otherwise the hosted viewer,
which draws NC's parcel layer and centres anywhere else on coordinates.
"""
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

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
    """static/gis_viewer.html — five basemaps, NC LiDAR default, water overlay."""

    def test_nc_rows_default_to_the_state_lidar(self):
        # NC rows open on NC OneMap's own DEM03 hillshade; every other state
        # (and NC when the service is down) keeps the cached 3DEP relief.
        assert 'const DEFAULT_BASE = service ? "lidar_nc" : "lidar";' in VIEWER

    def test_all_five_basemaps_are_offered(self):
        for key in ("lidar_nc", "lidar", "imagery", "topo", "street"):
            assert f'data-base="{key}"' in VIEWER, key
        assert "lidar_nc: buildLidarNc" in VIEWER
        assert "lidar: buildLidar" in VIEWER

    def test_lidar_stacks_usgs_relief_over_esri_hillshade(self):
        assert "USGSShadedReliefOnly" in VIEWER
        assert "Elevation/World_Hillshade" in VIEWER

    def test_nc_lidar_is_retiled_through_this_app(self):
        # The SDK's ImageryLayer rendered the dynamic DEM03 service with
        # no-data wedges; XYZ tiles are deterministic and cacheable. A
        # root-relative template would resolve to a bogus host, so it must be
        # absolute (same trap as the NC ortho template).
        assert 'location.origin + "/gis/lidar/{level}/{col}/{row}.png"' in VIEWER
        assert "/gis/lidar/" in VIEWER
        assert "DEM03_hillshade" in VIEWER or "DEM03" in VIEWER

    def test_lidar_probe_falls_back_without_persisting(self):
        # A dead service must not lock the visitor to the 3DEP fallback.
        assert 'applyBase("lidar", false)' in VIEWER
        assert "if (persist !== false) writePref(STORAGE_KEY, key);" in VIEWER

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
        # v2 adds the NC LiDAR basemap; a stale v1 "lidar" preference must not
        # pin returning visitors to the coarse 3DEP relief.
        assert '"mountains-basemap-v2"' in VIEWER
        assert '"mountains-water"' in VIEWER


class TestLidarTileRoute:
    """GET /gis/lidar/<z>/<x>/<y>.png — NC DEM03 re-tiled as XYZ."""

    @pytest.fixture()
    def app(self, tmp_path, monkeypatch):
        import base64
        from scraper.server import app
        from scraper.config import config as cfg
        cfg.db_path = tmp_path / "test.db"
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret"
        token = base64.b64encode(b"winmutt:1234asdf").decode()
        _orig_client = app.test_client

        def _authed_client(*args, **kwargs):
            c = _orig_client(*args, **kwargs)
            c.environ_base["HTTP_AUTHORIZATION"] = f"Basic {token}"
            return c

        monkeypatch.setattr(app, "test_client", _authed_client)
        return app

    @pytest.fixture()
    def captured(self, monkeypatch):
        """Replace the NC OneMap HTTP helper and record the exportImage URL."""
        from scraper import nconemap_net

        seen = {}

        class _Resp:
            status_code = 200
            content = b"\x89PNG\r\n\x1a\nfake"
            headers = {"content-type": "image/png"}

        def fake_get(url, **kwargs):
            seen["url"] = url
            return _Resp()

        monkeypatch.setattr(nconemap_net, "get", fake_get)
        return seen

    def test_tile_bbox_matches_the_xyz_grid(self, app, captured):
        import math

        with app.test_client() as client:
            resp = client.get("/gis/lidar/17/35315/51632.png")
        assert resp.status_code == 200
        assert resp.mimetype == "image/png"
        assert "max-age=86400" in resp.headers["Cache-Control"]

        url = captured["url"]
        assert "DEM03_hillshade/ImageServer/exportImage" in url
        assert "bboxSR=3857" in url and "imageSR=3857" in url
        assert "size=256,256" in url
        bbox = [float(v) for v in
                url.split("bbox=")[1].split("&")[0].split(",")]
        assert len(bbox) == 4

        # Independent slippy-map check: the tile centre must sit on the
        # Haywood parcel these numbers came from, and the bbox corners must
        # bracket it.
        n = 1 << 17
        z, x, y = 17, 35315, 51632
        lng = (x + 0.5) / n * 360 - 180
        lat = math.degrees(math.atan(math.sinh(
            math.pi * (1 - 2 * (y + 0.5) / n))))
        assert abs(lng - -83.0036) < 0.01
        assert abs(lat - 35.6375) < 0.01

        x0, y0, x1, y1 = bbox
        assert x0 < x1 and y0 < y1
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        assert abs(cx - lng / 180 * 20037508.342789244) < 1.0
        assert abs(cy - 20037508.342789244 * math.log(
            math.tan(math.pi / 4 + math.radians(lat) / 2)) / math.pi) < 1.0

    def test_out_of_range_tiles_are_404(self, app, captured):
        with app.test_client() as client:
            assert client.get("/gis/lidar/20/1/1.png").status_code == 404
            assert client.get("/gis/lidar/5/32/0.png").status_code == 404
            assert client.get("/gis/lidar/5/0/32.png").status_code == 404
        assert "url" not in captured

    def test_upstream_non_image_is_a_bad_gateway(self, app, monkeypatch):
        from scraper import nconemap_net

        class _Resp:
            status_code = 200
            content = b'{"error": "nope"}'
            headers = {"content-type": "application/json"}

        monkeypatch.setattr(nconemap_net, "get", lambda url, **k: _Resp())
        with app.test_client() as client:
            assert client.get("/gis/lidar/14/4414/6454.png").status_code == 502

    def test_repeat_tile_is_served_from_the_image_cache(self, app, captured):
        from scraper.server import _GIS_IMAGE_CACHE

        with app.test_client() as client:
            assert client.get("/gis/lidar/15/8828/12907.png").status_code == 200
            first = captured["url"]
            # A different image response would be visible; the cache must answer.
            assert client.get("/gis/lidar/15/8828/12907.png").status_code == 200
        assert captured["url"] == first
        assert _GIS_IMAGE_CACHE.get(("lidar", 15, 8828, 12907)) is not None


class TestNcOneMapTlsFallback:
    """scraper/nconemap_net.py — expired-certificate fallback, strict opt-out."""

    def setup_method(self):
        from scraper import nconemap_net
        nconemap_net.reset_tls_state()

    def teardown_method(self):
        from scraper import nconemap_net
        nconemap_net.reset_tls_state()

    def test_certificate_error_retries_unverified_and_is_remembered(
            self, monkeypatch):
        from scraper import nconemap_net as net

        calls = []

        class _Strict:
            def request(self, method, url, **kwargs):
                calls.append("strict")
                raise RuntimeError(
                    "curl: (60) SSL certificate problem: certificate has expired")

        class _Insecure:
            def request(self, method, url, **kwargs):
                calls.append("insecure")
                return "response"

        monkeypatch.delenv("NCONEMAP_TLS_STRICT", raising=False)
        monkeypatch.setattr(net, "_session",
                            lambda verify: _Strict() if verify else _Insecure())

        assert net.get("https://services.nconemap.gov/x") == "response"
        assert calls == ["strict", "insecure"]
        assert net._state["insecure_until"] > 0

        # The verdict is cached: no doomed handshake on the next tile.
        calls.clear()
        assert net.get("https://services.nconemap.gov/x") == "response"
        assert calls == ["insecure"]

    def test_strict_mode_refuses_the_fallback(self, monkeypatch):
        from scraper import nconemap_net as net

        class _Strict:
            def request(self, method, url, **kwargs):
                raise RuntimeError(
                    "curl: (60) SSL certificate problem: certificate has expired")

        monkeypatch.setenv("NCONEMAP_TLS_STRICT", "1")
        monkeypatch.setattr(net, "_session", lambda verify: _Strict())
        with pytest.raises(RuntimeError):
            net.get("https://services.nconemap.gov/x")
        assert net._state["insecure_until"] == 0

    def test_non_certificate_errors_are_not_retried(self, monkeypatch):
        from scraper import nconemap_net as net

        class _Strict:
            def request(self, method, url, **kwargs):
                raise RuntimeError("connection reset by peer")

        monkeypatch.delenv("NCONEMAP_TLS_STRICT", raising=False)
        monkeypatch.setattr(net, "_session", lambda verify: _Strict())
        with pytest.raises(RuntimeError, match="connection reset"):
            net.get("https://services.nconemap.gov/x")
        assert net._state["insecure_until"] == 0

    def test_renewed_certificate_drops_the_fallback(self, monkeypatch):
        import time
        from scraper import nconemap_net as net

        class _Ok:
            def request(self, method, url, **kwargs):
                return "verified"

        monkeypatch.delenv("NCONEMAP_TLS_STRICT", raising=False)
        monkeypatch.setattr(net, "_session", lambda verify: _Ok())
        # Pretend the cached verdict has expired (REPROBE_SECONDS elapsed).
        net._state["insecure_until"] = time.monotonic() - 1
        assert net.get("https://services.nconemap.gov/x") == "verified"
        assert net._state["insecure_until"] == 0
