"""Konfiguration aus Umgebungsvariablen (.env bzw. Coolify-Environment)."""
from __future__ import annotations

import json
import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "ja", "on")


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


class Settings:
    def __init__(self) -> None:
        self.site_title = os.getenv("SITE_TITLE", "Druckdienst")
        self.access_code = os.getenv("ACCESS_CODE", "")
        self.admin_password = os.getenv("ADMIN_PASSWORD", "")
        self.agent_token = os.getenv("AGENT_TOKEN", "")
        self.secret_key = os.getenv("SECRET_KEY", "")

        self.data_dir = Path(os.getenv("DATA_DIR", "/data"))
        self.presets_file = Path(
            os.getenv("PRESETS_FILE", str(Path(__file__).resolve().parent.parent / "presets.json"))
        )

        self.max_upload_mb = _int("MAX_UPLOAD_MB", 50)
        self.max_pages = _int("MAX_PAGES", 300)
        self.max_copies = _int("MAX_COPIES", 20)
        self.jobs_per_hour = _int("RATE_LIMIT_JOBS_PER_HOUR", 20)
        self.pages_per_day = _int("MAX_PAGES_PER_DAY", 0)  # 0 = unbegrenzt
        self.login_max_failures = _int("LOGIN_MAX_FAILURES", 8)
        self.login_window_min = _int("LOGIN_WINDOW_MIN", 15)

        self.job_expire_hours = _int("JOB_EXPIRE_HOURS", 12)
        self.printing_timeout_min = _int("PRINTING_TIMEOUT_MIN", 15)
        self.log_retention_days = _int("LOG_RETENTION_DAYS", 365)
        self.agent_offline_after_s = _int("AGENT_OFFLINE_AFTER_S", 60)

        self.formats = _list("FORMATS", "A4,A3")
        self.duplex_formats = _list("DUPLEX_FORMATS", "A4,A3")
        self.cookie_secure = _bool("COOKIE_SECURE", True)
        self.session_days = _int("SESSION_DAYS", 30)
        self.display_tz = os.getenv("DISPLAY_TZ", "Europe/Berlin")

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "druckdienst.db"

    def validate(self) -> None:
        missing = [
            n
            for n, v in (
                ("ACCESS_CODE", self.access_code),
                ("ADMIN_PASSWORD", self.admin_password),
                ("AGENT_TOKEN", self.agent_token),
                ("SECRET_KEY", self.secret_key),
            )
            if not v
        ]
        if missing:
            raise RuntimeError("Fehlende Pflicht-Umgebungsvariablen: " + ", ".join(missing))
        if len(self.agent_token) < 24:
            raise RuntimeError("AGENT_TOKEN muss mindestens 24 Zeichen lang sein.")
        if len(self.secret_key) < 32:
            raise RuntimeError("SECRET_KEY muss mindestens 32 Zeichen lang sein.")
        if not self.formats:
            raise RuntimeError("FORMATS darf nicht leer sein.")


def load_presets(settings: Settings) -> list[dict]:
    """Lädt presets.json und verwirft ungültige Einträge (mit klarer Fehlermeldung)."""
    if not settings.presets_file.exists():
        return []
    data = json.loads(settings.presets_file.read_text(encoding="utf-8"))
    presets = []
    for p in data:
        pid = str(p["id"])
        fmt = p.get("format", settings.formats[0])
        duplex = p.get("duplex", "none")
        if fmt not in settings.formats:
            raise RuntimeError(f"Preset {pid}: Format {fmt} ist nicht in FORMATS freigegeben.")
        if duplex not in ("none", "long", "short"):
            raise RuntimeError(f"Preset {pid}: duplex muss none/long/short sein.")
        presets.append(
            {
                "id": pid,
                "name": str(p["name"]),
                "format": fmt,
                "farbe": bool(p.get("farbe", False)),
                "duplex": duplex,
                "kopien": int(p.get("kopien", 1)),
                "hinweis": str(p.get("hinweis", "")),
            }
        )
    return presets


settings = Settings()
