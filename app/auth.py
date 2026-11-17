"""Comptes de l'espace HandyPay : connexion, session, mot de passe, verrouillage.

UN COMPTE = UNE PERSONNE. Identifiant de connexion : son nom d'utilisateur
(« mirv ») OU son numéro WhatsApp (« 3412 3456 », « +509 3412-3456 »). Le nom
d'utilisateur est ensuite la clé de la personne partout (tâches, ventes,
relances, mentions @mirv).

Rôles : `responsable` (tout, dont les comptes), `membre` (crée et modifie),
`observateur` (lit, commente et suit des tâches ; n'écrit ni vente ni relance).

Mots de passe : jamais choisis à la création. Le serveur en génère un, le montre
UNE fois au responsable (avec un lien wa.me pré-rempli pour le transmettre), et
n'en garde que l'empreinte scrypt. Chacun peut ensuite changer le sien.

Verrouillage : 8 échecs en 15 minutes bloquent l'identifiant 15 minutes (en base
pour un compte existant ; en mémoire pour un identifiant inconnu, qui reçoit
exactement les mêmes réponses : on ne devine pas qui a un compte).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app import security
from app.config import get_settings
from app.db import db, run

logger = logging.getLogger("handypay.auth")
router = APIRouter()

T_ACCOUNTS = "workspace_accounts"
T_MEMBERS = "workspace_members"
ROLES = ("responsable", "membre", "observateur")
RESERVED = {"ia", "github", "handypay", "admin", "root", "system"}
# Colonnes lisibles d'un compte : JAMAIS l'empreinte du mot de passe.
PUBLIC_COLS = "username,whatsapp,display_name,role,active,session_version,last_login_at,created_at"

LOCK_THRESHOLD = 8
LOCK_WINDOW_S = 15 * 60
LOCK_DURATION_S = 15 * 60
IP_THRESHOLD = 60

_mem_lock = threading.Lock()
_mem_failures: dict[str, list[float]] = {}
_mem_locked: dict[str, float] = {}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════════════
# Comptes
# ═══════════════════════════════════════════════════════════════════

def find_account(client, identifier: str, with_hash: bool = False) -> Optional[dict]:
    cols = PUBLIC_COLS + (",password_hash,failed_logins,first_failed_at,locked_until" if with_hash else "")
    if security.looks_like_phone(identifier):
        phone = security.normalize_phone(identifier)
        if not phone:
            return None
        rows = run(client.table(T_ACCOUNTS).select(cols).eq("whatsapp", phone).limit(1), "lecture du compte")
    else:
        name = security.clean_username(identifier)
        if not name:
            return None
        rows = run(client.table(T_ACCOUNTS).select(cols).eq("username", name).limit(1), "lecture du compte")
    return rows[0] if rows else None


def account_by_username(client, username: str) -> Optional[dict]:
    rows = run(client.table(T_ACCOUNTS).select(PUBLIC_COLS).eq("username", username).limit(1), "lecture du compte")
    return rows[0] if rows else None


def check_new_username(client, username: str) -> str:
    name = security.clean_username(username)
    if not name:
        raise HTTPException(status_code=400, detail="Nom d'utilisateur invalide : 2 à 40 caractères, lettres minuscules, chiffres, . _ -")
    if name in RESERVED:
        raise HTTPException(status_code=400, detail="Ce nom est réservé.")
    if account_by_username(client, name):
        raise HTTPException(status_code=409, detail="Ce nom d'utilisateur est déjà pris.")
    return name


def check_whatsapp(client, raw: str, exclude: str = "") -> Optional[str]:
    if not str(raw or "").strip():
        return None
    phone = security.normalize_phone(raw)
    if not phone:
        raise HTTPException(status_code=400, detail="Numéro WhatsApp invalide (8 chiffres haïtiens, ou +indicatif).")
    rows = run(client.table(T_ACCOUNTS).select("username").eq("whatsapp", phone).limit(1), "lecture du compte")
    if rows and rows[0].get("username") != exclude:
        raise HTTPException(status_code=409, detail="Ce numéro WhatsApp est déjà relié à un autre compte.")
    return phone


def create_account(client, *, username: str, display_name: str, role: str, whatsapp: Optional[str],
                   created_by: str, profile: Optional[dict] = None) -> tuple[dict, str]:
    """Crée le compte ET la fiche de membre ; renvoie (compte, mot de passe en clair, montré une fois)."""
    if role not in ROLES:
        raise HTTPException(status_code=400, detail="Rôle inconnu.")
    password = security.generate_password()
    row = {
        "username": username, "whatsapp": whatsapp, "display_name": " ".join(display_name.split())[:60] or username,
        "password_hash": security.hash_password(password), "role": role, "active": True,
        "session_version": 1, "created_by": created_by,
    }
    run(client.table(T_ACCOUNTS).insert(row), "création du compte")
    member = dict(profile or {}, username=username, added_by=created_by)
    existing = run(client.table(T_MEMBERS).select("username").eq("username", username).limit(1), "lecture du membre")
    if existing:
        run(client.table(T_MEMBERS).update({k: v for k, v in member.items() if k != "username"}).eq("username", username), "fiche du membre")
    else:
        run(client.table(T_MEMBERS).insert(member), "fiche du membre")
    account = account_by_username(client, username) or {k: v for k, v in row.items() if k != "password_hash"}
    return account, password


def reset_password(client, username: str) -> str:
    acc = account_by_username(client, username)
    if not acc:
        raise HTTPException(status_code=404, detail="Compte introuvable.")
    password = security.generate_password()
    run(client.table(T_ACCOUNTS).update({
        "password_hash": security.hash_password(password),
        "session_version": int(acc.get("session_version") or 1) + 1,
        "failed_logins": 0, "first_failed_at": None, "locked_until": None,
    }).eq("username", username), "nouveau mot de passe")
    return password


def invite_text(display_name: str, username: str, password: str, whatsapp: Optional[str]) -> str:
    url = get_settings().public_url
    ident = username + (f" (ou ton numéro {whatsapp})" if whatsapp else "")
    first = (display_name or username).split(" ")[0]
    return (f"Bonjour {first}, voici ton accès à l'espace d'équipe HandyPay : {url}\n"
            f"Identifiant : {ident}\nMot de passe : {password}\n"
            "Change-le dans « Mon profil » après ta première connexion.")


# ═══════════════════════════════════════════════════════════════════
# Session : lecture (appelée par le middleware) et émission
# ═══════════════════════════════════════════════════════════════════

def identify(request: Request) -> Optional[dict]:
    """Personne connectée d'après le cookie signé, relue en base (compte actif, même version)."""
    cfg = get_settings()
    payload = security.read_session(cfg.session_secret, request.cookies.get(security.SESSION_COOKIE, ""))
    if payload is None:
        return None
    client = db()
    acc = account_by_username(client, payload["u"])
    if not acc or acc.get("active") is False or int(acc.get("session_version") or 1) != int(payload.get("v") or 0):
        return None
    return {"username": acc["username"], "role": acc.get("role") if acc.get("role") in ROLES else "observateur",
            "display_name": acc.get("display_name") or acc["username"], "csrf": payload["c"]}


