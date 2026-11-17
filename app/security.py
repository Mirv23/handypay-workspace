"""Briques de sécurité : mots de passe, session signée, CSRF, numéros de téléphone.

- Mot de passe : scrypt (hashlib), sel aléatoire de 16 octets, comparaison en
  temps constant. Format stocké : `scrypt$n$r$p$sel$empreinte` (base64).
- Session : cookie `hp_session` = charge JSON en base64 + signature HMAC-SHA256
  (`SESSION_SECRET`). La charge porte l'identifiant, l'expiration, la version de
  session du compte (changer le mot de passe déconnecte partout) et le jeton
  CSRF lié à cette session.
- CSRF : double soumission. Le cookie `hp_csrf` (lisible par la page) doit être
  renvoyé dans l'en-tête `X-CSRF-Token` de toute requête qui modifie ; une fois
  connecté, il doit en plus être celui de la session.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Optional

SESSION_COOKIE = "hp_session"
CSRF_COOKIE = "hp_csrf"
CSRF_HEADER = "x-csrf-token"

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1
_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ═══════════════════════════════════════════════════════════════════
# Mots de passe
# ═══════════════════════════════════════════════════════════════════

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


_DUMMY_HASH: Optional[str] = None


def verify_password(password: str, stored: str) -> bool:
    """Vérifie ; un compte inconnu passe par un faux calcul (même durée)."""
    global _DUMMY_HASH
    if not stored:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = hash_password(secrets.token_urlsafe(12))
        stored, password = _DUMMY_HASH, password + "\x00"
        _verify(password, stored)
        return False
    return _verify(password, stored)


def _verify(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        got = hashlib.scrypt(password.encode("utf-8"), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(_unb64(digest)))
        return hmac.compare_digest(got, _unb64(digest))
    except (ValueError, TypeError):
        return False


def generate_password() -> str:
    """16 caractères sans ambiguïté (pas de 0/O, 1/l/I), en 4 blocs : ~93 bits."""
    raw = "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(16))
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def password_problem(password: str) -> str:
    if len(password) < 10:
        return "Le mot de passe doit faire au moins 10 caractères."
    if len(password) > 200:
        return "Le mot de passe est trop long (200 caractères au plus)."
    return ""


# ═══════════════════════════════════════════════════════════════════
# Session signée
# ═══════════════════════════════════════════════════════════════════

def sign_session(secret: str, username: str, version: int, max_age_s: int, csrf: str) -> str:
    payload = {"u": username, "v": int(version), "exp": int(time.time()) + max_age_s, "c": csrf}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64(hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    return body + "." + sig


def read_session(secret: str, cookie: str) -> Optional[dict]:
    """Charge d'une session VALIDE (signature, expiration), sinon None."""
    if not cookie or "." not in cookie or len(cookie) > 2048:
        return None
    body, _, sig = cookie.partition(".")
    expected = _b64(hmac.new(secret.encode("utf-8"), body.encode("ascii", "replace"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig.encode("ascii", "replace"), expected.encode("ascii")):
        return None
    try:
        payload = json.loads(_unb64(body).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or int(payload.get("exp") or 0) < time.time():
        return None
    if not isinstance(payload.get("u"), str) or not isinstance(payload.get("c"), str):
        return None
    return payload


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def same_token(a: str, b: str) -> bool:
    if not a or not b or len(a) < 32:
        return False
    return hmac.compare_digest(a.encode("utf-8", "replace"), b.encode("utf-8", "replace"))


# ═══════════════════════════════════════════════════════════════════
# Identifiants et numéros
# ═══════════════════════════════════════════════════════════════════

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]{1,39}$")


def clean_username(raw: str) -> str:
    text = str(raw or "").strip().lower().lstrip("@")
    return text if USERNAME_RE.match(text) else ""


def normalize_phone(raw: str) -> str:
    """Numéro au format international `+509XXXXXXXX`, ou "" s'il est inutilisable.

    - 8 chiffres (numéro haïtien) → +509 devant ;
    - 509 + 8 chiffres → + devant ;
    - +… ou 00… suivi de 8 à 15 chiffres → gardé tel quel (diaspora) ;
    - 1 + 10 chiffres (États-Unis, Canada) → + devant.
    """
    text = str(raw or "").strip()
    if not text or len(text) > 40:
        return ""
    plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    if not plus and digits.startswith("00"):
        plus, digits = True, digits[2:]
    if plus:
        return "+" + digits if 8 <= len(digits) <= 15 else ""
    if len(digits) == 8:
        return "+509" + digits
    if len(digits) == 11 and digits.startswith("509"):
        return "+" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return ""


def looks_like_phone(raw: str) -> bool:
    text = str(raw or "").strip()
    return bool(text) and bool(re.fullmatch(r"[+\d\s().\-]{8,30}", text)) and len(re.sub(r"\D", "", text)) >= 8


def wa_link(phone: str, text: str) -> str:
    """Lien `https://wa.me/<chiffres>?text=…` (un humain envoie, rien d'automatique)."""
    from urllib.parse import quote

    digits = re.sub(r"\D", "", phone or "")
    base = "https://wa.me/" + digits if digits else "https://wa.me/"
    return base + ("?text=" + quote(text, safe="") if text else "")
