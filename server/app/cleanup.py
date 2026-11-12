"""Aufräumen: verfallene Aufträge, hängende Druckaufträge, Dateien, altes Log."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from . import db
from .config import Settings
from .timeutil import ago_iso, now_iso

log = logging.getLogger("druckdienst.cleanup")


def job_file(settings: Settings, job_id: str) -> Path:
    return settings.files_dir / f"{job_id}.pdf"


def delete_job_file(settings: Settings, job_id: str) -> None:
    try:
        job_file(settings, job_id).unlink(missing_ok=True)
    finally:
        db.mark_file_deleted(job_id)


def run_cleanup(settings: Settings) -> None:
    expire_before = ago_iso(hours=settings.job_expire_hours)
    with db.connect() as c:
        expired = c.execute(
            "UPDATE jobs SET status='expired', finished_at=?, error=? "
            "WHERE status='queued' AND queued_at < ? RETURNING id",
            (now_iso(), f"Nicht innerhalb von {settings.job_expire_hours} h gedruckt – verworfen.", expire_before),
        ).fetchall()
        hung = c.execute(
            "UPDATE jobs SET status='error', finished_at=?, error=? "
            "WHERE status='printing' AND claimed_at < ? RETURNING id",
            (
                now_iso(),
                "Keine Rückmeldung vom Druck-PC (Zeitüberschreitung). Bitte am Drucker prüfen, ob gedruckt wurde.",
                ago_iso(minutes=settings.printing_timeout_min),
            ),
        ).fetchall()
        to_delete = c.execute(
            "SELECT id FROM jobs WHERE has_file = 1 AND ("
            " status IN ('done','cancelled','expired') OR (status = 'error' AND queued_at < ?))",
            (expire_before,),
        ).fetchall()
        purged = c.execute(
            "DELETE FROM jobs WHERE created_at < ? AND status NOT IN ('queued','printing') RETURNING id, has_file",
            (ago_iso(days=settings.log_retention_days),),
        ).fetchall()

    for row in to_delete:
        delete_job_file(settings, row["id"])
    for row in purged:
        if row["has_file"]:
            job_file(settings, row["id"]).unlink(missing_ok=True)

    # Waisen: Dateien ohne zugehörigen Auftrag (z. B. abgebrochener Upload, Absturz)
    cutoff = time.time() - 3600
    for f in settings.files_dir.iterdir():
        if f.stat().st_mtime > cutoff:
            continue
        job_id = f.name.split(".")[0]
        row = db.get_job(job_id)
        if row is None or not row["has_file"]:
            f.unlink(missing_ok=True)

    if expired or hung or to_delete or purged:
        log.info(
            "Cleanup: %d verfallen, %d Zeitüberschreitung, %d Dateien gelöscht, %d Logeinträge entfernt",
            len(expired), len(hung), len(to_delete), len(purged),
        )