def _cookie_args() -> dict:
    cfg = get_settings()
    return {"secure": cfg.cookie_secure, "samesite": "lax", "path": "/"}


def set_csrf_cookie(response: Response, token: Optional[str] = None) -> str:
    token = token or security.new_csrf_token()
    response.set_cookie(security.CSRF_COOKIE, token, httponly=False, max_age=get_settings().session_max_age_s, **_cookie_args())
    return token


def issue_session(response: Response, username: str, version: int) -> None:
    cfg = get_settings()
    csrf = security.new_csrf_token()
    cookie = security.sign_session(cfg.session_secret, username, version, cfg.session_max_age_s, csrf)
    response.set_cookie(security.SESSION_COOKIE, cookie, httponly=True, max_age=cfg.session_max_age_s, **_cookie_args())
    set_csrf_cookie(response, csrf)


def clear_session(response: Response) -> None:
    response.delete_cookie(security.SESSION_COOKIE, **_cookie_args())
    set_csrf_cookie(response)


# ═══════════════════════════════════════════════════════════════════
# Verrouillage
# ═══════════════════════════════════════════════════════════════════

def _mem_failed(key: str) -> bool:
    """Échec en mémoire ; True si la clé vient d'être bloquée."""
    now = time.time()
    with _mem_lock:
        fails = [t for t in _mem_failures.get(key, []) if now - t < LOCK_WINDOW_S] + [now]
        if len(_mem_failures) > 10000:
            _mem_failures.clear()
        _mem_failures[key] = fails
        if len(fails) >= (IP_THRESHOLD if key.startswith("ip:") else LOCK_THRESHOLD):
            _mem_locked[key] = now + LOCK_DURATION_S
            _mem_failures[key] = []
            return True
    return False


