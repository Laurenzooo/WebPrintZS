"""Web-Druckdienst: Upload, Warteschlange, Status, Admin und Agent-API."""
from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.middleware.sessions import SessionMiddleware

from . import db
from .cleanup import delete_job_file, job_file, run_cleanup
from .config import load_presets, settings
from .pdfcheck import PDF_MIME_TYPES, Rejected, check_magic, count_pages, parse_page_range, sheets_needed
from .timeutil import ago_iso, fmt_local, now_iso, seconds_since

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("druckdienst")

BASE = Path(__file__).resolve().parent
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("font/woff2", ".woff2")
STATUS_LABELS = {
    "queued": "In Warteschlange",
    "printing": "Wird gedruckt",
    "done": "Gedruckt",
    "error": "Fehler",
    "cancelled": "Abgebrochen",
    "expired": "Verfallen",
}
ORIENTATION_LABELS = {"auto": "automatisch", "portrait": "Hochformat", "landscape": "Querformat"}
DUPLEX_LABELS = {"none": "einseitig", "long": "beidseitig (lange Kante)", "short": "beidseitig (kurze Kante)"}

settings.validate()
PRESETS = load_presets(settings)
PRESET_IDS = {p["id"] for p in PRESETS}


# ---------------------------------------------------------------- Hilfsfunktionen

def _version(secret: str) -> str:
    """Kurzer Fingerabdruck – ändert sich der Code/das Passwort, werden alte Sitzungen ungültig."""
    return hashlib.sha256(("v1:" + secret).encode()).hexdigest()[:16]


def current_user(request: Request) -> dict | None:
    s = request.session
    if s.get("cv") == _version(settings.access_code) and s.get("name") and s.get("sid"):
        return s
    return None


def is_admin(request: Request) -> bool:
    return request.session.get("av") == _version(settings.admin_password)


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


def check_csrf(request: Request, value: str | None) -> None:
    expected = request.session.get("csrf", "")
    if not expected or not value or not hmac.compare_digest(expected, value):
        raise HTTPException(403, "Ungültiges Formular-Token. Bitte Seite neu laden.")


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def flash(request: Request, msg: str, kind: str = "ok") -> None:
    request.session.setdefault("flash", []).append([kind, msg])


def pop_flash(request: Request) -> list:
    return request.session.pop("flash", [])


def clean_text(value: str, max_len: int) -> str:
    value = re.sub(r"[\x00-\x1f\x7f]", "", value or "").strip()
    return value[:max_len]


def agent_status() -> dict:
    info = db.kv_get("agent") or {}
    age = seconds_since(info.get("last_seen"))
    info["online"] = age is not None and age <= settings.agent_offline_after_s
    info["age_s"] = age
    return info


def job_public(row) -> dict:
    return {
        "id": row["id"],
        "datei": row["filename"],
        "format": row["format"],
        "farbe": bool(row["color"]),
        "duplex": row["duplex"],
        "kopien": row["copies"],
        "seiten": row["page_range"],
        "ausrichtung": row["orientation"],
        "skalierung": row["scaling"],
        "seitenzahl": row["pages"],
        "druckseiten": row["print_pages"],
        "blatt": row["sheets"],
        "preset": row["preset"],
        "status": row["status"],
        "status_text": STATUS_LABELS[row["status"]],
        "fehler": row["error"],
        "erstellt": row["created_at"],
        "erstellt_lokal": fmt_local(row["created_at"], settings.display_tz),
        "erstellt_zeit": fmt_local(row["created_at"], settings.display_tz, "%H:%M"),
        "erstellt_tag": fmt_local(row["created_at"], settings.display_tz, "%d.%m."),
        "position": db.queue_position(row["id"]) if row["status"] == "queued" else None,
    }


def marker_color(name: str) -> str:
    n = (name or "").lower()
    for key, col in (("cyan", "#00A3E0"), ("magenta", "#D6247A"), ("yellow", "#F2C200"),
                     ("gelb", "#F2C200"), ("black", "#2B2A27"), ("schwarz", "#2B2A27")):
        if key in n:
            return col
    return "#7A7975"


