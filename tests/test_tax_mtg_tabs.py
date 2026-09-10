"""Tax / Mtg dashboard tabs: category mapping, mortgage retention tagging,
newspaper mortgage classification, and JSONL raw-notice logging."""
from __future__ import annotations
import json
from datetime import datetime, timezone

from scraper.server import property_category
from scraper.tn_publicnotice import TNPublicNoticeScraper, _tn_parse_parcels
from scraper.nc_publicnotice import _classify_nc_notice
from scraper.newspaper_notices import (
    _classify_newspaper_notice,
    _is_mortgage_notice,
    _is_tax_foreclosure_notice,
)
from scraper.rawlog import log_raw


class TestPropertyCategoryTaxMtg:
    """Dashboard tabs collapsed to Tax + Mtg per state."""

    def test_tax_foreclosure_is_tax(self):
        assert property_category(
            {"source": "tn_publicnotice", "property_type": "tax_foreclosure"}
        ) == "tax"

    def test_explicit_mortgage_type_is_mtg(self):
        assert property_category(
            {"source": "tn_publicnotice", "property_type": "mortgage_foreclosure"}
        ) == "mtg"
        assert property_category(
            {"source": "ga_publicnotice", "property_type": "mortgage_foreclosure"}
        ) == "mtg"

    def test_legacy_mortgage_notes_is_mtg(self):
        assert property_category(
            {"source": "nc_publicnotice", "property_type": "foreclosure",
             "notes": "mortgage"}
        ) == "mtg"
        assert property_category(
            {"source": "nc_publicnotice", "property_type": "foreclosure",
             "notes": "mtg"}
        ) == "mtg"

    def test_notices_collapse_into_tax(self):
        assert property_category(
            {"source": "newspaper_notices", "property_type": "public_notice"}
        ) == "tax"
        assert property_category(
            {"source": "nc_publicnotice", "property_type": "foreclosure",
             "notes": None, "description": "service of process by publication"}
        ) == "tax"


class TestClassifyNcNotice:
    def test_tax_sale(self):
        assert _classify_nc_notice(
            "FORECLOSURE SALE TO SATISFY UNPAID PROPERTY TAXES owing to the County"
        ) == "tax_foreclosure"

    def test_mortgage_sale_kept(self):
        assert _classify_nc_notice(
            "NOTICE OF SUBSTITUTE TRUSTEE'S SALE under that Deed of Trust "
            "for default in payment of the indebtedness"
        ) == "mortgage_foreclosure"

    def test_both_signals_dropped(self):
        assert _classify_nc_notice(
            "tax foreclosure sale by Substitute Trustee under Deed of Trust"
        ) is None

    def test_neither_signal_dropped(self):
        assert _classify_nc_notice(
            "NOTICE OF HOA ASSESSMENT LIEN for unpaid association dues"
        ) is None


class TestMortgageKindTagging:
    STRIPPED = (
        "NOTICEOFSULLIVANCOUNTYDELINQUENTTAXSALEPursuanttotheOrdersomeOwner,"
        "Tamika2442BroadSt020-G/G/023.00County:$2,188.55City:$1,848.64"
        "DB3536/1636Total:$5,519.81"
        "Owner,Brittany495EmmettRd030-L/B/001.20County:$3,000.00"
        "Total:$3,000.00"
    )

    def test_tn_parse_parcels_defaults_to_tax(self):
        parcels = _tn_parse_parcels(self.STRIPPED, "sullivan", "2026-09-02", "http://x")
        assert all(p["property_type"] == "tax_foreclosure" for p in parcels)

    def test_tn_parse_parcels_mortgage_kind(self):
        parcels = _tn_parse_parcels(
            self.STRIPPED, "sullivan", "2026-09-02", "http://x",
            kind="mortgage_foreclosure",
        )
        assert len(parcels) == 2
        assert all(p["property_type"] == "mortgage_foreclosure" for p in parcels)

    def test_ga_parse_acres_rabun_subdivision_lot_is_none(self):
        # Rabun Sky Valley lots are described by lot/plat refs with no acreage
        # in the notice text, and qPublic itself reports Acres 0 for them —
        # the parser must return None (unknown), not misparse plat numbers.
        from scraper.ga_publicnotice import GAPublicNoticeScraper
        block = ("Map & Parcel: 047B048 Defendant in Fi-Fa: Alvarez, Youmia Sergina "
                 "Legal Description: All that tract of land being in the State of "
                 "Georgia, County of Rabun, Being Lot 288, Part 10, of Ridge Pole "
                 "Area Of Sky Valley Subdivision. As shown in Plat Book 16, "
                 "Page 169. Being known as Tax Map & Parcel 047B048, Rabun "
                 "County, Georgia.")
        assert GAPublicNoticeScraper._parse_acres(block) is None

    def test_ga_parse_parcels_mortgage_kind(self):
        from scraper.ga_publicnotice import GAPublicNoticeScraper
        scraper = GAPublicNoticeScraper.__new__(GAPublicNoticeScraper)
        notice = ("File #: 12 Map/Parcel Number: 221 003 Defendant(s) in FiFa: "
                  "Doe, Jane; Tax Map & Parcel 221 003 / 1.5 Acs. Years Due: 2023-2025")
        props = scraper._parse_parcels(
            notice, "towns", "2026-09-01", "http://x",
            kind="mortgage_foreclosure",
        )
        assert props[0]["property_type"] == "mortgage_foreclosure"

    def test_tn_extract_detail_mortgage_fallback_type(self):
        s = TNPublicNoticeScraper.__new__(TNPublicNoticeScraper)
        s._extract_notice_text = lambda page, sid, rec: (
            "NOTICE OF SUBSTITUTE TRUSTEE'S SALE ... deed of trust ..."
        )
        s._is_tax_foreclosure = lambda t: False
        s._is_publication_notice = lambda t: False
        s._extract_acreage = lambda t: None
        rec = {"pk_id": "1", "sp_case": None, "county": "Sullivan"}
        props = s._extract_detail(None, "SID", rec)
        assert len(props) == 1
        assert props[0]["property_type"] == "mortgage_foreclosure"


