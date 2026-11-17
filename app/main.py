"""HandyPay · Espace d'équipe — l'application FastAPI.

Lancer en local :  uvicorn app.main:app --reload
Production      :  cf. Dockerfile et README.

LE GARDIEN (middleware `guard`) :
- `/api/workspace/github` et `/api/workspace/ia/*` s'authentifient eux-mêmes
  (signature GitHub, jeton de l'IA) : aucune session, aucun cookie ;
- toute requête `/api/*` qui MODIFIE (POST, PUT, PATCH, DELETE) doit porter
  l'en-tête `X-CSRF-Token` égal au cookie `hp_csrf` (double soumission) ; une
  fois connecté, ce jeton doit aussi être celui de la session ;
- `/api/ws/*` exige une session valide, relue en base à chaque requête
  (compte actif, même version de session) : sinon 401.
Chaque réponse porte des en-têtes de sécurité ; la page a sa propre CSP.

LES JOURNAUX ne contiennent jamais de numéro : la partie « ?… » des adresses
est retirée des journaux d'accès et toute suite de chiffres qui ressemble à un
numéro est masquée (cf. `RedactFilter`).
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app import auth, connect, digest, evening, followups, sales, security
from app import workspace as ws
from app.config import get_settings

logger = logging.getLogger("handypay")

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}
_API_CSP = "default-src 'none'; frame-ancestors 'none'"


# ═══════════════════════════════════════════════════════════════════
# Journaux sans numéro de téléphone
# ═══════════════════════════════════════════════════════════════════

# 8 à 15 chiffres, séparés au plus par une espace ou un tiret ; jamais collés à
# une lettre, un point, deux-points ou un tiret (segments d'UUID, adresses IP,
# heures). Les dates AAAA-MM-JJ sont laissées telles quelles.
_PHONE_RE = re.compile(r"(?<![\w.:\-])\+?\d(?:[ \-]?\d){7,14}(?![\w.:\-])")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def redact(text: str) -> str:
    return _PHONE_RE.sub(lambda m: m.group(0) if _DATE_RE.match(m.group(0)) else "[numero-masque]", text)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) >= 3:
            # Le formateur d'uvicorn relit ces 5 arguments : on les garde, en
            # retirant la partie « ?… » de l'adresse et tout numéro du chemin.
            args = list(record.args)
            args[2] = redact(str(args[2]).split("?", 1)[0])
            record.args = tuple(args)
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        clean = redact(message)
        if clean != message:
            record.msg, record.args = clean, ()
        if record.exc_info and not getattr(record, "exc_text", None):
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        return True


def install_log_filters() -> None:
    flt = RedactFilter()
    app_logger = logging.getLogger("handypay")
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s %(message)s"))
        app_logger.addHandler(handler)
        app_logger.setLevel(logging.INFO)
        app_logger.propagate = False
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access", "handypay"):
        lg = logging.getLogger(name)
        if flt not in lg.filters:
            lg.addFilter(flt)
        for h in lg.handlers:
            if not any(isinstance(f, RedactFilter) for f in h.filters):
                h.addFilter(flt)


# ═══════════════════════════════════════════════════════════════════
# Application
# ═══════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(_app: FastAPI):
    install_log_filters()
    cfg = get_settings()
    logger.info("Espace d'équipe HandyPay : mode %s", "local (.data/)" if cfg.local else "Supabase")
    try:
        digest.start_scheduler()
    except Exception:  # noqa: BLE001 - l'e-mail du matin ne bloque jamais le démarrage
        logger.warning("Planificateur de l'e-mail du matin non démarré")
    yield


app = FastAPI(title=ws.APP_NAME, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def _deny(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


def _secure(response, path: str):
    for key, value in _HEADERS.items():
        response.headers.setdefault(key, value)
    response.headers["Content-Security-Policy"] = ws.WORKSPACE_CSP if path == ws.PAGE_PATH else _API_CSP
    if get_settings().cookie_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
    return response


@app.middleware("http")
async def guard(request: Request, call_next):
    path, method = request.url.path, request.method
    if connect.is_external_path(path):
        return _secure(await call_next(request), path)
    if path.startswith("/api/"):
        header = request.headers.get(security.CSRF_HEADER, "")
        if method not in SAFE_METHODS and not security.same_token(header, request.cookies.get(security.CSRF_COOKIE, "")):
            return _secure(_deny(403, "Jeton CSRF absent ou invalide : recharge la page."), path)
        needs_user = path.startswith(ws.API + "/") or path == "/api/auth/password"
        if needs_user or path == "/api/auth/logout":
            try:
                user = await run_in_threadpool(auth.identify, request)
            except Exception:  # noqa: BLE001 - base injoignable
                return _secure(_deny(503, "Espace d'équipe momentanément indisponible."), path)
            if needs_user and user is None:
                return _secure(_deny(401, "Session requise."), path)
            if user is not None and method not in SAFE_METHODS and not security.same_token(header, user["csrf"]):
                return _secure(_deny(403, "Jeton CSRF invalide pour cette session : recharge la page."), path)
            request.state.user = user
    return _secure(await call_next(request), path)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True, "mode": "local" if get_settings().local else "supabase"}


for _router in (auth.router, ws.router, sales.router, followups.router, evening.router, digest.router, connect.router):
    app.include_router(_router)
