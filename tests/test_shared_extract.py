"""Tests for the shared checkpoint (``scraper.jsonl``) and PDF text
(``scraper.pdftext``) modules the long one-off passes were each duplicating.

The point of both modules is that a caller's behaviour survives the move, so
the tests pin the *differences* the four PDF copies and two checkpoint loops
actually had — Blount's 3,000-character OCR floor, Haywood's "OCR only if
longer", the notice family's stripped text, and a checkpoint that resumes on
a truncated last line.
"""
import io
import json
import sys
from unittest.mock import patch

import pytest

from scraper import jsonl, pdftext
from scraper.db import _ensure_db, start_scrape_run


# ---------------------------------------------------------------------------
# scraper/jsonl.py
# ---------------------------------------------------------------------------

class TestLoadDone:
    def test_missing_file_is_empty(self, tmp_path):
        assert jsonl.load_done(tmp_path / "nope.jsonl") == {}

    def test_last_record_per_id_wins(self, tmp_path):
        path = tmp_path / "cp.jsonl"
        jsonl.append_record(path, {"id": 1, "status": "ok", "verdict": "old"})
        jsonl.append_record(path, {"id": 1, "status": "ok", "verdict": "new"})
        jsonl.append_record(path, {"id": 2, "status": "failed"})
        done = jsonl.load_done(path)
        assert set(done) == {1, 2}
        assert done[1]["verdict"] == "new"

    def test_truncated_last_line_does_not_abort_resume(self, tmp_path):
        # A pass killed mid-write leaves half a JSON object at EOF; hours of
        # finished work still have to load.
        path = tmp_path / "cp.jsonl"
        jsonl.append_record(path, {"id": 7, "status": "ok"})
        with open(path, "a", encoding="utf-8") as fh:
            fh.write('{"id": 8, "stat')
        assert set(jsonl.load_done(path)) == {7}

    def test_record_without_the_key_is_skipped(self, tmp_path):
        path = tmp_path / "cp.jsonl"
        jsonl.append_record(path, {"status": "ok"})
        jsonl.append_record(path, {"id": 3, "status": "ok"})
        assert set(jsonl.load_done(path)) == {3}

    def test_string_ids_normalise_to_int(self, tmp_path):
        # The harvest wrote int ids, the HOA sweep coerced strings; both now
        # key the same way so `row["id"] not in done` keeps working.
        path = tmp_path / "cp.jsonl"
        jsonl.append_record(path, {"id": "12"})
        assert set(jsonl.load_done(path)) == {12}

    def test_custom_key(self, tmp_path):
        path = tmp_path / "cp.jsonl"
        jsonl.append_record(path, {"id": 1, "zpid": "4001"})
        assert "4001" not in jsonl.load_done(path)
        assert list(jsonl.load_done(path, key="zpid").values())[0]["id"] == 1


class TestAppendRecord:
    def test_creates_file_and_round_trips_unicode(self, tmp_path):
        path = tmp_path / "sub" / "cp.jsonl"
        path.parent.mkdir()
        jsonl.append_record(path, {"id": 1, "address": "17 Smoky Ridge Rd"})
        jsonl.append_record(path, {"id": 2, "notes": "ridgeline — no roof"})
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[1])["notes"] == "ridgeline — no roof"

    def test_write_records_batches(self, tmp_path):
        path = tmp_path / "cp.jsonl"
        jsonl.write_records(path, [{"id": i} for i in range(3)])
        assert set(jsonl.load_done(path)) == {0, 1, 2}


class TestWaitForSource:
    @pytest.fixture
    def conn(self, tmp_path):
        return _ensure_db(tmp_path / "test.db")

    def test_false_when_nothing_running(self, conn):
        assert jsonl.sweep_running(conn, "zillow") is False

    def test_true_while_the_named_source_runs(self, conn):
        start_scrape_run(conn, "zillow")
        assert jsonl.sweep_running(conn, "zillow") is True
        assert jsonl.sweep_running(conn, "kania_law") is False

    def test_returns_immediately_when_idle(self, conn):
        assert jsonl.wait_for_source(conn, "zillow",
                                     poll_seconds=1) is None

    def test_a_stale_run_does_not_wedge_the_wait(self, conn):
        # The 2026-11-08 failure: a killed Zillow run stayed 'running' and
        # froze the HOA rejudge for two days.
        stale = start_scrape_run(conn, "zillow")
        conn.execute("UPDATE scrape_runs SET started_at = datetime('now',"
                     " '-6 hours') WHERE id=?", (stale,))
        conn.commit()
        assert jsonl.wait_for_source(conn, "zillow", poll_seconds=1) is None
        assert conn.execute("SELECT status FROM scrape_runs WHERE id=?",
                            (stale,)).fetchone()[0] == "failed"


