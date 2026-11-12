#!/usr/bin/env python3
"""Druck-Agent: holt Aufträge vom Druckdienst ab und druckt sie lokal.

Nur Python-Standardbibliothek (ab 3.11). Druckwege:
  sumatra  – Windows, SumatraPDF im Silent-Modus (Standard)
  cups     – Linux/macOS, lp
  ipp      – direkt an den Drucker (IPP, Port 631), falls er PDF nativ annimmt
  dummy    – druckt nicht, nur zum Testen
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ipp  # noqa: E402

VERSION = "1.0.0"
IS_WINDOWS = os.name == "nt"
log = logging.getLogger("druckagent")


class NetError(Exception):
    """Server nicht erreichbar – später erneut versuchen."""


class PrintError(Exception):
    """Druck fehlgeschlagen – Meldung geht an den Nutzer."""


# ---------------------------------------------------------------- Konfiguration

DEFAULTS = {
    "poll_interval": 5,
    "heartbeat_interval": 30,
    "method": "sumatra" if IS_WINDOWS else "cups",
    "printer": "",
    "work_dir": str(HERE / "work"),
    "log_file": str(HERE / "druckagent.log"),
    "print_timeout": 600,
    "verify_tls": True,
    "sumatra": {
        "path": r"C:\Program Files\SumatraPDF\SumatraPDF.exe",
        "scaling": "fit",
        "wait_for_spooler": True,
        "extra": [],
    },
    "cups": {"options": []},
    "ipp": {"uri": "", "status_uri": "", "scaling": "fit", "wait_for_completion": True},
    "route": [],
}

MATCH_KEYS = ("preset", "format", "farbe", "duplex")


def load_config(path: Path) -> dict:
    with path.open("rb") as f:
        user = tomllib.load(f)
    cfg = json.loads(json.dumps(DEFAULTS))
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    for key in ("server_url", "api_token"):
        if not cfg.get(key):
            raise SystemExit(f"Konfiguration: '{key}' fehlt in {path}")
    cfg["server_url"] = cfg["server_url"].rstrip("/")
    if cfg["method"] not in ("sumatra", "cups", "ipp", "dummy"):
        raise SystemExit("Konfiguration: method muss sumatra, cups, ipp oder dummy sein.")
    for r in cfg["route"]:
        if not any(k in r for k in MATCH_KEYS):
            raise SystemExit(f"Konfiguration: route {r} hat keine Bedingung ({', '.join(MATCH_KEYS)}).")
    return cfg


def setup_logging(log_file: str) -> None:
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    if sys.stderr is not None:  # unter pythonw.exe gibt es keine Konsole
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


# ---------------------------------------------------------------- Server-API

class Api:
    def __init__(self, cfg: dict) -> None:
        self.base = cfg["server_url"]
        self.token = cfg["api_token"]
        self.ctx = None
        if not cfg["verify_tls"]:
            import ssl

            self.ctx = ssl._create_unverified_context()

    def _request(self, method: str, path: str, body: dict | None = None, timeout: float = 30):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("User-Agent", f"druckagent/{VERSION}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            return urllib.request.urlopen(req, timeout=timeout, context=self.ctx)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                log.error("Server lehnt das API-Token ab (HTTP %s). Bitte api_token prüfen.", e.code)
                raise NetError(f"HTTP {e.code}") from e
            if e.code >= 500 or e.code == 429:
                raise NetError(f"HTTP {e.code}") from e
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout, OSError) as e:
            raise NetError(str(e)) from e

    def claim(self) -> dict | None:
        with self._request("POST", "/api/agent/claim", {}) as r:
            if r.status == 204:
                return None
            return json.loads(r.read())

    def download(self, job_id: str, dest: Path) -> None:
        tmp = dest.with_suffix(".part")
        with self._request("GET", f"/api/agent/jobs/{job_id}/file", timeout=120) as r, tmp.open("wb") as f:
            shutil.copyfileobj(r, f, 1024 * 1024)
        with tmp.open("rb") as f:
            if b"%PDF-" not in f.read(1024):
                tmp.unlink(missing_ok=True)
                raise PrintError("Heruntergeladene Datei ist keine PDF.")
        tmp.replace(dest)

    def report(self, job_id: str, status: str, fehler: str | None = None) -> None:
        try:
            with self._request("POST", f"/api/agent/jobs/{job_id}/status", {"status": status, "fehler": fehler}):
                pass
        except urllib.error.HTTPError as e:
            if e.code in (404, 409):
                log.warning("Status für %s nicht übernommen (HTTP %s, z. B. inzwischen abgebrochen).", job_id, e.code)
                return
            raise

    def heartbeat(self, info: dict) -> None:
        with self._request("POST", "/api/agent/heartbeat", info, timeout=15):
            pass


class PendingReports:
    """Statusmeldungen, die wegen Netzwerkausfall nicht ankamen – persistent, damit nichts verloren geht."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.items: list[dict] = []
        if path.exists():
            try:
                self.items = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                log.exception("pending_reports.json unlesbar – wird verworfen")

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, job_id: str, status: str, fehler: str | None) -> None:
        self.items.append({"id": job_id, "status": status, "fehler": fehler})
        self._save()

    def flush(self, api: Api) -> None:
        while self.items:
            it = self.items[0]
            api.report(it["id"], it["status"], it["fehler"])  # NetError bricht ab, Rest bleibt gespeichert
            log.info("Nachgereichte Statusmeldung für %s: %s", it["id"], it["status"])
            self.items.pop(0)
            self._save()


