"""Telegram notification module for investclosure foreclosure alerts.

Copied from realestate/scraper/telegrams.py and adapted for foreclosure data.
Uses the Telegram Bot API to send push notifications.

Configuration via environment variables (also loaded from .env / docker-compose):
    TELEGRAM_BOT_TOKEN  — Bot token from @BotFather
    TELEGRAM_CHAT_ID    — Your chat/channel ID
    TELEGRAM_ENABLED    — Set to "true" to enable (default "false")

Keys reused from realestate:
    TWO_CAPTCHA_API_KEY    — already in investclosure .env (4ab46...)
    GOOGLE_MAPS_API_KEY    — stored for future elevation/GMaps enrichment
"""
from __future__ import annotations
import os
import logging
import time
from typing import Optional

import requests
from requests.exceptions import RequestException

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
MAX_RETRIES = 3
RETRY_DELAY = 2


def escape_markdown(text: str) -> str:
    special_chars = r'\_*[]()~`>#+-=|{}.!'
    for c in special_chars:
        text = text.replace(c, '\\' + c)
    return text


class TelegramNotifier:
    """Send push notifications via Telegram Bot API."""

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        enabled: Optional[bool] = None,
    ):
        # enabled can be forced via arg, otherwise env
        if enabled is not None:
            self.enabled = enabled
        else:
            self.enabled = os.environ.get("TELEGRAM_ENABLED", "false").lower() == "true"
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
        self._session = requests.Session()

    def _send(self, message: str, parse_mode: str = "HTML") -> bool:
        if not self.enabled:
            logger.debug("Telegram disabled (TELEGRAM_ENABLED != true)")
            return False
        if not self.bot_token or not self.chat_id:
            logger.warning("Telegram notification disabled: missing TOKEN or CHAT_ID")
            return False
        url = f"{TELEGRAM_API}/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": message, "parse_mode": parse_mode}
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self._session.post(url, json=payload, timeout=10)
                if resp.status_code == 200:
                    return True
                else:
                    logger.warning(f"Telegram API returned {resp.status_code}: {resp.text[:500]}")
            except RequestException as e:
                logger.warning(f"Telegram request failed (attempt {attempt}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_DELAY * attempt)
            except Exception as e:
                logger.warning(f"Unexpected error sending Telegram message: {e}")
                break
        return False

    def send_new_properties(
        self,
        properties: list[dict],
        source: str = "unknown",
    ) -> bool:
        """Send notification about new foreclosure properties.

        investclosure properties use: address, county, state, acres, parcel_number,
        auction_date, initial_auction_date, gis_url, google_maps_url, price_cents, description.
        Supports both scraper PropertyData dicts and DB sqlite3.Row dicts.
        """
        if not properties:
            return self._send("✅ Scrape completed — no new properties found.", parse_mode="HTML")

        # Top-level header
        lines = [
            "<b>🏠 New Foreclosure!</b>",
            "",
            f"Source: <code>{source}</code>",
            f"Count: {len(properties)}",
            "",
        ]
        for i, prop in enumerate(properties[:10], 1):
            addr = prop.get("address") or prop.get("description") or "?"
            county = prop.get("county") or "?"
            state = prop.get("state") or ""
            acres = prop.get("acres")
            parcel = prop.get("parcel_number") or prop.get("extracted_pin") or ""
            auction = prop.get("initial_auction_date") or prop.get("auction_date") or ""
            price_cents = prop.get("price_cents")
            google = prop.get("google_maps_url") or ""
            gis = prop.get("gis_url") or ""
            url = prop.get("url") or ""

            # Title line: address + county
            county_state = f"{county}, {state}" if state else county
            lines.append(f"{i}. <b>{addr}</b>")
            lines.append(f"   <code>{county_state}</code>")

            # Acres / parcel / auction
            details = []
            if acres:
                try:
                    details.append(f"{float(acres):.2f}ac")
                except:
                    details.append(f"{acres}ac")
            if parcel:
                details.append(f"Parcel: <code>{parcel}</code>")
            if auction:
                details.append(f"Sale: {auction}")
            if price_cents:
                try:
                    dollars = int(price_cents) / 100
                    if dollars > 0:
                        details.append(f"${dollars:,.0f}")
                except:
                    pass
            if details:
                lines.append(f"   • {' | '.join(details)}")

            # Links (HTML) - telegram caps at one preview, so we send as links
            link_parts = []
            if google:
                link_parts.append(f'<a href="{google}">Maps</a>')
            if gis:
                link_parts.append(f'<a href="{gis}">GIS</a>')
            if url and url != google and url != gis:
                # listing source url
                link_parts.append(f'<a href="{url}">Listing</a>')
            if link_parts:
                lines.append(f"   • {' | '.join(link_parts)}")
            lines.append("")

        if len(properties) > 10:
            lines += [f"... and {len(properties) - 10} more (see dashboard)"]

        message = "\n".join(lines)
        # Telegram limit 4096 chars — truncate if needed
        if len(message) > 4000:
            message = message[:4000] + "\n… truncated"
        return self._send(message, parse_mode="HTML")

    def send_new_property_single(self, prop: dict, source: str = "unknown") -> bool:
        """Send notification for a single new property (immediate mode)."""
        return self.send_new_properties([prop], source=source)

    def send_scrape_summary(self, results: list[dict]) -> bool:
        lines = ["📊 <b>Scrape Run Summary</b>", ""]
        total_found = 0
        total_new = 0
        total_dups = 0
        errors = []
        for r in results:
            scraper = r.get("scraper", "unknown")
            found = r.get("found", 0)
            new = r.get("new", 0)
            dups = r.get("duplicates", 0)
            error = r.get("error")
            total_found += found
            total_new += new
            total_dups += dups
            if error:
                errors.append(f"❌ {scraper}: {error}")
                lines.append(f"❌ {scraper} — FAILED: {error[:80]}")
            else:
                lines.append(f"✅ {scraper} — Found: {found} | New: {new} | Dups: {dups}")
        lines += ["", f"Total Found: {total_found}", f"Total New: {total_new}", f"Total Dups: {total_dups}"]
        if errors:
            lines += ["", "<b>Errors:</b>"] + errors
        message = "\n".join(lines)
        return self._send(message, parse_mode="HTML")

    def send_health_alert(self, message: str, details: Optional[str] = None) -> bool:
        lines = ["🚨 <b>Health Alert</b>", "", message]
        if details:
            lines += ["", details]
        return self._send("\n".join(lines), parse_mode="HTML")

    def test_connection(self) -> bool:
        return self._send("✅ investclosure Telegram notifications working!")


def send_notification(message: str, parse_mode: str = "HTML") -> bool:
    notifier = TelegramNotifier()
    return notifier._send(message, parse_mode)
