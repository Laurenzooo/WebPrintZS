"""Zeitstempel: intern immer UTC als ISO-String (sortierbar), Anzeige in lokaler Zeit."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

FMT = "%Y-%m-%dT%H:%M:%SZ"


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(FMT)


def now_iso() -> str:
    return iso(now())


def ago_iso(**kwargs) -> str:
    return iso(now() - timedelta(**kwargs))


def parse(s: str) -> datetime:
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)


def fmt_local(s: str | None, tz: str, fmt: str = "%d.%m.%Y %H:%M") -> str:
    if not s:
        return "–"
    return parse(s).astimezone(ZoneInfo(tz)).strftime(fmt)


def seconds_since(s: str | None) -> float | None:
    if not s:
        return None
    return (now() - parse(s)).total_seconds()
