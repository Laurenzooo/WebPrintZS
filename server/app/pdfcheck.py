"""PDF-Prüfung (Magic Bytes, Lesbarkeit, Seitenzahl) und Seitenbereich-Parser."""
from __future__ import annotations

import math
import re
from pathlib import Path

PDF_MIME_TYPES = {"application/pdf", "application/x-pdf"}
_RANGE_RE = re.compile(r"^\d{1,5}(-\d{1,5})?$")


class Rejected(Exception):
    """Fehler, dessen Meldung dem Nutzer angezeigt werden darf."""


def check_magic(head: bytes) -> None:
    # Laut PDF-Spezifikation darf der Header innerhalb der ersten 1024 Bytes stehen.
    if b"%PDF-" not in head[:1024]:
        raise Rejected("Die Datei ist keine gültige PDF-Datei.")


def count_pages(path: Path) -> int:
    from pypdf import PdfReader  # lazy import, spart Startzeit

    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise Rejected("Die PDF-Datei ist passwortgeschützt. Bitte ohne Passwort exportieren.")
        n = len(reader.pages)
    except Rejected:
        raise
    except Exception:
        raise Rejected("Die PDF-Datei konnte nicht gelesen werden (beschädigt?).") from None
    if n < 1:
        raise Rejected("Die PDF-Datei enthält keine Seiten.")
    return n


def parse_page_range(raw: str, total: int) -> tuple[str | None, int]:
    """'1-3, 5' -> ('1-3,5', 4). Leer -> (None, total)."""
    s = raw.strip().replace(" ", "").replace(";", ",")
    if not s:
        return None, total
    parts: list[str] = []
    count = 0
    for p in s.split(","):
        if not p:
            continue
        if not _RANGE_RE.match(p):
            raise Rejected(f"Ungültiger Seitenbereich „{p}“. Beispiel: 1-3,5")
        a_s, _, b_s = p.partition("-")
        a = int(a_s)
        b = int(b_s) if b_s else a
        if a < 1 or b < a:
            raise Rejected(f"Ungültiger Seitenbereich „{p}“.")
        if b > total:
            raise Rejected(f"Seite {b} gibt es nicht – das Dokument hat {total} Seiten.")
        parts.append(str(a) if a == b else f"{a}-{b}")
        count += b - a + 1
    if not parts:
        return None, total
    if len(parts) > 50:
        raise Rejected("Zu viele Einzelbereiche (max. 50).")
    return ",".join(parts), count


def sheets_needed(pages: int, duplex: str, copies: int) -> int:
    per_copy = math.ceil(pages / 2) if duplex != "none" else pages
    return per_copy * copies
