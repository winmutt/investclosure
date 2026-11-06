"""HTTP access to NC OneMap's ArcGIS REST services.

NC OneMap's service host presents a DigiCert certificate for
``CN=services.gis.nc.gov`` that expired on **2026-11-05 23:59:59Z**; until the
state renews it every request fails verification, which took out the dashboard
parcel layer, the NC ortho basemap, the LiDAR basemap and all NC acreage/parcel
enrichment at once (both ``services.nconemap.gov`` and the newer
``services.gis.nc.gov`` serve the same expired certificate, so the rename is no
help).

NC OneMap is public, read-only reference data and we only ever GET imagery and
JSON from it, so a certificate failure falls back to an unverified connection
instead of losing the whole NC pipeline. Once a certificate error is seen the
unverified path is used directly for ``REPROBE_SECONDS`` (a doomed handshake per
tile otherwise doubles every map request), after which verification is retried
and the fallback drops itself the moment the state renews. Set
``NCONEMAP_TLS_STRICT=1`` to refuse the fallback entirely — an outage then
surfaces as errors, which is what you want if you would rather fail than trust
an unverified host.
"""
import logging
import os
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Marks found in the error text of a TLS verification failure (curl reports
# "(60) SSL certificate problem: certificate has expired").
_CERT_ERROR_MARKERS = ("certificate", "cert verify", "ssl", "self signed")

# How long to trust the "certificate is broken" verdict before trying a verified
# connection again (seconds).
REPROBE_SECONDS = 600.0

_IMPERSONATE = "chrome131"
_local = threading.local()
_state = {"insecure_until": 0.0, "warned": False}


def _strict() -> bool:
    """True when the unverified fallback is disabled by configuration."""
    flag = os.environ.get("NCONEMAP_TLS_STRICT", "").strip().lower()
    return flag in ("1", "true", "yes", "on")


def _is_cert_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _CERT_ERROR_MARKERS)


def _session(verify: bool):
    """Thread-local curl_cffi session (connection reuse, no shared cookies)."""
    attr = "strict" if verify else "insecure"
    session = getattr(_local, attr, None)
    if session is None:
        from curl_cffi import requests as curl_requests
        session = curl_requests.Session(impersonate=_IMPERSONATE, verify=verify)
        setattr(_local, attr, session)
    return session


def _warn(exc: BaseException) -> None:
    if _state["warned"]:
        return
    _state["warned"] = True
    logger.warning(
        "NC OneMap TLS verification failed (%s); using an unverified connection "
        "and re-checking in %d min. This is the 2026-11-05 certificate expiry "
        "workaround — remove it (or set NCONEMAP_TLS_STRICT=1) once the state "
        "renews.", exc, int(REPROBE_SECONDS // 60))


def reset_tls_state() -> None:
    """Forget the certificate verdict (tests, or after an operator fix)."""
    _state["insecure_until"] = 0.0
    _state["warned"] = False
    _local.__dict__.clear()


def request(method: str, url: str, *, timeout: int = 30,
            params: Optional[dict] = None, data: Any = None,
            headers: Optional[dict] = None):
    """curl_cffi request to NC OneMap, falling back to an unverified session."""
    kwargs = dict(timeout=timeout, params=params, data=data, headers=headers)
    method = method.upper()

    if not _strict() and time.monotonic() < _state["insecure_until"]:
        return _session(verify=False).request(method, url, **kwargs)

    try:
        resp = _session(verify=True).request(method, url, **kwargs)
    except Exception as exc:  # noqa: BLE001 - re-raised unless it is a cert error
        if _strict() or not _is_cert_error(exc):
            raise
        _warn(exc)
        _state["insecure_until"] = time.monotonic() + REPROBE_SECONDS
        return _session(verify=False).request(method, url, **kwargs)

    if _state["insecure_until"]:
        # The certificate verifies again: back to normal TLS.
        _state["insecure_until"] = 0.0
        logger.info("NC OneMap TLS verifies again — dropping the unverified fallback.")
    return resp


def get(url: str, **kwargs):
    return request("GET", url, **kwargs)


def post(url: str, **kwargs):
    return request("POST", url, **kwargs)
