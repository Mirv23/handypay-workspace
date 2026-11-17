"""Heure de référence : America/Port-au-Prince.

« Aujourd'hui », pour les ventes, les relances, le tableau du soir et l'e-mail
du matin, c'est la date à Port-au-Prince, jamais la date UTC (à 20 h en Haïti,
il est déjà demain en UTC). Le paquet `tzdata` fournit le fuseau sous Windows ;
à défaut, la règle d'Haïti est appliquée à la main.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

TZ_NAME = "America/Port-au-Prince"


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def haiti_now(now_utc: Optional[datetime] = None) -> datetime:
    now_utc = now_utc or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return now_utc.astimezone(ZoneInfo(TZ_NAME))
    except Exception:  # noqa: BLE001 - fuseau absent du système
        # Haïti suit l'heure d'été nord-américaine : UTC-4 du 2e dimanche de
        # mars (7 h UTC) au 1er dimanche de novembre (6 h UTC), UTC-5 sinon.
        year = now_utc.year
        start = datetime.combine(_nth_sunday(year, 3, 2), datetime.min.time(), timezone.utc) + timedelta(hours=7)
        end = datetime.combine(_nth_sunday(year, 11, 1), datetime.min.time(), timezone.utc) + timedelta(hours=6)
        offset = -4 if start <= now_utc < end else -5
        return now_utc.astimezone(timezone(timedelta(hours=offset)))


def today() -> date:
    return haiti_now().date()


def now_iso() -> str:
    # Millisecondes : le navigateur relit cet horodatage pour son « since ».
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse_day(value, default: Optional[date] = None) -> date:
    """`AAAA-MM-JJ` → date ; vide → `default` (aujourd'hui à Port-au-Prince)."""
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    if not text:
        return default or today()
    return date.fromisoformat(text)


def day_of(timestamp: str) -> str:
    """Jour à Port-au-Prince d'un horodatage ISO (UTC en base)."""
    try:
        dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return str(timestamp)[:10]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return haiti_now(dt.astimezone(timezone.utc)).date().isoformat()