def _mem_is_locked(key: str) -> bool:
    with _mem_lock:
        until = _mem_locked.get(key, 0)
        if until and until < time.time():
            _mem_locked.pop(key, None)
            return False
        return bool(until)


def _record_failure(client, acc: dict) -> None:
    now = _utcnow()
    first = _parse(acc.get("first_failed_at"))
    count = int(acc.get("failed_logins") or 0)
    if not first or (now - first).total_seconds() >= LOCK_WINDOW_S:
        first, count = now, 0
    count += 1
    changes = {"failed_logins": count, "first_failed_at": _iso(first)}
    if count >= LOCK_THRESHOLD:
        changes.update(failed_logins=0, first_failed_at=None, locked_until=_iso(now + timedelta(seconds=LOCK_DURATION_S)))
    try:
        run(client.table(T_ACCOUNTS).update(changes).eq("username", acc["username"]), "échec de connexion")
    except HTTPException:
        pass


# ═══════════════════════════════════════════════════════════════════
# Routes
# ═══════════════════════════════════════════════════════════════════

class LoginIn(BaseModel):
    identifier: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)


_LOCKED = "Trop d'essais : cet identifiant est bloqué pendant 15 minutes."
_BAD = "Identifiant ou mot de passe incorrect."


@router.post("/api/auth/login")
def login(body: LoginIn, request: Request, response: Response):
    client = db()
    ip = "ip:" + (request.client.host if request.client else "?")
    ident = body.identifier.strip()
    key = "id:" + (security.normalize_phone(ident) if security.looks_like_phone(ident) else ident.lower())
    if _mem_is_locked(ip) or _mem_is_locked(key):
        raise HTTPException(status_code=429, detail=_LOCKED)
    acc = find_account(client, ident, with_hash=True)
    if acc is not None:
        locked = _parse(acc.get("locked_until"))
        if locked and locked > _utcnow():
            raise HTTPException(status_code=429, detail=_LOCKED)
    ok = security.verify_password(body.password, (acc or {}).get("password_hash") or "")
    if not ok or acc is None or acc.get("active") is False:
        _mem_failed(ip)
        if acc is not None:
            _record_failure(client, acc)
        else:
            # Même rythme qu'un compte réel : 401 jusqu'au 8e échec, 429 ensuite.
            _mem_failed(key)
        logger.info("Connexion refusée")
        raise HTTPException(status_code=401, detail=_BAD)
    run(client.table(T_ACCOUNTS).update({
        "failed_logins": 0, "first_failed_at": None, "locked_until": None, "last_login_at": _iso(_utcnow()),
    }).eq("username", acc["username"]), "connexion")
    issue_session(response, acc["username"], int(acc.get("session_version") or 1))
    return {"ok": True, "user": {"username": acc["username"], "display_name": acc.get("display_name"), "role": acc.get("role")}}


@router.post("/api/auth/logout")
def logout(response: Response):
    clear_session(response)
    return {"ok": True}


@router.get("/api/auth/me")
def me(request: Request, response: Response):
    if not request.cookies.get(security.CSRF_COOKIE):
        set_csrf_cookie(response)
    user = identify(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Session requise.")
    return {"user": {k: v for k, v in user.items() if k != "csrf"}}


class PasswordIn(BaseModel):
    current: str = Field(min_length=1, max_length=200)
    new: str = Field(min_length=1, max_length=200)


@router.post("/api/auth/password")
def change_password(body: PasswordIn, request: Request, response: Response):
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Session requise.")
    client = db()
    acc = find_account(client, user["username"], with_hash=True)
    if not acc or not security.verify_password(body.current, acc.get("password_hash") or ""):
        raise HTTPException(status_code=400, detail="Mot de passe actuel incorrect.")
    problem = security.password_problem(body.new)
    if problem:
        raise HTTPException(status_code=400, detail=problem)
    version = int(acc.get("session_version") or 1) + 1
    run(client.table(T_ACCOUNTS).update({
        "password_hash": security.hash_password(body.new), "session_version": version,
    }).eq("username", acc["username"]), "changement du mot de passe")
    # Les autres appareils sont déconnectés ; celui-ci reçoit une session neuve.
    issue_session(response, acc["username"], version)
    return {"ok": True}
