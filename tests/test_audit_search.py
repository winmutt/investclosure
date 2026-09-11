"""Audit trail + parcel-aware search (Camp Wahsega #341 follow-up)."""
from __future__ import annotations

from scraper import db as scraper_db


def _db(tmp_path):
    return scraper_db._ensure_db(tmp_path / "audit.db")


def _insert(conn, **kw):
    base = dict(
        source="ga_publicnotice", source_listing_id="lumpkin:028 030",
        url="http://x", address="Camp Wahsega Road", city=None,
        county="lumpkin", state="GA", zip_code=None, latitude=None,
        longitude=None, price_cents=1, acres=10.13,
        description="tax sale notice", property_type="tax_foreclosure",
        parcel_number="028 030", court_case=None,
    )
    base.update(kw)
    return scraper_db.insert_property(conn, **base)


class TestAuditLog:
    def test_archive_and_unarchive_write_history(self, tmp_path):
        conn = _db(tmp_path)
        _insert(conn)
        row = conn.execute(
            "SELECT id FROM properties WHERE parcel_number='028 030'").fetchone()
        pid = row["id"]
        assert scraper_db.archive_property(conn, pid, reason="sold at auction",
                                           archived_by="dashboard") is True
        assert scraper_db.unarchive_property(conn, pid, reason="owner request",
                                             restored_by="dashboard") is True
        evs = scraper_db.get_audit_log(conn, pid)
        assert [(e["action"], e["actor"]) for e in evs] == [
            ("unarchive", "dashboard"), ("archive", "dashboard")]
        assert evs[1]["reason"] == "sold at auction"
        assert evs[1]["prev_status"] == "active"
        assert evs[0]["prev_status"] == "archived"

    def test_archive_below_acres_logs_each_property(self, tmp_path):
        conn = _db(tmp_path)
        _insert(conn, source_listing_id="a:1", parcel_number="001",
                address="One", acres=0.5)
        _insert(conn, source_listing_id="a:2", parcel_number="002",
                address="Two", acres=50.0)
        assert scraper_db.archive_below_acres(conn, 2.0) == 1
        rows = conn.execute("SELECT * FROM audit_log").fetchall()
        assert len(rows) == 1
        assert rows[0]["actor"] == "system"
        assert rows[0]["reason"] == "below_min_acres"


class TestParcelSearch:
    def _client(self, monkeypatch, tmp_path):
        import base64
        import scraper.server as server
        db_path = tmp_path / "search.db"

        def _conn():
            return scraper_db._ensure_db(db_path)

        token = base64.b64encode(b"winmutt:1234asdf").decode()
        _orig_client = server.app.test_client

        def _authed_client(*args, **kwargs):
            client = _orig_client(*args, **kwargs)
            client.environ_base["HTTP_AUTHORIZATION"] = f"Basic {token}"
            return client

        monkeypatch.setattr(server.app, "test_client", _authed_client)
        conn = _conn()
        _insert(conn)
        _insert(conn, source_listing_id="lumpkin:999", parcel_number="999",
                address="Other Rd", acres=5.0, court_case="26SP000001",
                description="mortgage sale")
        conn.close()
        monkeypatch.setattr(server, "get_conn", _conn)
        return server.app.test_client()

    def test_search_by_parcel_number(self, monkeypatch, tmp_path):
        c = self._client(monkeypatch, tmp_path)
        html = c.get("/properties?q=028%20030").get_data(as_text=True)
        assert "Camp Wahsega Road" in html
        assert "Other Rd" not in html

    def test_search_by_court_case(self, monkeypatch, tmp_path):
        c = self._client(monkeypatch, tmp_path)
        html = c.get("/properties?q=26SP000001").get_data(as_text=True)
        assert "Other Rd" in html

    def test_status_filter_shows_archived(self, monkeypatch, tmp_path):
        import scraper.server as server
        c = self._client(monkeypatch, tmp_path)
        conn = server.get_conn()
        pid = conn.execute(
            "SELECT id FROM properties WHERE parcel_number='999'").fetchone()["id"]
        assert scraper_db.archive_property(conn, pid, reason="t") is True
        conn.close()
        assert "Other Rd" not in c.get("/properties").get_data(as_text=True)
        html = c.get("/properties?status=archived").get_data(as_text=True)
        assert "Other Rd" in html

    def test_archive_routes_write_audit_and_detail_shows_history(
            self, monkeypatch, tmp_path):
        import scraper.server as server
        c = self._client(monkeypatch, tmp_path)
        conn = server.get_conn()
        pid = conn.execute(
            "SELECT id FROM properties WHERE parcel_number='028 030'").fetchone()["id"]
        conn.close()
        assert c.post(f"/archive/{pid}").status_code == 302
        conn = server.get_conn()
        evs = scraper_db.get_audit_log(conn, pid)
        conn.close()
        assert evs and evs[0]["actor"] == "dashboard"
        html = c.get(f"/property/{pid}").get_data(as_text=True)
        assert "History" in html
        assert "dashboard archive button" in html
        assert c.post(f"/unarchive/{pid}").status_code == 302