class TestSweepCallersAgree:
    """The harvest's wrappers and the sweep must read the same primitives."""

    def test_harvest_wrappers_delegate(self, tmp_path, monkeypatch):
        from scraper import nc_building_harvest as harvest
        cp = tmp_path / "harvest.jsonl"
        monkeypatch.setattr(harvest, "HARVEST_JSONL", str(cp))
        harvest.append_record({"id": 5, "status": "ok"})
        assert set(harvest.load_done()) == {5}
        assert harvest.load_done()[5]["status"] == "ok"

    def test_sweep_wrappers_delegate(self, tmp_path, monkeypatch):
        from scraper import zillow_hoa_sweep as sweep
        cp = tmp_path / "sweep.jsonl"
        monkeypatch.setattr(sweep, "SWEEP_JSONL", str(cp))
        sweep.append_record({"id": "9", "status": "ok"})
        assert set(sweep.load_done()) == {9}


# ---------------------------------------------------------------------------
# scraper/pdftext.py
# ---------------------------------------------------------------------------

pytest.importorskip("pdfplumber")


class _FakePage:
    def __init__(self, text):
        self._text = text

    def extract_text(self):
        return self._text


def _fake_pdf(pages):
    """Patch pdfplumber.open so the text layer is whatever we decide."""
    cm = patch("pdfplumber.open")
    started = cm.start()
    started.return_value.__enter__.return_value.pages = [_FakePage(p)
                                                         for p in pages]
    return cm


class TestPdfTextLayer:
    def teardown_method(self):
        patch.stopall()

    def test_text_layer_is_used_verbatim(self):
        cm = _fake_pdf(["  NOTICE OF SALE\non the following parcel  "])
        try:
            assert pdftext.pdf_text(b"%PDF-1.4 body", label="T") == \
                "  NOTICE OF SALE\non the following parcel  "
        finally:
            cm.stop()

    def test_strip_flag_matches_the_notice_family(self):
        cm = _fake_pdf(["\n  NOTICE OF SALE  \n"])
        try:
            assert pdftext.pdf_text(b"%PDF", label="T") == \
                "\n  NOTICE OF SALE  \n"
            assert pdftext.pdf_text(b"%PDF", strip=True, label="T") == \
                "NOTICE OF SALE"
        finally:
            cm.stop()

    def test_non_pdf_returns_none(self):
        # A Cloudflare challenge page saved under a .pdf name.
        assert pdftext.pdf_text(b"<html>Just a moment</html>", label="T") \
            is None
        assert pdftext.pdf_text(b"", label="T") is None

    def test_missing_pdfplumber_is_loud_and_returns_none(self, capsys):
        # The 2026-09-11 incident: an image without pdfplumber truncated
        # every notice to on-page text and nothing reported it.
        saved = sys.modules.get("pdfplumber")
        sys.modules["pdfplumber"] = None
        try:
            assert pdftext.pdf_text(b"%PDF-1.4", label="Notice") is None
        finally:
            if saved is not None:
                sys.modules["pdfplumber"] = saved
            else:
                del sys.modules["pdfplumber"]
        assert "pdfplumber missing" in capsys.readouterr().err

    def test_extract_error_falls_through_to_ocr(self):
        with patch("pdfplumber.open", side_effect=Exception("corrupt xref")):
            with patch("scraper.pdftext.ocr_pdf_text",
                       return_value="OCR RECOVERY") as ocr:
                assert pdftext.pdf_text(b"%PDF", label="T") == "OCR RECOVERY"
            ocr.assert_called_once()


