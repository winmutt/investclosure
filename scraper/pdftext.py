"""One PDF text extractor for every notice the scrapers read.

Four modules needed the same thing — "give me the text of this notice PDF,
and if it is a scan, OCR it" — and each had written its own: the public
notice family (``publicnotice_base``), Haywood's CivicPlus bid notices,
Blount's scanned delinquent-tax list, and Ashe's sale list. They agreed on
the shape (pdfplumber first, ``pdftoppm`` + ``tesseract`` when there is no
text layer) and differed only in how much text counts as enough and which
OCR settings suit that county's scan. Those are the parameters below; the
machinery exists once.

Why the OCR fallback matters: some county PDFs carry no text layer at all
(Grainger's ImageMagick-"smashed" 300 dpi scans), so a text-layer-only read
returns nothing and the row silently keeps the truncated on-page text
instead of the full notice.

A missing ``pdfplumber`` is a loud error, not a quiet one: on 2026-09-11 an
image built without it truncated every TN notice to 1,000 characters and
nothing in the run reported a failure.
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Iterable, List, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)

__all__ = ["pdf_text", "ocr_pdf_text"]

# Generous ceilings: these are scanned multi-page legal notices on a
# scraper's own clock, and a wrong guess kills a notice, not a process.
RENDER_TIMEOUT_S = 1800
TESSERACT_TIMEOUT_S = 300


def _run(cmd: Sequence[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), capture_output=True, text=True,
                          timeout=timeout)


def ocr_pdf_text(data: bytes, *, dpi: Union[int, Iterable[int]] = 300,
                 psm: int = 4, min_chars: int = 0,
                 label: str = "PDF") -> Optional[str]:
    """OCR a scanned PDF: render each page with ``pdftoppm``, read with
    ``tesseract``. Returns None when OCR is unavailable or fails.

    ``dpi`` may be a sequence: each setting is tried in turn and the first
    render that yields at least ``min_chars`` of text wins. Blount uses
    ``(200, 300)`` — its large type reads at 200 dpi in a third of the time,
    and small print escalates to 300.
    """
    if not data or data[:4] != b"%PDF":
        return None
    if not (shutil.which("pdftoppm") and shutil.which("tesseract")):
        logger.error("%s OCR unavailable (pdftoppm/tesseract missing)", label)
        return None
    sizes: Tuple[int, ...] = ((dpi,) if isinstance(dpi, int)
                              else tuple(dpi))
    try:
        with tempfile.TemporaryDirectory(prefix="pdf_ocr_") as tmpdir:
            pdf_path = os.path.join(tmpdir, "notice.pdf")
            with open(pdf_path, "wb") as fh:
                fh.write(data)
            for size in sizes:
                prefix = os.path.join(tmpdir, f"pg{size}")
                _run(["pdftoppm", "-r", str(size), "-png", pdf_path, prefix],
                     RENDER_TIMEOUT_S)
                texts: List[str] = []
                for png in sorted(os.listdir(tmpdir)):
                    if not png.startswith(f"pg{size}") or not \
                            png.endswith(".png"):
                        continue
                    r = _run(["tesseract", os.path.join(tmpdir, png),
                              "stdout", "--psm", str(psm)],
                             TESSERACT_TIMEOUT_S)
                    texts.append(r.stdout or "")
                joined = "\n".join(t for t in texts if t).strip()
                if joined and len(joined) >= min_chars:
                    return joined
                logger.warning("%s OCR @ %ddpi produced too little text "
                               "(%d chars)", label, size, len(joined))
        return None
    except FileNotFoundError:
        logger.warning("%s OCR unavailable (pdftoppm/tesseract missing)",
                       label)
        return None
    except Exception as e:
        logger.warning("%s OCR failed: %s", label, e)
        return None


def pdf_text(data: bytes, *, min_text_len: int = 1, ocr: bool = True,
             ocr_dpi: Union[int, Iterable[int]] = 300, psm: int = 4,
             min_ocr_len: int = 0, prefer_longer: bool = False,
             strip: bool = False, label: str = "PDF") -> Optional[str]:
    """Text of a PDF: the embedded text layer when it is good enough, else OCR.

    ``min_text_len`` is how long the embedded text must be to be taken as
    the notice. A scanned page yields "" here, which is why the default is
    1; counties whose PDFs mix a thin digital header with an image body pass
    a real floor (Haywood 200, Blount 501) so the OCR pass still runs.
    ``min_ocr_len`` is the mirror for the OCR result, applied by
    :func:`ocr_pdf_text` — Blount demands 3,000 characters of a 30-page tax
    list before it trusts the render. With ``prefer_longer`` the two partial
    reads are compared and the longer one wins (Haywood's rule: its notices
    sometimes carry a real paragraph above a picture).

    When neither read clears its floor the result is None, not the leftovers:
    a caller that asked for a notice and got a footer would report the notice
    as containing nothing. ``ocr=False`` skips the fallback and returns the
    text layer as-is. ``strip`` drops the PDF's outer whitespace (the
    public-notice family stores its raw source stripped and its regexes were
    tuned on that).
    """
    if not data or data[:4] != b"%PDF":
        if data:
            logger.debug("%s is not a PDF (%d bytes) — skipped", label,
                         len(data))
        return None
    try:
        import pdfplumber
    except ImportError:
        # Loud on purpose — see the module docstring's 2026-09-11 incident.
        # requirements.txt pins pdfplumber; rebuild the image if this fires.
        logger.error("%s: pdfplumber not installed; cannot parse PDF", label)
        print(f"ERROR: pdfplumber missing — {label} text unavailable",
              file=sys.stderr, flush=True)
        return None

    text = ""
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception as e:
        logger.warning("%s PDF text extraction failed: %s", label, e)

    def done(value: str) -> Optional[str]:
        out = value.strip() if strip else value
        return out or None

    stripped = text.strip()
    if len(stripped) >= min_text_len:
        return done(text)
    if not ocr:
        return done(text)
    if stripped:
        logger.info("%s PDF text layer too thin (%d chars); OCR fallback",
                    label, len(stripped))
    else:
        logger.info("%s PDF has no text layer; OCR fallback", label)
    scanned = ocr_pdf_text(data, dpi=ocr_dpi, psm=psm, min_chars=min_ocr_len,
                           label=label)
    if not scanned:
        # Nothing cleared the floor, so this is not the document — handing a
        # caller its footer is how a stub list gets parsed as an empty one.
        return None
    if prefer_longer and len(stripped) >= len(scanned.strip()):
        return done(text)
    return done(scanned)