# ---------------------------------------------------------------- Routing & Drucken

def resolve_route(job: dict, cfg: dict) -> dict:
    """Erste passende [[route]] gewinnt; sonst Standard (method/printer aus der Hauptkonfiguration)."""
    route = {"method": cfg["method"], "printer": cfg["printer"], "bin": None, "extra": [], "name": "standard"}
    for i, r in enumerate(cfg["route"]):
        if all(job.get(k) == r[k] for k in MATCH_KEYS if k in r):
            route.update({k: r[k] for k in ("method", "printer", "bin", "extra") if k in r})
            route["name"] = r.get("name", f"route[{i}]")
            break
    return route


def sumatra_settings(job: dict, route: dict, cfg: dict) -> str:
    s = []
    if job.get("seiten"):
        s.append(job["seiten"])  # z. B. "1-3,5"
    s.append(f"paper={job['format']}")
    s.append("color" if job["farbe"] else "monochrome")
    s.append({"none": "simplex", "long": "duplexlong", "short": "duplexshort"}[job["duplex"]])
    s.append(f"{int(job['kopien'])}x")
    if cfg["sumatra"].get("scaling"):
        s.append(cfg["sumatra"]["scaling"])
    if route.get("bin"):
        s.append(f"bin={route['bin']}")
    s += list(cfg["sumatra"].get("extra", [])) + list(route.get("extra", []))
    return ",".join(s)


def _ps(cmd: str, timeout: float = 30) -> str:
    r = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", cmd],
        capture_output=True, text=True, timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if r.returncode != 0:
        raise PrintError(f"PowerShell-Fehler: {r.stderr.strip()[:300]}")
    return r.stdout


def wait_for_windows_spooler(printer: str, doc_name: str, timeout_s: float) -> None:
    """Wartet, bis der Auftrag die Windows-Druckwarteschlange verlassen hat."""
    q = printer.replace("'", "''")
    cmd = (
        f"Get-PrintJob -PrinterName '{q}' | Select-Object Id, DocumentName, "
        "@{n='Status';e={[string]$_.JobStatus}} | ConvertTo-Json -Compress"
    )
    deadline = time.monotonic() + timeout_s
    status = ""
    while time.monotonic() < deadline:
        out = _ps(cmd).strip()
        jobs = json.loads(out) if out else []
        if isinstance(jobs, dict):
            jobs = [jobs]
        mine = [j for j in jobs if doc_name in (j.get("DocumentName") or "")]
        if not mine:
            return
        status = mine[0].get("Status") or ""
        if any(w in status for w in ("Error", "Offline", "PaperOut", "Blocked", "UserIntervention")):
            log.warning("Windows-Warteschlange meldet: %s", status)
        time.sleep(3)
    raise PrintError(
        f"Auftrag hängt in der Windows-Druckwarteschlange ({status or 'unbekannt'}). Papier/Tinte/Stau am Drucker prüfen."
    )


