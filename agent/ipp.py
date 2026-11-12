"""Minimaler IPP/2.0-Client (nur Standardbibliothek).

Unterstützt: Print-Job, Get-Job-Attributes, Get-Printer-Attributes.
Reicht für IPP-Everywhere/AirPrint-Drucker wie den HP OfficeJet Pro 7740.
"""
from __future__ import annotations

import http.client
import itertools
import ssl
import struct
import time
import urllib.parse
from pathlib import Path

# Operationen
PRINT_JOB = 0x0002
GET_JOB_ATTRIBUTES = 0x0009
GET_PRINTER_ATTRIBUTES = 0x000B

# Gruppen-Tags
TAG_OPERATION, TAG_JOB, TAG_END, TAG_PRINTER = 0x01, 0x02, 0x03, 0x04

# Wert-Tags
INTEGER, BOOLEAN, ENUM = 0x21, 0x22, 0x23
RANGE = 0x33
TEXT, NAME, KEYWORD, URI, CHARSET, LANGUAGE, MIME = 0x41, 0x42, 0x44, 0x45, 0x47, 0x48, 0x49
BEG_COLLECTION, END_COLLECTION, MEMBER_NAME = 0x34, 0x37, 0x4A

JOB_STATES = {3: "pending", 4: "pending-held", 5: "processing", 6: "processing-stopped",
              7: "canceled", 8: "aborted", 9: "completed"}
PRINTER_STATES = {3: "idle", 4: "processing", 5: "stopped"}

MEDIA = {
    "A3": "iso_a3_297x420mm",
    "A4": "iso_a4_210x297mm",
    "A5": "iso_a5_148x210mm",
    "Letter": "na_letter_8.5x11in",
    "Legal": "na_legal_8.5x14in",
    "Tabloid": "na_ledger_11x17in",
}
SIDES = {"none": "one-sided", "long": "two-sided-long-edge", "short": "two-sided-short-edge"}

_request_ids = itertools.count(1)


class IppError(Exception):
    pass


def _value(tag: int, value) -> bytes:
    if tag in (INTEGER, ENUM):
        return struct.pack(">i", int(value))
    if tag == BOOLEAN:
        return b"\x01" if value else b"\x00"
    if tag == RANGE:
        return struct.pack(">ii", *value)
    return str(value).encode("utf-8")


def _attr(tag: int, name: str, values) -> bytes:
    if not isinstance(values, (list, tuple)) or tag == RANGE and isinstance(values, tuple):
        values = [values]
    out = b""
    for i, v in enumerate(values):
        n = name.encode() if i == 0 else b""
        vb = _value(tag, v)
        out += struct.pack(">BH", tag, len(n)) + n + struct.pack(">H", len(vb)) + vb
    return out


def build_request(op: int, printer_uri: str, op_attrs=(), job_attrs=()) -> bytes:
    b = struct.pack(">BBHI", 2, 0, op, next(_request_ids))
    b += bytes([TAG_OPERATION])
    b += _attr(CHARSET, "attributes-charset", "utf-8")
    b += _attr(LANGUAGE, "attributes-natural-language", "de")
    b += _attr(URI, "printer-uri", printer_uri)
    for tag, name, val in op_attrs:
        b += _attr(tag, name, val)
    if job_attrs:
        b += bytes([TAG_JOB])
        for tag, name, val in job_attrs:
            b += _attr(tag, name, val)
    b += bytes([TAG_END])
    return b


def _decode(tag: int, raw: bytes):
    if tag in (INTEGER, ENUM) and len(raw) == 4:
        return struct.unpack(">i", raw)[0]
    if tag == BOOLEAN and len(raw) == 1:
        return raw != b"\x00"
    if tag == RANGE and len(raw) == 8:
        return struct.unpack(">ii", raw)
    if 0x40 <= tag <= 0x4A and tag not in (0x35, 0x36):
        return raw.decode("utf-8", "replace")
    return raw


def parse_response(data: bytes) -> tuple[int, list[tuple[int, dict]]]:
    if len(data) < 8:
        raise IppError("IPP-Antwort zu kurz.")
    _, status, _ = struct.unpack(">HHI", data[:8])
    i, groups, cur, last, depth = 8, [], None, None, 0
    while i < len(data):
        tag = data[i]
        i += 1
        if tag == TAG_END:
            break
        if tag < 0x10:
            cur = {}
            groups.append((tag, cur))
            continue
        nlen = struct.unpack(">H", data[i:i + 2])[0]
        name = data[i + 2:i + 2 + nlen].decode("utf-8", "replace")
        i += 2 + nlen
        vlen = struct.unpack(">H", data[i:i + 2])[0]
        raw = data[i + 2:i + 2 + vlen]
        i += 2 + vlen
        # Collections (z. B. media-col) überspringen – brauchen wir nicht.
        if tag == BEG_COLLECTION:
            depth += 1
            continue
        if tag == END_COLLECTION:
            depth -= 1
            continue
        if depth > 0 or tag == MEMBER_NAME or cur is None:
            continue
        val = _decode(tag, raw)
        if nlen:
            cur[name] = [val]
            last = name
        elif last:
            cur[last].append(val)
    return status, groups