class TestNewspaperMortgageClassification:
    def test_mortgage_only_is_mortgage(self):
        text = ("NOTICE OF TRUSTEE'S SALE under and by virtue of a Deed of Trust "
                "for default in the payment of the indebtedness secured")
        assert _is_mortgage_notice(text) is True
        assert _is_tax_foreclosure_notice(text) is False
        assert _classify_newspaper_notice(text) == "mortgage_foreclosure"

    def test_strong_tax_with_trustee_is_tax(self):
        text = ("tax foreclosure sale of the described real property by the "
                "Substitute Trustee under NCGS 105")
        assert _is_tax_foreclosure_notice(text) is True
        assert _is_mortgage_notice(text) is False
        assert _classify_newspaper_notice(text) == "public_notice"

    def test_bank_sale_with_lien_mention_is_dropped(self):
        # A bank sale that merely mentions surviving "unpaid taxes" is neither
        # a genuine tax sale nor a mortgage-only notice worth the Mtg tab.
        text = ("NOTICE OF SUBSTITUTE TRUSTEE'S SALE under Deed of Trust, "
                "sale made subject to all prior liens, unpaid taxes of record")
        assert _is_tax_foreclosure_notice(text) is False
        assert _classify_newspaper_notice(text) is None


class TestRawlog:
    def test_log_raw_writes_valid_jsonl(self, tmp_path):
        log_raw(
            "test_source", listing_id="1", county="sullivan", state="TN",
            decision="kept_tax", reason="unit test",
            raw_text="TAX SALE notice body", url="http://x",
            directory=tmp_path,
        )
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        lines = (tmp_path / f"test_source_raw_{day}.jsonl").read_text(
            encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["source"] == "test_source"
        assert rec["decision"] == "kept_tax"
        assert rec["raw_source_text"] == "TAX SALE notice body"
        assert rec["chars"] == len("TAX SALE notice body")

    def test_log_raw_never_raises(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir", encoding="utf-8")
        # directory collides with an existing file -> write fails internally,
        # but log_raw must not raise.
        log_raw("test_source", raw_text="x", directory=blocker)


class _FakeEl:
    def __init__(self):
        self.clicked = False

    def is_visible(self):
        return True

    def click(self, timeout=None):
        self.clicked = True


class _FakeFrame:
    def __init__(self, url, el=None):
        self.url = url
        self._el = el

    def query_selector(self, sel):
        return self._el


class _FakePage:
    """Minimal stand-in for a camoufox page (token/body polling only)."""

    def __init__(self, body_len=100, token="", frames=None):
        self._body_len = body_len
        self._token = token
        self.frames = frames or []
        self.waits = 0

    def evaluate(self, script, *args):
        if "lblMessage" in script:
            return ""
        if "cf-turnstile-response" in script:
            return self._token
        if "innerText" in script:
            return self._body_len
        return ""

    def query_selector(self, sel):
        return None

    def wait_for_timeout(self, ms):
        self.waits += 1


class TestInBrowserTurnstileSolve:
    def _solver(self):
        from scraper.ga_publicnotice import GAPublicNoticeScraper
        return GAPublicNoticeScraper.__new__(GAPublicNoticeScraper)

    def test_body_already_visible_passes(self):
        s = self._solver()
        assert s._solve_turnstile_in_browser(_FakePage(body_len=2000), "key") is True

    def test_widget_token_passes(self):
        s = self._solver()
        assert s._solve_turnstile_in_browser(_FakePage(token="tok123"), "key") is True

    def test_timeout_fails(self):
        s = self._solver()
        assert s._solve_turnstile_in_browser(_FakePage(), "key", timeout_s=0) is False

    def test_checkbox_click_in_challenge_frame(self):
        from scraper.publicnotice_base import PublicNoticeScraper
        el = _FakeEl()
        page = _FakePage(frames=[_FakeFrame("https://challenges.cloudflare.com/turnstile/v0", el)])
        assert PublicNoticeScraper._click_turnstile_checkbox(page) is True
        assert el.clicked is True

    def test_checkbox_click_no_widget(self):
        from scraper.publicnotice_base import PublicNoticeScraper
        assert PublicNoticeScraper._click_turnstile_checkbox(_FakePage()) is False

    def test_gate_submits_after_in_browser_solve(self):
        from scraper.ga_publicnotice import GAPublicNoticeScraper

        class GatePage(_FakePage):
            def __init__(self):
                super().__init__(body_len=100)
                self.posted = False

            def evaluate(self, script, *args):
                if "__doPostBack" in script:
                    self.posted = True
                    self._body_len = 2000
                    return ""
                return super().evaluate(script, *args)

            def query_selector(self, sel):
                return object()  # Turnstile widget present

            def wait_for_load_state(self, *args, **kwargs):
                pass

        s = GAPublicNoticeScraper.__new__(GAPublicNoticeScraper)
        s.solve_captcha = True
        s._solve_turnstile_in_browser = lambda page, key, timeout_s=90: True
        page = GatePage()
        assert s._pass_turnstile_gate(page, "key") is True
        assert page.posted is True
