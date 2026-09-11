"""HTTP Basic Auth: bootstrap, gate, user admin, password changes."""
from __future__ import annotations
import base64


def _client(monkeypatch, tmp_path):
    import scraper.server as server
    from scraper import db as scraper_db
    db_path = tmp_path / "auth.db"

    def _conn():
        return scraper_db._ensure_db(db_path)

    monkeypatch.setattr(server, "get_conn", _conn)
    return server.app.test_client()


def _hdr(user, pw):
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


ADMIN = _hdr("winmutt", "1234asdf")


class TestBasicAuth:
    def test_bootstrap_and_login(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        r = c.get("/")
        assert r.status_code == 401
        assert "Basic" in r.headers.get("WWW-Authenticate", "")
        assert c.get("/", headers=ADMIN).status_code == 200

    def test_wrong_password_rejected(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        assert c.get("/").status_code == 401  # seeds default user
        assert c.get("/", headers=_hdr("winmutt", "nope")).status_code == 401
        assert c.get("/", headers=_hdr("nobody", "1234asdf")).status_code == 401

    def test_health_stays_public(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        assert c.get("/health").status_code == 200


class TestUserAdmin:
    def test_add_user_then_login(self, monkeypatch, tmp_path):
        import scraper.server as server
        c = _client(monkeypatch, tmp_path)
        assert c.get("/", headers=ADMIN).status_code == 200
        r = c.post("/admin/add-user",
                   data={"username": "bob", "password": "bobspass"},
                   headers=ADMIN)
        assert r.status_code == 302
        assert c.get("/", headers=_hdr("bob", "bobspass")).status_code == 200

    def test_add_duplicate_rejected(self, monkeypatch, tmp_path):
        import scraper.server as server
        from scraper import db as scraper_db
        c = _client(monkeypatch, tmp_path)
        c.get("/", headers=ADMIN)
        c.post("/admin/add-user", data={"username": "bob", "password": "bobspass"},
               headers=ADMIN)
        c.post("/admin/add-user", data={"username": "bob", "password": "other"},
               headers=ADMIN)
        conn = server.get_conn()
        try:
            assert scraper_db.count_users(conn) == 2
        finally:
            conn.close()

    def test_change_own_password_needs_current(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        assert c.get("/", headers=ADMIN).status_code == 200
        c.post("/admin/change-password",
               data={"username": "winmutt", "current_password": "wrong",
                     "new_password": "newpass1"},
               headers=ADMIN)
        assert c.get("/", headers=ADMIN).status_code == 200  # unchanged
        c.post("/admin/change-password",
               data={"username": "winmutt", "current_password": "1234asdf",
                     "new_password": "newpass1"},
               headers=ADMIN)
        assert c.get("/", headers=ADMIN).status_code == 401  # old dead
        assert c.get("/", headers=_hdr("winmutt", "newpass1")).status_code == 200

    def test_admin_resets_other_user(self, monkeypatch, tmp_path):
        c = _client(monkeypatch, tmp_path)
        assert c.get("/", headers=ADMIN).status_code == 200
        c.post("/admin/add-user", data={"username": "bob", "password": "bobspass"},
               headers=ADMIN)
        c.post("/admin/change-password",
               data={"username": "bob", "new_password": "resetpw"},
               headers=ADMIN)
        assert c.get("/", headers=_hdr("bob", "resetpw")).status_code == 200
        assert c.get("/", headers=_hdr("bob", "bobspass")).status_code == 401


class TestUserHelpers:
    def test_create_duplicate_raises(self, tmp_path):
        from scraper import db as scraper_db
        conn = scraper_db._ensure_db(tmp_path / "u.db")
        scraper_db.create_user(conn, "ann", "annspass")
        try:
            scraper_db.create_user(conn, "ann", "other")
            raise SystemExit("should have raised")
        except ValueError:
            pass
        assert scraper_db.verify_user(conn, "ann", "annspass") is True
        assert scraper_db.verify_user(conn, "ann", "wrong") is False
        assert scraper_db.set_password(conn, "ghost", "newpass1") is False
        conn.close()