class TestPdfTextThresholds:
    """Each county's floor for trusting a text layer, pinned per caller."""

    def teardown_method(self):
        patch.stopall()

    def test_notice_path_ocrs_only_an_empty_layer(self):
        cm = _fake_pdf([""])
        try:
            with patch("scraper.pdftext.ocr_pdf_text",
                       return_value="  RECOVERED  ") as ocr:
                assert pdftext.pdf_text(b"%PDF", strip=True, label="Notice") \
                    == "RECOVERED"
            ocr.assert_called_once()
        finally:
            cm.stop()
        cm = _fake_pdf(["Tax List 2026"])
        try:
            with patch("scraper.pdftext.ocr_pdf_text") as ocr:
                pdftext.pdf_text(b"%PDF", strip=True, label="Notice")
            ocr.assert_not_called()
        finally:
            cm.stop()

    def test_haywood_floor_runs_ocr_on_a_thin_layer(self):
        thin = "x" * 199
        cm = _fake_pdf([thin])
        try:
            with patch("scraper.pdftext.ocr_pdf_text") as ocr:
                pdftext.pdf_text(b"%PDF", min_text_len=200, label="T")
            ocr.assert_called_once()
            # A 199-char layer with nothing readable under it is "no notice",
            # not a 199-character notice. (Haywood used to return the footer;
            # its parser found nothing in it either way.)
            with patch("scraper.pdftext.ocr_pdf_text", return_value=None):
                assert pdftext.pdf_text(b"%PDF", min_text_len=200,
                                        prefer_longer=True, label="T") is None
        finally:
            cm.stop()

    def test_prefer_longer_keeps_a_winning_text_layer(self):
        thin = "notice text"          # 11 chars
        cm = _fake_pdf([thin])
        try:
            with patch("scraper.pdftext.ocr_pdf_text", return_value="ocr"):
                assert pdftext.pdf_text(b"%PDF", min_text_len=200,
                                        prefer_longer=True, label="T") == thin
            with patch("scraper.pdftext.ocr_pdf_text",
                       return_value="o" * 40):
                assert pdftext.pdf_text(b"%PDF", min_text_len=200,
                                        prefer_longer=True, label="T") == \
                    "o" * 40
        finally:
            cm.stop()

    def test_blount_settings_reach_the_ocr_call(self):
        # ocr_pdf_text is what enforces the 3,000-character floor (it returns
        # None when no render clears it); pdf_text must hand it those exact
        # settings and then report "no notice" rather than the footer.
        cm = _fake_pdf(["digital footer"])
        try:
            with patch("scraper.pdftext.ocr_pdf_text",
                       return_value=None) as ocr:
                assert pdftext.pdf_text(b"%PDF", min_text_len=501,
                                        ocr_dpi=(200, 300), psm=6,
                                        min_ocr_len=3000, label="Blount") \
                    is None
            kwargs = ocr.call_args.kwargs
            assert kwargs["dpi"] == (200, 300) and kwargs["psm"] == 6
            assert kwargs["min_chars"] == 3000
        finally:
            cm.stop()

    def test_blount_accepts_a_full_render(self):
        cm = _fake_pdf(["digital footer"])
        try:
            with patch("scraper.pdftext.ocr_pdf_text", return_value="y" *
                       5000):
                assert pdftext.pdf_text(b"%PDF", min_text_len=501,
                                        min_ocr_len=3000, label="Blount") == \
                    "y" * 5000
        finally:
            cm.stop()


class TestOcrAvailability:
    def test_missing_tools_report_once(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pdftext.shutil, "which", lambda _exe: None)
        assert pdftext.ocr_pdf_text(b"%PDF", label="Blount") is None

    def test_non_pdf_never_renders(self, monkeypatch):
        monkeypatch.setattr(pdftext.shutil, "which", lambda _exe: "/usr/bin/x")
        with patch("scraper.pdftext.subprocess.run") as run:
            assert pdftext.ocr_pdf_text(b"<html>", label="T") is None
        run.assert_not_called()


class TestOcrRealRender:
    """End-to-end OCR: an image-only PDF must read back through the shared
    reader with the same settings the notice family used."""

    def test_scanned_notice_reads_back(self):
        Image = pytest.importorskip("PIL.Image")
        ImageDraw = pytest.importorskip("PIL.ImageDraw")
        import shutil as _shutil
        if not (_shutil.which("tesseract") and _shutil.which("pdftoppm")):
            pytest.skip("pdftoppm/tesseract required for OCR")

        img = Image.new("RGB", (1500, 420), "white")
        d = ImageDraw.Draw(img)
        d.text((60, 60), "NOTICE OF SUBSTITUTE TRUSTEE SALE", fill="black")
        d.text((60, 170), "Property Address: 501 Oak Ridge Turnpike",
               fill="black")
        buf = io.BytesIO()
        img.save(buf, format="PDF")

        text = pdftext.pdf_text(buf.getvalue(), strip=True, label="Notice")
        assert text and "OAK RIDGE TURNPIKE" in text.upper()

    def test_min_chars_floor_rejects_a_tiny_render(self):
        Image = pytest.importorskip("PIL.Image")
        ImageDraw = pytest.importorskip("PIL.ImageDraw")
        import shutil as _shutil
        if not (_shutil.which("tesseract") and _shutil.which("pdftoppm")):
            pytest.skip("pdftoppm/tesseract required for OCR")

        img = Image.new("RGB", (900, 200), "white")
        ImageDraw.Draw(img).text((40, 60), "SHORT", fill="black")
        buf = io.BytesIO()
        img.save(buf, format="PDF")
        assert pdftext.ocr_pdf_text(buf.getvalue(), min_chars=3000,
                                    label="Blount") is None
        # ...and through the caller, a page-fragment scan is no tax list at
        # all — not a tax list with zero rows on it.
        assert pdftext.pdf_text(buf.getvalue(), min_text_len=501,
                                ocr_dpi=(200, 300), psm=6, min_ocr_len=3000,
                                label="Blount") is None