def _post(printer_uri: str, body: bytes, timeout: float) -> bytes:
    u = urllib.parse.urlparse(printer_uri)
    port = u.port or 631
    if u.scheme in ("ipps", "https"):
        # Drucker haben fast immer selbstsignierte Zertifikate.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(u.hostname, port, timeout=timeout, context=ctx)
    else:
        conn = http.client.HTTPConnection(u.hostname, port, timeout=timeout)
    try:
        conn.request("POST", u.path or "/ipp/print", body=body, headers={"Content-Type": "application/ipp"})
        resp = conn.getresponse()
        data = resp.read()
        if resp.status != 200:
            raise IppError(f"HTTP {resp.status} vom Drucker.")
        return data
    except OSError as e:
        raise IppError(f"Drucker nicht erreichbar ({printer_uri}): {e}") from e
    finally:
        conn.close()


def _call(printer_uri: str, body: bytes, timeout: float = 30) -> list[tuple[int, dict]]:
    status, groups = parse_response(_post(printer_uri, body, timeout))
    if status >= 0x0100:
        msg = ""
        for tag, attrs in groups:
            if tag == TAG_OPERATION and "status-message" in attrs:
                msg = attrs["status-message"][0]
        raise IppError(f"IPP-Fehler 0x{status:04x}{': ' + msg if msg else ''}")
    return groups


def _group(groups, tag: int) -> dict:
    merged: dict = {}
    for t, attrs in groups:
        if t == tag:
            merged.update(attrs)
    return merged


def get_printer_attributes(printer_uri: str, names: list[str] | None = None, timeout: float = 10) -> dict:
    op = [(NAME, "requesting-user-name", "druckagent")]
    if names:
        op.append((KEYWORD, "requested-attributes", names))
    body = build_request(GET_PRINTER_ATTRIBUTES, printer_uri, op)
    return _group(_call(printer_uri, body, timeout), TAG_PRINTER)


def printer_status(printer_uri: str) -> dict:
    a = get_printer_attributes(
        printer_uri,
        ["printer-state", "printer-state-reasons", "printer-state-message",
         "marker-names", "marker-levels", "marker-colors", "printer-make-and-model"],
    )
    names = a.get("marker-names", [])
    levels = a.get("marker-levels", [])
    markers = [{"name": n, "level": levels[i] if i < len(levels) else None} for i, n in enumerate(names)]
    reasons = [r for r in a.get("printer-state-reasons", []) if r != "none"]
    return {
        "state": PRINTER_STATES.get((a.get("printer-state") or [0])[0], "unknown"),
        "reasons": reasons,
        "message": (a.get("printer-state-message") or [""])[0],
        "model": (a.get("printer-make-and-model") or [""])[0],
        "markers": markers,
    }


def print_job(
    printer_uri: str,
    pdf: Path,
    *,
    job_name: str,
    user: str,
    copies: int = 1,
    sides: str = "one-sided",
    media: str | None = None,
    color: bool = True,
    page_ranges: list[tuple[int, int]] | None = None,
    scaling: str | None = "fit",
    document_format: str = "application/pdf",
    timeout: float = 120,
) -> int:
    job_attrs = [
        (INTEGER, "copies", copies),
        (KEYWORD, "sides", sides),
        (KEYWORD, "print-color-mode", "color" if color else "monochrome"),
    ]
    if media:
        job_attrs.append((KEYWORD, "media", media))
    if page_ranges:
        job_attrs.append((RANGE, "page-ranges", page_ranges))
    if scaling:
        job_attrs.append((KEYWORD, "print-scaling", scaling))
    op = [
        (NAME, "requesting-user-name", user[:60] or "druckagent"),
        (NAME, "job-name", job_name[:200] or "druckauftrag"),
        (BOOLEAN, "ipp-attribute-fidelity", False),
        (MIME, "document-format", document_format),
    ]
    body = build_request(PRINT_JOB, printer_uri, op, job_attrs) + pdf.read_bytes()
    groups = _call(printer_uri, body, timeout)
    job = _group(groups, TAG_JOB)
    if "job-id" not in job:
        raise IppError("Drucker hat keine job-id zurückgegeben.")
    return job["job-id"][0]


def wait_for_job(printer_uri: str, job_id: int, timeout_s: float, poll_s: float = 3) -> None:
    """Wartet bis der Job fertig ist. Wirft IppError bei Abbruch/Timeout."""
    deadline = time.monotonic() + timeout_s
    last = {}
    while time.monotonic() < deadline:
        body = build_request(
            GET_JOB_ATTRIBUTES, printer_uri,
            [(INTEGER, "job-id", job_id), (NAME, "requesting-user-name", "druckagent"),
             (KEYWORD, "requested-attributes", ["job-state", "job-state-reasons", "job-state-message"])],
        )
        last = _group(_call(printer_uri, body, 15), TAG_JOB)
        state = (last.get("job-state") or [0])[0]
        if state == 9:
            return
        if state in (7, 8):
            reasons = ", ".join(r for r in last.get("job-state-reasons", []) if r != "none")
            raise IppError(f"Druckauftrag {JOB_STATES[state]}{': ' + reasons if reasons else ''}")
        time.sleep(poll_s)
    reasons = ", ".join(last.get("job-state-reasons", []))
    raise IppError(f"Drucker hat den Auftrag nicht rechtzeitig abgeschlossen ({reasons or 'kein Grund'}).")


def parse_ranges(s: str | None) -> list[tuple[int, int]] | None:
    if not s:
        return None
    ranges = []
    for part in s.split(","):
        a, _, b = part.partition("-")
        ranges.append((int(a), int(b or a)))
    # IPP verlangt aufsteigende, nicht überlappende Bereiche.
    merged: list[tuple[int, int]] = []
    for a, b in sorted(ranges):
        if merged and a <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged
