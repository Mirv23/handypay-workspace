"""Réglages, lus UNIQUEMENT dans les variables d'environnement.

Aucune clé, aucun secret, aucune adresse réelle n'est écrit dans le code. Un
fichier `.env` à la racine (ignoré par git) est lu au démarrage pour le
confort en local ; une variable déjà définie dans l'environnement gagne.

Deux modes :
- local : `SUPABASE_URL` vide. Les données vivent dans `.data/` (fichiers JSON),
  par un petit client qui imite celui de Supabase (cf. `localdb.py`) ;
- production : `SUPABASE_URL` et `SUPABASE_SERVICE_KEY` renseignés.
"""

from __future__ import annotations

import functools
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Lecteur minimal de `.env` (CLÉ=valeur), sans dépendance."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT / ".env")


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def env_bool(name: str, default: bool) -> bool:
    raw = env(name)
    if not raw:
        return default
    return raw.lower() not in ("0", "false", "non", "no", "off")


def env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(env(name) or default)
    except ValueError:
        return default
    return value if low <= value <= high else default


@dataclass(frozen=True)
class Settings:
    app_env: str
    supabase_url: str
    supabase_service_key: str
    data_dir: Path
    session_secret: str
    session_max_age_s: int
    cookie_secure: bool
    public_url: str

    @property
    def local(self) -> bool:
        return not self.supabase_url

    @property
    def production(self) -> bool:
        return self.app_env == "production"


def _local_secret(data_dir: Path) -> str:
    """En local seulement : secret de session généré une fois, rangé dans `.data/`."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "session_secret.txt"
    try:
        value = path.read_text(encoding="utf-8").strip()
        if len(value) >= 32:
            return value
    except OSError:
        pass
    value = secrets.token_urlsafe(48)
    path.write_text(value, encoding="utf-8")
    return value


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    supabase_url = env("SUPABASE_URL")
    app_env = env("APP_ENV", "production" if supabase_url else "local").lower()
    if app_env not in ("local", "production"):
        app_env = "production"
    data_dir = Path(env("LOCAL_DATA_DIR") or (ROOT / ".data"))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    secret = env("SESSION_SECRET")
    if len(secret) < 32:
        if app_env == "production":
            raise RuntimeError(
                "SESSION_SECRET manque ou est trop court (32 caractères au moins). "
                "Génère-le avec : python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        secret = _local_secret(data_dir)
    hours = env_int("SESSION_MAX_AGE_HOURS", 168, 1, 24 * 90)
    return Settings(
        app_env=app_env,
        supabase_url=supabase_url,
        supabase_service_key=env("SUPABASE_SERVICE_KEY"),
        data_dir=data_dir,
        session_secret=secret,
        session_max_age_s=hours * 3600,
        cookie_secure=env_bool("COOKIE_SECURE", app_env == "production"),
        public_url=(env("WORKSPACE_PUBLIC_URL") or "http://127.0.0.1:8000").rstrip("/"),
    )
