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



class TestBulkArchive:
    """Dashboard checkbox multi-archive POST /bulk-archive."""

    def _client(self, monkeypatch, tmp_path):
        import base64
        import scraper.server as server
        db_path = tmp_path / "bulk.db"

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
        rows = {}
        for lid in ("A1", "A2", "A3"):
            _insert(conn, source_listing_id=lid, parcel_number=lid,
                    address=f"{lid} Road", acres=5.0)
        conn.close()
        monkeypatch.setattr(server, "get_conn", _conn)
        return server.app.test_client(), _conn

    def _status(self, _conn_conn_source, lid):
        conn = _conn_conn_source()
        row = conn.execute(
            "SELECT status FROM properties WHERE source_listing_id=?",
            (lid,)).fetchone()
        conn.close()
        return row["status"]

    def test_bulk_archives_multiple_actives(self, monkeypatch, tmp_path):
        c, conn_src = self._client(monkeypatch, tmp_path)
        conn = conn_src()
        ids = {r["source_listing_id"]: r["id"] for r in conn.execute(
            "SELECT id, source_listing_id FROM properties").fetchall()}
        conn.close()
        resp = c.post("/bulk-archive",
                      data={"ids": [ids["A1"], ids["A2"]]},
                      follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/"  # default landing redirect
        assert self._status(conn_src, "A1") == "archived"
        assert self._status(conn_src, "A2") == "archived"
        assert self._status(conn_src, "A3") == "active"

    def test_bulk_reason_and_actor_audited(self, monkeypatch, tmp_path):
        c, conn_src = self._client(monkeypatch, tmp_path)
        conn = conn_src()
        pid = conn.execute(
            "SELECT id FROM properties WHERE source_listing_id='A1'"
        ).fetchone()["id"]
        conn.close()
        c.post("/bulk-archive", data={
            "ids": [pid], "reason": "wrong county"})
        conn = conn_src()
        ev = scraper_db.get_audit_log(conn, pid)[-1]
        assert (ev["action"], ev["actor"]) == ("archive", "dashboard")
        assert ev["reason"] == "wrong county"
        conn.close()

    def test_bulk_default_reason_without_input(self, monkeypatch, tmp_path):
        c, conn_src = self._client(monkeypatch, tmp_path)
        conn = conn_src()
        pid = conn.execute(
            "SELECT id FROM properties WHERE source_listing_id='A1'"
        ).fetchone()["id"]
        conn.close()
        c.post("/bulk-archive", data={"ids": [pid], "reason": "   "})
        conn = conn_src()
        assert scraper_db.get_audit_log(conn, pid)[-1]["reason"] == \
            "dashboard bulk archive"
        conn.close()

    def test_bulk_skips_archived_and_missing(self, monkeypatch, tmp_path):
        c, conn_src = self._client(monkeypatch, tmp_path)
        conn = conn_src()
        rows = {r["source_listing_id"]: r["id"] for r in conn.execute(
            "SELECT id, source_listing_id FROM properties").fetchall()}
        conn.close()
        c.post("/bulk-archive",
               data={"ids": [rows["A1"], "9999"]})
        assert self._status(conn_src, "A1") == "archived"

    def test_bulk_rejects_non_integer_id(self, monkeypatch, tmp_path):
        c, conn_src = self._client(monkeypatch, tmp_path)
        resp = c.post("/bulk-archive", data={"ids": ["not-an-int"]})
        assert resp.status_code == 400
        assert self._status(conn_src, "A1") == "active"

    def test_return_to_local_redirect(self, monkeypatch, tmp_path):
        c, conn_src = self._client(monkeypatch, tmp_path)
        conn = conn_src()
        pid = conn.execute(
            "SELECT id FROM properties WHERE source_listing_id='A1'"
        ).fetchone()["id"]
        conn.close()
        resp = c.post("/bulk-archive", data={
            "ids": [pid], "return_to": "/properties?q=Camp"})
        assert resp.status_code == 302
        assert resp.headers["Location"] == "/properties?q=Camp"

    def test_return_to_open_redirect_rejected(self, monkeypatch, tmp_path):
        c, conn_src = self._client(monkeypatch, tmp_path)
        conn = conn_src()
        pid = conn.execute(
            "SELECT id FROM properties WHERE source_listing_id='A1'"
        ).fetchone()["id"]
        conn.close()
        for evil in ("//evil.example.com", "http://evil.example.com"):
            resp = c.post("/bulk-archive", data={
                "ids": [pid], "return_to": evil})
            assert resp.headers["Location"] == "/"

    def test_bulk_id_cap_500(self, monkeypatch, tmp_path):
        c, conn_src = self._client(monkeypatch, tmp_path)
        conn = conn_src()
        ids = {r["source_listing_id"]: r["id"] for r in conn.execute(
            "SELECT id, source_listing_id FROM properties").fetchall()}
        conn.close()
        # real ids sent LAST so the cap truncates them away
        payload = [str(900000 + i) for i in range(509)] + \
            [str(ids["A1"]), str(ids["A2"])]
        c.post("/bulk-archive", data={"ids": payload})
        assert self._status(conn_src, "A1") == "active"
        assert self._status(conn_src, "A2") == "active"

    def test_property_views_render_bulk_form(self, monkeypatch, tmp_path):
        c, _ = self._client(monkeypatch, tmp_path)
        html = c.get("/properties").get_data(as_text=True)
        assert 'name="ids"' in html
        assert "data-bulk-form" in html
        assert "data-bulk-select-all" in html

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
