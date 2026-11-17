"""Client de base de données : Supabase en production, faux client JSON en local."""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, Optional

from fastapi import HTTPException

from app.config import get_settings

logger = logging.getLogger("handypay.db")


@functools.lru_cache(maxsize=1)
def _supabase(url: str, key: str):
    from supabase import create_client

    return create_client(url, key)


@functools.lru_cache(maxsize=1)
def _local(path: str):
    from pathlib import Path

    from app.localdb import LocalClient

    return LocalClient(Path(path))


def client_or_none():
    cfg = get_settings()
    if cfg.local:
        return _local(str(cfg.data_dir))
    if not cfg.supabase_service_key:
        return None
    try:
        return _supabase(cfg.supabase_url, cfg.supabase_service_key)
    except Exception as exc:  # noqa: BLE001 - environnement
        logger.warning("Client Supabase indisponible (%s)", type(exc).__name__)
        return None


def db():
    client = client_or_none()
    if client is None:
        raise HTTPException(status_code=503, detail="Base indisponible : SUPABASE_SERVICE_KEY n'est pas configurée.")
    return client


def run(query, what: str) -> list[dict]:
    """Exécute une requête ; une panne devient 503, jamais un faux vide."""
    try:
        res = query.execute()
    except Exception as exc:  # noqa: BLE001 - frontière SDK/réseau
        logger.warning("Base : %s impossible (%s)", what, type(exc).__name__)
        raise HTTPException(
            status_code=503,
            detail="Espace d'équipe momentanément indisponible. Vérifie que sql/schema.sql a été exécuté.",
        )
    data = getattr(res, "data", None)
    if isinstance(data, dict):
        return [data]
    return [r for r in (data or []) if isinstance(r, dict)]


def fetch_all(build: Callable[[], Any], what: str, page: int = 1000, cap: int = 50000) -> list[dict]:
    """Lit toutes les lignes, page par page.

    Supabase plafonne une réponse à 1 000 lignes : au-delà, `limit()` ne suffit
    pas. `build` fabrique une requête NEUVE (filtres et tri compris) à chaque page.
    """
    out: list[dict] = []
    start = 0
    while start < cap:
        rows = run(build().range(start, start + page - 1), what)
        out.extend(rows)
        if len(rows) < page:
            break
        start += page
    return out


def count(query, what: str) -> int:
    try:
        res = query.execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Base : %s impossible (%s)", what, type(exc).__name__)
        raise HTTPException(status_code=503, detail="Espace d'équipe momentanément indisponible.")
    value: Optional[int] = getattr(res, "count", None)
    return int(value or 0)