PRINTER_STATES_DE = {"idle": "bereit", "processing": "druckt", "stopped": "angehalten", "nicht erreichbar": "nicht erreichbar"}
PRINTER_REASONS_DE = {
    "media-low": "Papier fast leer", "media-empty": "Papier leer", "media-needed": "Papier einlegen",
    "media-jam": "Papierstau", "marker-supply-low": "Tinte niedrig", "marker-supply-empty": "Tinte leer",
    "door-open": "Klappe offen", "cover-open": "Abdeckung offen", "input-tray-missing": "Papierfach fehlt",
    "offline": "offline", "paused": "pausiert", "toner-low": "Toner niedrig", "toner-empty": "Toner leer",
}


def printer_state_de(state: str) -> str:
    return PRINTER_STATES_DE.get(state or "", state or "unbekannt")


def printer_reasons_de(reasons: list) -> list:
    out = []
    for r in reasons or []:
        base = str(r).rsplit("-", 1)[0] if str(r).endswith(("-report", "-warning", "-error")) else str(r)
        out.append(PRINTER_REASONS_DE.get(base, base))
    return out


def marker_label(name: str) -> str:
    n = (name or "").lower()
    for key, label in (("cyan", "Cyan"), ("magenta", "Magenta"), ("yellow", "Gelb"), ("black", "Schwarz")):
        if key in n:
            return label
    return name


class LoginLimiter:
    """Fehlversuche pro IP im Speicher (ein Prozess genügt für dieses Projekt)."""

    def __init__(self, max_failures: int, window_s: int) -> None:
        self.max_failures, self.window_s = max_failures, window_s
        self._fails: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str) -> deque:
        q = self._fails[key]
        cutoff = time.monotonic() - self.window_s
        while q and q[0] < cutoff:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key)) >= self.max_failures

    def fail(self, key: str) -> None:
        with self._lock:
            self._prune(key).append(time.monotonic())

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)


login_limiter = LoginLimiter(settings.login_max_failures, settings.login_window_min * 60)


# ---------------------------------------------------------------- App

async def _cleanup_loop() -> None:
    while True:
        try:
            await run_in_threadpool(run_cleanup, settings)
        except Exception:
            log.exception("Cleanup fehlgeschlagen")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.files_dir.mkdir(parents=True, exist_ok=True)
    db.init(str(settings.db_path))
    task = asyncio.create_task(_cleanup_loop())
    log.info("Druckdienst gestartet, Daten in %s, %d Presets", settings.data_dir, len(PRESETS))
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.filters["localtime"] = lambda s: fmt_local(s, settings.display_tz)
templates.env.globals.update(
    STATUS_LABELS=STATUS_LABELS, DUPLEX_LABELS=DUPLEX_LABELS, ORIENTATION_LABELS=ORIENTATION_LABELS, site_title=settings.site_title
)

CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
    "worker-src 'self'; font-src 'self'; "
    "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
)


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.method == "POST" and request.url.path == "/upload":
        length = request.headers.get("content-length")
        if length is None:
            return PlainTextResponse("Content-Length fehlt.", status_code=411)
        if int(length) > settings.max_upload_bytes + 1024 * 1024:
            return JSONResponse(
                {"ok": False, "error": f"Datei zu groß (max. {settings.max_upload_mb} MB)."}, status_code=413
            )
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if request.url.path.startswith(("/api/", "/admin")):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# Muss nach @app.middleware registriert werden, damit die Session auch dort existiert.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="druck_session",
    max_age=settings.session_days * 86400,
    same_site="lax",
    https_only=settings.cookie_secure,
)