def print_sumatra(path: Path, job: dict, route: dict, cfg: dict) -> None:
    exe = cfg["sumatra"]["path"]
    if not Path(exe).exists():
        raise PrintError(f"SumatraPDF nicht gefunden: {exe}")
    if not route["printer"]:
        raise PrintError("Kein Druckername konfiguriert.")
    settings = sumatra_settings(job, route, cfg)
    args = [exe, "-print-to", route["printer"], "-print-settings", settings, "-silent", "-exit-when-done", str(path)]
    log.info("SumatraPDF: %s", " ".join(f'"{a}"' if " " in a else a for a in args))
    try:
        r = subprocess.run(
            args, capture_output=True, text=True, timeout=cfg["print_timeout"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise PrintError("SumatraPDF hat nicht rechtzeitig geantwortet.") from None
    if r.returncode != 0:
        raise PrintError(f"SumatraPDF-Fehler (Code {r.returncode}) {(r.stderr or r.stdout).strip()[:300]}")
    if cfg["sumatra"].get("wait_for_spooler") and IS_WINDOWS:
        wait_for_windows_spooler(route["printer"], path.name, cfg["print_timeout"])


def print_cups(path: Path, job: dict, route: dict, cfg: dict) -> None:
    args = ["lp"]
    if route["printer"]:
        args += ["-d", route["printer"]]
    args += ["-n", str(int(job["kopien"])), "-t", job.get("datei", path.name)[:100]]
    args += ["-o", f"media={job['format']}"]
    args += ["-o", "sides=" + ipp.SIDES[job["duplex"]]]
    args += ["-o", "print-color-mode=" + ("color" if job["farbe"] else "monochrome")]
    if job.get("seiten"):
        args += ["-o", f"page-ranges={job['seiten']}"]
    if route.get("bin"):
        args += ["-o", f"InputSlot={route['bin']}"]
    for opt in list(cfg["cups"].get("options", [])) + list(route.get("extra", [])):
        args += ["-o", opt]
    args.append(str(path))
    log.info("CUPS: %s", " ".join(args))
    r = subprocess.run(args, capture_output=True, text=True, timeout=cfg["print_timeout"])
    if r.returncode != 0:
        raise PrintError(f"lp-Fehler: {(r.stderr or r.stdout).strip()[:300]}")


def print_ipp(path: Path, job: dict, route: dict, cfg: dict) -> None:
    uri = route["printer"] if str(route["printer"]).startswith(("ipp://", "ipps://")) else cfg["ipp"]["uri"]
    if not uri:
        raise PrintError("Keine IPP-Adresse konfiguriert ([ipp] uri).")
    media = ipp.MEDIA.get(job["format"])
    try:
        job_id = ipp.print_job(
            uri, path,
            job_name=job.get("datei", path.name), user=job.get("nutzer", "druckagent"),
            copies=int(job["kopien"]), sides=ipp.SIDES[job["duplex"]], media=media,
            color=bool(job["farbe"]), page_ranges=ipp.parse_ranges(job.get("seiten")),
            scaling=cfg["ipp"].get("scaling") or None,
        )
        log.info("IPP: Auftrag an %s übergeben, Drucker-Job-ID %s", uri, job_id)
        if cfg["ipp"].get("wait_for_completion", True):
            ipp.wait_for_job(uri, job_id, cfg["print_timeout"])
    except ipp.IppError as e:
        raise PrintError(str(e)) from e


def print_dummy(path: Path, job: dict, route: dict, cfg: dict) -> None:
    log.info("DUMMY-Druck: %s (%d Bytes) mit %s", path.name, path.stat().st_size,
             sumatra_settings(job, route, cfg))
    time.sleep(1)


PRINTERS = {"sumatra": print_sumatra, "cups": print_cups, "ipp": print_ipp, "dummy": print_dummy}


def print_file(path: Path, job: dict, cfg: dict) -> None:
    route = resolve_route(job, cfg)
    log.info("Route '%s': method=%s printer=%s", route["name"], route["method"], route["printer"] or "-")
    PRINTERS[route["method"]](path, job, route, cfg)


# ---------------------------------------------------------------- Hauptschleife

class Agent:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.api = Api(cfg)
        self.work = Path(cfg["work_dir"])
        self.work.mkdir(parents=True, exist_ok=True)
        self.pending = PendingReports(self.work / "pending_reports.json")
        self.last_heartbeat = 0.0
        self.last_message = "gestartet"

    def cleanup_work_dir(self) -> None:
        for f in self.work.glob("*.pdf"):
            f.unlink(missing_ok=True)
        for f in self.work.glob("*.part"):
            f.unlink(missing_ok=True)

    def report(self, job_id: str, status: str, fehler: str | None = None) -> None:
        try:
            self.api.report(job_id, status, fehler)
        except NetError as e:
            log.warning("Statusmeldung fehlgeschlagen (%s) – wird nachgereicht.", e)
            self.pending.add(job_id, status, fehler)

    def heartbeat(self) -> None:
        info = {
            "version": VERSION,
            "hostname": socket.gethostname(),
            "method": self.cfg["method"],
            "printer": self.cfg["printer"] or self.cfg["ipp"].get("uri", ""),
            "message": self.last_message,
        }
        status_uri = self.cfg["ipp"].get("status_uri") or self.cfg["ipp"].get("uri")
        if status_uri:
            try:
                info["printer_status"] = ipp.printer_status(status_uri)
            except Exception as e:
                info["printer_status"] = {"state": "nicht erreichbar", "reasons": [str(e)[:200]], "markers": []}
        self.api.heartbeat(info)
        self.last_heartbeat = time.monotonic()

    def process(self, job: dict) -> None:
        job_id = job["id"]
        log.info(
            "Auftrag %s von %s: %s | %s %s %s %sx Seiten=%s Preset=%s",
            job_id, job.get("nutzer"), job.get("datei"), job["format"],
            "farbe" if job["farbe"] else "sw", job["duplex"], job["kopien"], job.get("seiten") or "alle",
            job.get("preset") or "-",
        )
        path = self.work / f"{job_id}.pdf"
        try:
            for attempt in range(3):
                try:
                    self.api.download(job_id, path)
                    break
                except NetError:
                    if attempt == 2:
                        raise
                    time.sleep(5 * (attempt + 1))
            print_file(path, job, self.cfg)
            self.last_message = f"zuletzt gedruckt: {job.get('datei')}"
            log.info("Auftrag %s erfolgreich gedruckt", job_id)
            self.report(job_id, "done")
        except PrintError as e:
            log.error("Auftrag %s fehlgeschlagen: %s", job_id, e)
            self.last_message = f"Fehler: {e}"
            self.report(job_id, "error", str(e))
        except NetError as e:
            log.error("Auftrag %s: Download fehlgeschlagen: %s", job_id, e)
            self.report(job_id, "error", "Druck-PC konnte die Datei nicht herunterladen (Netzwerk).")
        except Exception as e:
            log.exception("Unerwarteter Fehler bei Auftrag %s", job_id)
            self.report(job_id, "error", f"Interner Fehler im Druck-Agenten: {type(e).__name__}")
        finally:
            path.unlink(missing_ok=True)

    def run(self, once: bool = False) -> None:
        log.info("Druck-Agent %s startet: Server %s, Methode %s, Drucker %s",
                 VERSION, self.cfg["server_url"], self.cfg["method"], self.cfg["printer"] or "-")
        self.cleanup_work_dir()
        backoff = self.cfg["poll_interval"]
        while True:
            try:
                self.pending.flush(self.api)
                if time.monotonic() - self.last_heartbeat >= self.cfg["heartbeat_interval"]:
                    self.heartbeat()
                job = self.api.claim()
                if backoff != self.cfg["poll_interval"]:
                    log.info("Verbindung zum Server wiederhergestellt.")
                backoff = self.cfg["poll_interval"]
                if job:
                    self.process(job)
                    if not once:
                        continue  # sofort nächsten Auftrag holen
            except NetError as e:
                log.warning("Server nicht erreichbar: %s – neuer Versuch in %ss", e, backoff)
                backoff = min(backoff * 2, 120)
            except Exception:
                log.exception("Unerwarteter Fehler in der Hauptschleife")
            if once:
                return
            time.sleep(backoff)


# ---------------------------------------------------------------- CLI

def single_instance_lock():
    """Verhindert, dass zwei Agenten gleichzeitig laufen (würde sonst doppelt pollen)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 47631))
    except OSError:
        raise SystemExit("Druck-Agent läuft bereits.")
    return s


def cmd_check_ipp(uri: str) -> None:
    attrs = ipp.get_printer_attributes(uri)
    show = [
        "printer-make-and-model", "printer-state", "printer-state-reasons", "document-format-supported",
        "media-supported", "media-ready", "sides-supported", "print-color-mode-supported",
        "copies-supported", "page-ranges-supported", "print-scaling-supported", "media-source-supported",
        "print-quality-supported", "marker-names", "marker-levels",
    ]
    for k in show:
        if k in attrs:
            print(f"{k:28} {', '.join(map(str, attrs[k]))}")
    formats = attrs.get("document-format-supported", [])
    print()
    if "application/pdf" in formats:
        print("✔ Drucker nimmt PDF nativ an → method = \"ipp\" ist möglich.")
    else:
        print("✘ Kein application/pdf – IPP-Direktdruck nicht möglich. Bitte sumatra (Windows) bzw. cups nutzen.")
        print("  (Statusabfrage/Tintenstände per [ipp] status_uri funktionieren trotzdem.)")


def cmd_list_printers() -> None:
    if IS_WINDOWS:
        print(_ps("Get-Printer | Format-Table -AutoSize Name, DriverName, PortName | Out-String -Width 200"))
    else:
        subprocess.run(["lpstat", "-p", "-d"])


def main() -> None:
    p = argparse.ArgumentParser(description="Druck-Agent für den Web-Druckdienst")
    p.add_argument("--config", default=str(HERE / "config.toml"))
    p.add_argument("--once", action="store_true", help="nur einen Poll-Durchlauf")
    p.add_argument("--check-ipp", metavar="URI", help="IPP-Fähigkeiten des Druckers anzeigen, z. B. ipp://192.168.1.50/ipp/print")
    p.add_argument("--list-printers", action="store_true", help="installierte Drucker/Instanzen anzeigen")
    p.add_argument("--test-print", metavar="PDF", help="lokale Datei mit Routing testen (ohne Server)")
    p.add_argument("--format", default="A4")
    p.add_argument("--farbe", action="store_true")
    p.add_argument("--duplex", default="none", choices=["none", "long", "short"])
    p.add_argument("--kopien", type=int, default=1)
    p.add_argument("--seiten", default=None)
    p.add_argument("--preset", default=None)
    a = p.parse_args()

    if a.check_ipp:
        cmd_check_ipp(a.check_ipp)
        return
    if a.list_printers:
        cmd_list_printers()
        return

    cfg = load_config(Path(a.config))
    setup_logging(cfg["log_file"])

    if a.test_print:
        job = {"id": "test", "datei": Path(a.test_print).name, "format": a.format, "farbe": a.farbe,
               "duplex": a.duplex, "kopien": a.kopien, "seiten": a.seiten, "preset": a.preset, "nutzer": "test"}
        print_file(Path(a.test_print).resolve(), job, cfg)
        log.info("Testdruck übergeben.")
        return

    _lock = single_instance_lock()  # noqa: F841 – Socket muss offen bleiben
    try:
        Agent(cfg).run(once=a.once)
    except KeyboardInterrupt:
        log.info("Beendet.")


if __name__ == "__main__":
    main()
