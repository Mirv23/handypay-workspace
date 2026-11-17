"""Téléphone : application installable (PWA) et mode compteur.

PUBLIC, SANS SESSION (rien de personnel, aucun cookie posé) :
- `/manifest.webmanifest` : nom, couleurs, icônes de l'application ;
- `/sw.js` : le service worker. Il ne met en cache QUE la coquille statique
  (la page « / », le manifeste, les icônes) ; jamais une réponse de `/api/*` ;
- `/icons/<nom>.png` : liste fermée de fichiers (aucun autre chemin n'est servi).

RÉSERVÉ AUX PERSONNES CONNECTÉES :
- `/compteur` : la page plein écran que Mirv filme chaque matin (« Défi 3 000
  cartes »). Sans session : renvoi vers la connexion (`/#/compteur`), qui y
  revient une fois connecté ;
- `GET /api/ws/compteur` : ses chiffres, en lecture seule. Liste blanche de
  champs : cumul, jour de la campagne, objectif du jour, jalons, marge. Aucun
  nom ni numéro, ni de client ni de l'équipe (le classement reste dans Ventes).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app import auth, sales, timeutil
from app import workspace as ws

router = APIRouter()
_STATIC = Path(__file__).parent / "static"
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

COUNTER_PATH = "/compteur"
SW_PATH = "/sw.js"
MANIFEST_PATH = "/manifest.webmanifest"
# Le service worker n'a besoin que de relire la coquille sur la même origine.
SW_CSP = "default-src 'none'; connect-src 'self'"
ICONS = ("icon-192.png", "icon-512.png", "icon-maskable-512.png", "apple-touch-icon.png", "favicon-32.png")

# Lus une fois : de petits fichiers figés, livrés avec le code.
_ICON_BYTES = {name: (_STATIC / "icons" / name).read_bytes() for name in ICONS}
_MANIFEST = (_STATIC / "manifest.webmanifest").read_bytes()
_SW = (_STATIC / "sw.js").read_bytes()


# ═══════════════════════════════════════════════════════════════════
# Application installable
# ═══════════════════════════════════════════════════════════════════

@router.get(MANIFEST_PATH, include_in_schema=False)
def manifest():
    return Response(_MANIFEST, media_type="application/manifest+json",
                    headers={"Cache-Control": "public, max-age=3600"})


@router.get(SW_PATH, include_in_schema=False)
def service_worker():
    # `no-cache` : le navigateur revalide le script à chaque visite (mise à jour
    # immédiate). Servi à la racine, sa portée est déjà « / » : pas besoin de
    # l'en-tête Service-Worker-Allowed.
    return Response(_SW, media_type="text/javascript; charset=utf-8", headers={"Cache-Control": "no-cache"})


@router.get("/icons/{name}", include_in_schema=False)
def icon(name: str):
    data = _ICON_BYTES.get(name)
    if data is None:
        return JSONResponse(status_code=404, content={"detail": "Introuvable."})
    return Response(data, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


# ═══════════════════════════════════════════════════════════════════
# Mode compteur
# ═══════════════════════════════════════════════════════════════════

def counter_payload(client) -> dict:
    """Les chiffres du compteur : une liste blanche, rien de personnel."""
    # team=[] : le calcul ne lit même pas les comptes de l'équipe.
    st = sales.compute_stats(client, timeutil.today(), team=[])
    t, c = st["today"], st["campaign"]
    nm = c.get("next_milestone")
    return {
        "day": st["day"],
        "campaign": {
            "start": c["start"], "end": c["end"], "day": c["elapsed"], "days": c["days"],
            "cumul": c["cumul"], "target": c["target"], "expected": c["expected"], "gap": c["gap"],
        },
        "today": {"cards": t["cards"], "target": t["target"]},
        "next_milestone": None if not nm else {
            "date": nm["date"], "target": nm["target"], "remaining": nm["remaining"],
            "days_left": nm["days_left"], "per_day_needed": nm["per_day_needed"],
        },
        "milestones": [{"date": m["date"], "target": m["target"], "reached": bool(m["reached"])} for m in c["milestones"]],
        "margin": {"total": c["margin"], "today": t["margin"], "currency": c["currency"]},
        "updated_at": timeutil.now_iso(),
    }


@router.get(ws.API + "/compteur")
def counter_data(request: Request):
    client = ws._db()
    ws._me(request, client)
    return counter_payload(client)


@router.get(COUNTER_PATH, include_in_schema=False)
def counter_page(request: Request):
    try:
        user = auth.identify(request)
    except Exception:  # noqa: BLE001 - base injoignable
        return JSONResponse(status_code=503, content={"detail": "Espace d'équipe momentanément indisponible."})
    if user is None or user.get("username") == ws.AI_USER:
        # La page de connexion revient ici une fois connecté.
        return RedirectResponse("/#/compteur", status_code=303)
    try:
        initial = counter_payload(ws._db())
    except Exception:  # noqa: BLE001 - la page charge quand même, puis réessaie
        initial = None
    return _templates.TemplateResponse(request, "compteur.html",
                                       {"app_name": ws.APP_NAME, "initial": initial, "csp_nonce": ws.page_nonce(request)})