def render(request: Request, template: str, status_code: int = 200, **ctx) -> HTMLResponse:
    ctx.update(request=request, csrf=csrf_token(request), flashes=pop_flash(request), is_admin=is_admin(request))
    return templates.TemplateResponse(request, template, ctx, status_code=status_code)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/") or request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse({"ok": False, "error": exc.detail}, status_code=exc.status_code)
    return render(request, "error.html", status_code=exc.status_code, message=exc.detail)


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------- Login

@app.get("/login")
def login_form(request: Request):
    if current_user(request):
        return RedirectResponse("/", 303)
    return render(request, "login.html", name=request.session.get("name", ""))


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    check_csrf(request, form.get("csrf"))
    ip = client_ip(request)
    name = clean_text(str(form.get("name", "")), 40)
    if login_limiter.blocked(ip):
        flash(request, f"Zu viele Fehlversuche. Bitte in {settings.login_window_min} Minuten erneut versuchen.", "error")
        return RedirectResponse("/login", 303)
    code = str(form.get("code", "")).strip()
    if not hmac.compare_digest(code.encode(), settings.access_code.encode()):
        login_limiter.fail(ip)
        log.info("Falscher Zugangscode von %s", ip)
        flash(request, "Zugangscode falsch.", "error")
        return RedirectResponse("/login", 303)
    if len(name) < 2:
        flash(request, "Bitte einen Namen angeben (mind. 2 Zeichen) – er erscheint im Druck-Log.", "error")
        return RedirectResponse("/login", 303)
    login_limiter.reset(ip)
    sid = request.session.get("sid") or secrets.token_urlsafe(16)
    request.session.update(name=name, sid=sid, cv=_version(settings.access_code))
    return RedirectResponse("/", 303)


@app.post("/logout")
async def logout(request: Request):
    form = await request.form()
    check_csrf(request, form.get("csrf"))
    request.session.clear()
    return RedirectResponse("/login", 303)


# ---------------------------------------------------------------- Nutzerseite

@app.get("/")
def index(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 303)
    jobs = [job_public(r) for r in db.jobs_for_session(user["sid"])]
    return render(
        request,
        "index.html",
        user=user,
        jobs=jobs,
        presets=PRESETS,
        presets_json=json.dumps(PRESETS).replace("</", "<\\/"),
        formats=settings.formats,
        duplex_formats_json=json.dumps(settings.duplex_formats),
        max_copies=settings.max_copies,
        max_pages=settings.max_pages,
        max_upload_mb=settings.max_upload_mb,
        agent=agent_status(),
    )


@app.get("/api/my-jobs")
def my_jobs(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Nicht angemeldet.")
    agent = agent_status()
    return {
        "jobs": [job_public(r) for r in db.jobs_for_session(user["sid"])],
        "agent_online": agent["online"],
    }


@app.post("/upload")
async def upload(request: Request):
    xhr = request.headers.get("x-requested-with") == "XMLHttpRequest"

    def fail(msg: str, code: int = 400):
        if xhr:
            return JSONResponse({"ok": False, "error": msg}, status_code=code)
        flash(request, msg, "error")
        return RedirectResponse("/", 303)

    user = current_user(request)
    if not user:
        return fail("Sitzung abgelaufen – bitte neu anmelden.", 401) if xhr else RedirectResponse("/login", 303)

    form = await request.form(max_files=1, max_fields=20)
    check_csrf(request, form.get("csrf"))
    ip = client_ip(request)

    # --- Einstellungen validieren
    fmt = str(form.get("format", ""))
    if fmt not in settings.formats:
        return fail("Ungültiges Papierformat.")
    color = str(form.get("farbe", "0")) == "1"
    orientation = str(form.get("ausrichtung", "auto"))
    if orientation not in ORIENTATION_LABELS:
        return fail("Ungültige Ausrichtung.")
    scaling = str(form.get("skalierung", "fit"))
    if scaling not in ("fit", "actual"):
        return fail("Ungültige Skalierung.")
    duplex = str(form.get("duplex", "none"))
    if duplex not in DUPLEX_LABELS:
        return fail("Ungültige Duplex-Einstellung.")
    if duplex != "none" and fmt not in settings.duplex_formats:
        return fail(f"Beidseitiger Druck ist für {fmt} nicht möglich.")
    try:
        copies = int(str(form.get("kopien", "1")))
    except ValueError:
        return fail("Ungültige Kopienanzahl.")
    if not 1 <= copies <= settings.max_copies:
        return fail(f"Kopien: 1 bis {settings.max_copies}.")
    preset = str(form.get("preset", "")) or None
    if preset and preset not in PRESET_IDS:
        preset = None
    page_range_raw = clean_text(str(form.get("seiten", "")), 200)

    upload_file = form.get("datei")
    if not isinstance(upload_file, UploadFile) or not upload_file.filename:
        return fail("Bitte eine PDF-Datei auswählen.")
    if (upload_file.content_type or "").split(";")[0].strip().lower() not in PDF_MIME_TYPES:
        return fail("Nur PDF-Dateien sind erlaubt.")

    # --- Rate-Limit (persistiert in der DB, gilt pro IP, Name und Sitzung)
    if settings.jobs_per_hour > 0:
        n = db.recent_job_count(ago_iso(hours=1), ip, user["name"], user["sid"])
        if n >= settings.jobs_per_hour:
            return fail(f"Zu viele Aufträge (max. {settings.jobs_per_hour} pro Stunde). Bitte später erneut.", 429)

    # --- Datei speichern (in Blöcken, mit Größenlimit)
    job_id = uuid.uuid4().hex
    tmp = settings.files_dir / f"{job_id}.part"
    size = 0
    try:
        with tmp.open("wb") as out:
            first = True
            while chunk := await upload_file.read(1024 * 1024):
                if first:
                    check_magic(chunk)
                    first = False
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise Rejected(f"Datei zu groß (max. {settings.max_upload_mb} MB).")
                out.write(chunk)
        if size == 0:
            raise Rejected("Die Datei ist leer.")

        pages = await run_in_threadpool(count_pages, tmp)
        if pages > settings.max_pages:
            raise Rejected(f"Zu viele Seiten ({pages}, max. {settings.max_pages}).")
        page_range, selected = parse_page_range(page_range_raw, pages)
        print_pages = selected * copies
        if settings.pages_per_day > 0:
            used = db.recent_page_count(ago_iso(hours=24), user["name"], user["sid"])
            if used + print_pages > settings.pages_per_day:
                raise Rejected(
                    f"Tageslimit erreicht: {used} von {settings.pages_per_day} Seiten in den letzten 24 h verbraucht."
                )
        tmp.rename(job_file(settings, job_id))
    except Rejected as e:
        tmp.unlink(missing_ok=True)
        return fail(str(e))
    except Exception:
        tmp.unlink(missing_ok=True)
        log.exception("Upload fehlgeschlagen")
        return fail("Interner Fehler beim Speichern der Datei.", 500)

    filename = clean_text(Path(upload_file.filename.replace("\\", "/")).name, 120) or "dokument.pdf"
    ts = now_iso()
    db.insert_job(
        {
            "id": job_id,
            "filename": filename,
            "size_bytes": size,
            "pages": pages,
            "print_pages": print_pages,
            "sheets": sheets_needed(selected, duplex, copies),
            "format": fmt,
            "color": int(color),
            "duplex": duplex,
            "orientation": orientation,
            "scaling": scaling,
            "copies": copies,
            "page_range": page_range,
            "preset": preset,
            "user_name": user["name"],
            "session_id": user["sid"],
            "ip": ip,
            "status": "queued",
            "error": None,
            "created_at": ts,
            "queued_at": ts,
        }
    )
    log.info(
        "Auftrag %s von %s (%s): %s, %d S., %s %s %s %dx",
        job_id, user["name"], ip, filename, print_pages, fmt, "farbe" if color else "sw", duplex, copies,
    )
    msg = f"„{filename}“ ist in der Warteschlange ({print_pages} Seite{'n' if print_pages != 1 else ''})."
    if xhr:
        return JSONResponse({"ok": True, "message": msg, "id": job_id})
    flash(request, msg)
    return RedirectResponse("/", 303)


@app.post("/jobs/{job_id}/cancel")
async def cancel_own(request: Request, job_id: str):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", 303)
    form = await request.form()
    check_csrf(request, form.get("csrf"))
    row = db.get_job(job_id)
    if not row or row["session_id"] != user["sid"]:
        raise HTTPException(404, "Auftrag nicht gefunden.")
    if db.transition(job_id, ("queued",), "cancelled", "Vom Nutzer abgebrochen."):
        delete_job_file(settings, job_id)
        flash(request, f"„{row['filename']}“ wurde abgebrochen.")
    else:
        flash(request, "Der Auftrag wird bereits gedruckt und kann nicht mehr abgebrochen werden.", "error")
    return RedirectResponse("/", 303)


# ---------------------------------------------------------------- Agent-API

def require_agent(request: Request) -> None:
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    if not token or not hmac.compare_digest(token.encode(), settings.agent_token.encode()):
        log.warning("Agent-API: ungültiges Token von %s", client_ip(request))
        raise HTTPException(401, "Ungültiges Agent-Token.")


@app.post("/api/agent/claim")
def agent_claim(request: Request):
    require_agent(request)
    db.touch_agent(ip=client_ip(request))
    row = db.claim_next()
    if not row:
        return Response(status_code=204)
    log.info("Agent holt Auftrag %s ab", row["id"])
    job = job_public(row)
    job["nutzer"] = row["user_name"]
    job["download_url"] = f"/api/agent/jobs/{row['id']}/file"
    return job


@app.get("/api/agent/jobs/{job_id}/file")
def agent_file(request: Request, job_id: str):
    require_agent(request)
    row = db.get_job(job_id)
    if not row or row["status"] != "printing" or not row["has_file"]:
        raise HTTPException(404, "Auftrag nicht im Druck oder Datei bereits gelöscht.")
    return FileResponse(job_file(settings, job_id), media_type="application/pdf", filename=f"{job_id}.pdf")


@app.post("/api/agent/jobs/{job_id}/status")
async def agent_report(request: Request, job_id: str):
    require_agent(request)
    body = await request.json()
    status = body.get("status")
    if status not in ("done", "error"):
        raise HTTPException(400, "status muss 'done' oder 'error' sein.")
    error = clean_text(str(body.get("fehler") or ""), 500) or None
    if status == "error" and not error:
        error = "Unbekannter Druckfehler."
    if not db.transition(job_id, ("printing",), status, error if status == "error" else None):
        # z. B. vom Admin abgebrochen oder per Timeout auf Fehler gesetzt
        raise HTTPException(409, "Auftrag ist nicht (mehr) im Status 'printing'.")
    if status == "done":
        delete_job_file(settings, job_id)
    log.info("Agent meldet %s für %s%s", status, job_id, f": {error}" if error else "")
    return {"ok": True}


@app.post("/api/agent/heartbeat")
async def agent_heartbeat(request: Request):
    require_agent(request)
    raw = await request.body()
    if len(raw) > 16384:
        raise HTTPException(413, "Heartbeat zu groß.")
    body = json.loads(raw or b"{}")
    allowed = {k: body[k] for k in ("version", "hostname", "method", "printer", "printer_status", "message") if k in body}
    db.touch_agent(ip=client_ip(request), heartbeat_at=now_iso(), **allowed)
    return {"ok": True, "server_time": now_iso()}


# ---------------------------------------------------------------- Admin

def require_admin(request: Request) -> None:
    if not is_admin(request):
        raise HTTPException(403, "Nur für Admins.")


@app.get("/admin/login")
def admin_login_form(request: Request):
    if is_admin(request):
        return RedirectResponse("/admin", 303)
    return render(request, "admin_login.html")


@app.post("/admin/login")
async def admin_login(request: Request):
    form = await request.form()
    check_csrf(request, form.get("csrf"))
    ip = client_ip(request)
    key = "admin:" + ip
    if login_limiter.blocked(key):
        flash(request, "Zu viele Fehlversuche.", "error")
        return RedirectResponse("/admin/login", 303)
    pw = str(form.get("password", ""))
    if not hmac.compare_digest(pw.encode(), settings.admin_password.encode()):
        login_limiter.fail(key)
        log.warning("Falsches Admin-Passwort von %s", ip)
        flash(request, "Passwort falsch.", "error")
        return RedirectResponse("/admin/login", 303)
    login_limiter.reset(key)
    request.session["av"] = _version(settings.admin_password)
    return RedirectResponse("/admin", 303)


@app.post("/admin/logout")
async def admin_logout(request: Request):
    form = await request.form()
    check_csrf(request, form.get("csrf"))
    request.session.pop("av", None)
    return RedirectResponse("/admin/login", 303)


@app.get("/admin")
def admin(request: Request):
    if not is_admin(request):
        return RedirectResponse("/admin/login", 303)
    recent, _ = db.log_entries(15, 0)
    return render(
        request,
        "admin.html",
        agent=agent_status(),
        queue=db.active_jobs(),
        recent=recent,
        stats=db.stats_since(ago_iso(days=30)),
        marker_color=marker_color,
        marker_label=marker_label,
        printer_state_de=printer_state_de,
        printer_reasons_de=printer_reasons_de,
        settings=settings,
    )


@app.get("/admin/log")
def admin_log(request: Request, page: int = 1, user: str = ""):
    if not is_admin(request):
        return RedirectResponse("/admin/login", 303)
    per_page = 50
    page = max(page, 1)
    rows, total = db.log_entries(per_page, (page - 1) * per_page, user.strip())
    return render(
        request, "admin_log.html", rows=rows, total=total, page=page,
        pages=max(1, -(-total // per_page)), user=user,
    )


@app.get("/admin/log.csv")
def admin_log_csv(request: Request):
    require_admin(request)
    cols = [
        "id", "created_at", "finished_at", "user_name", "ip", "filename", "status", "error", "format",
        "color", "duplex", "orientation", "scaling", "copies", "page_range", "pages", "print_pages", "sheets", "preset", "size_bytes",
    ]

    def gen():
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(cols)
        yield "﻿" + buf.getvalue()  # BOM, damit Excel UTF-8 erkennt
        for row in db.all_log_entries():
            buf.seek(0)
            buf.truncate()
            w.writerow([row[c] for c in cols])
            yield buf.getvalue()

    return StreamingResponse(
        gen(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=druck-log.csv"},
    )


@app.post("/admin/jobs/{job_id}/cancel")
async def admin_cancel(request: Request, job_id: str):
    require_admin(request)
    form = await request.form()
    check_csrf(request, form.get("csrf"))
    if db.transition(job_id, ("queued", "printing"), "cancelled", "Vom Admin abgebrochen."):
        delete_job_file(settings, job_id)
        flash(request, "Auftrag abgebrochen. (Bereits an den Drucker gesendete Seiten ggf. am Gerät stoppen.)")
    else:
        flash(request, "Auftrag konnte nicht abgebrochen werden (Status geändert?).", "error")
    back = str(form.get("back", ""))
    return RedirectResponse(back if back.startswith("/admin") else "/admin", 303)


@app.post("/admin/jobs/{job_id}/requeue")
async def admin_requeue(request: Request, job_id: str):
    require_admin(request)
    form = await request.form()
    check_csrf(request, form.get("csrf"))
    if db.requeue(job_id):
        flash(request, "Auftrag erneut in die Warteschlange gestellt.")
    else:
        flash(request, "Erneutes Drucken nicht möglich (Datei bereits gelöscht?).", "error")
    return RedirectResponse("/admin", 303)
